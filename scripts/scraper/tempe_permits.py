"""
City of Tempe Building Permit integration via ArcGIS FeatureServer.

Fetches permit data from Tempe's ArcGIS FeatureServer endpoint and
normalizes it into the Poliscopic Permit model schema.

ArcGIS endpoint:   /0/query
Pagination:        resultOffset + resultRecordCount (max 2000)
Date format:       /Date(milliseconds)/ or ISO strings
"""

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

ARCGIS_URL = (
    "https://services.arcgis.com/lQySeXwbBg53XWDi/arcgis/rest/services/"
    "building_permits/FeatureServer/0"
)
MAX_RECORD_COUNT = 2000
SOURCE_SYSTEM = "tempe_arcgis_accela_building_permits"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ── Date parsing helpers ────────────────────────────────────────────────────

# Pattern for ArcGIS millisecond timestamps:  /Date(1711929600000)/
_ARCGIS_MS_PATTERN = re.compile(r"/Date\((\d+)\)/")
_ARCGIS_MS_NUMERIC = re.compile(r"^(\d{13})")


def _parse_arcgis_date(value) -> Optional[str]:
    """Parse an ArcGIS date field value into YYYY-MM-DD string.

    Handles two formats:
    1.  ``/Date(1711929600000)/`` — millisecond timestamps
    2.  ISO date strings like ``2024-01-15T00:00:00.000Z``

    Returns None for null/empty/parse-failure.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Try millisecond timestamp patterns
    # Format 1: /Date(1711929600000)/
    m = _ARCGIS_MS_PATTERN.match(s)
    if m:
        try:
            ts_ms = int(m.group(1))
            return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return None

    # Format 2: raw 13-digit millisecond integer (from FeatureServer)
    m2 = _ARCGIS_MS_NUMERIC.match(s)
    if m2:
        try:
            ts_ms = int(m2.group(1))
            dt = datetime.utcfromtimestamp(ts_ms / 1000)
            # Sanity check: reject if year is outside reasonable range
            if dt.year < 1970 or dt.year > 2100:
                return None
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None

    # Try ISO date
    try:
        # Handle both "2024-01-15T00:00:00.000Z" and "2024-01-15"
        if "T" in s:
            dt = datetime.fromisoformat(s.split(".")[0].replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        else:
            dt = datetime.fromisoformat(s)
            return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass

    return None


# ── ArcGIS API ──────────────────────────────────────────────────────────────

def _build_query_url(
    offset: int = 0,
    count: int = MAX_RECORD_COUNT,
    where: str = "1=1",
    out_fields: str = "*",
) -> str:
    """Build a FeatureServer query URL with pagination."""
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "false",
        "resultOffset": str(offset),
        "resultRecordCount": str(count),
        "f": "json",
    }
    qs = "&".join(f"{k}={urllib.request.quote(v, safe='')}" for k, v in params.items())
    return f"{ARCGIS_URL}/query?{qs}"


def fetch_page(
    offset: int = 0,
    count: int = MAX_RECORD_COUNT,
    where: str = "1=1",
) -> dict:
    """Fetch one page of permit records from the ArcGIS FeatureServer.

    Returns the parsed JSON response dict, which contains a ``features``
    list and optional ``exceededTransferLimit`` flag.
    """
    url = _build_query_url(offset=offset, count=count, where=where)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Fetch all permit records from ArcGIS, paginating through results.

    Optionally filters by date range using ArcGIS where clause.
    Returns a flat list of feature attributes (dicts).

    Args:
        start_date: YYYY-MM-DD, earliest AppliedDateDtm to include.
        end_date: YYYY-MM-DD, latest AppliedDateDtm to include.
        limit: Maximum total records to fetch.
    """
    # Build WHERE clause
    where_parts = ["1=1"]
    if start_date:
        where_parts.append(f"AppliedDateDtm >= DATE '{start_date}'")
    if end_date:
        where_parts.append(f"AppliedDateDtm <= DATE '{end_date}'")
    where = " AND ".join(where_parts)

    all_records: list[dict] = []
    offset = 0

    while limit is None or len(all_records) < limit:
        batch_count = MAX_RECORD_COUNT
        if limit is not None:
            remaining = limit - len(all_records)
            batch_count = min(batch_count, remaining)

        data = fetch_page(offset=offset, count=batch_count, where=where)
        features = data.get("features", [])

        if not features:
            break

        for feat in features:
            all_records.append(feat.get("attributes", {}))

        # Check if there are more pages
        exceeded = data.get("exceededTransferLimit", False)
        if not exceeded:
            break

        offset += len(features)
        time.sleep(0.5)  # rate-limit politeness

    return all_records


# ── Normalization ───────────────────────────────────────────────────────────

def normalize_row(arcgis_row: dict) -> dict:
    """Map an ArcGIS feature attribute dict to Permit model field names.

    Returns a flat dict with Permit-column keys ready for database insertion.
    """
    today_str = date.today().isoformat()
    date_str_for_adid = today_str.replace("-", "")

    # Parse ArcGIS date fields
    permit_issue_date = _parse_arcgis_date(arcgis_row.get("IssuedDateDtm"))
    applied_date = _parse_arcgis_date(arcgis_row.get("AppliedDateDtm"))
    completed_date = _parse_arcgis_date(arcgis_row.get("CompletedDateDtm"))
    if not completed_date:
        completed_date = _parse_arcgis_date(arcgis_row.get("CompletedDate"))
    co_date = _parse_arcgis_date(arcgis_row.get("COIssuedDateDtm"))
    if not co_date:
        co_date = _parse_arcgis_date(arcgis_row.get("COIssuedDate"))

    # Build raw source fields
    raw_permit_type = arcgis_row.get("PermitType") or None
    raw_permit_type_desc = arcgis_row.get("PermitTypeDesc") or None
    raw_permit_class = arcgis_row.get("PermitClass") or None

    # Description
    description = arcgis_row.get("Description") or None

    # Permit number
    permit_number = str(arcgis_row.get("PermitNum") or "")
    if not permit_number:
        permit_number = None

    # Source record ID (OBJECTID)
    source_record_id = str(arcgis_row.get("OBJECTID") or "")
    if not source_record_id:
        source_record_id = None

    # Housing units
    units_val = arcgis_row.get("HousingUnits")
    units = str(units_val) if units_val is not None else None

    # Square feet
    sqft_val = arcgis_row.get("TotalSqFt")
    permit_sqft = str(int(sqft_val)) if sqft_val is not None else None

    # Valuation
    val_val = arcgis_row.get("EstProjectCost")
    permit_val = str(int(val_val)) if val_val is not None else None

    # Fee
    fee_val = arcgis_row.get("Fee")
    fee = str(fee_val) if fee_val is not None else None

    # Coordinates
    lat = arcgis_row.get("Latitude")
    lng = arcgis_row.get("Longitude")
    latitude = str(lat) if lat is not None else None
    longitude = str(lng) if lng is not None else None

    # Contractor license
    contractor_license = str(arcgis_row.get("ContractorLicNum") or "") or None

    # Build the normalized record
    record = {
        "permit_number": permit_number,
        "permit_description": description,
        "permit_issue_date": permit_issue_date,
        "applied_date": applied_date,
        "completed_date": completed_date,
        "certificate_of_occupancy_date": co_date,
        "permit_status": arcgis_row.get("StatusCurrent") or None,
        "job_address": arcgis_row.get("OriginalAddress1") or None,
        "job_city": arcgis_row.get("OriginalCity") or None,
        "job_state": arcgis_row.get("OriginalState") or None,
        "job_zip": str(arcgis_row.get("OriginalZip") or "") or None,
        "raw_permit_type": raw_permit_type,
        "raw_permit_type_description": raw_permit_type_desc,
        "raw_permit_class": raw_permit_class,
        "permit_square_feet": permit_sqft,
        "units": units,
        "no_units": units,
        "permit_valuation": permit_val,
        "project_name": arcgis_row.get("ProjectName") or None,
        "fee": fee,
        "latitude": latitude,
        "longitude": longitude,
        "contractor_name": arcgis_row.get("ContractorCompanyName") or None,
        "contractor_license": contractor_license,
        "zone": arcgis_row.get("Zone") or None,
        "jurisdiction": "City of Tempe",
        "source_system": SOURCE_SYSTEM,
        "source_record_id": source_record_id,
    }

    # Normalized category based on raw permit type
    record["normalized_category"] = categorize_permit(
        raw_permit_type, raw_permit_type_desc, description,
        raw_permit_class=arcgis_row.get("PermitClass"),
    )
    record["work_type"] = classify_work_type(
        arcgis_row.get("PermitClass"), description, raw_permit_type,
    )

    # Row hash for dedup
    hash_parts = [
        record.get("permit_number") or "",
        record.get("source_record_id") or "",
        record.get("permit_issue_date") or "",
        record.get("job_address") or "",
    ]
    record["row_hash"] = hashlib.sha256(
        "||".join(hash_parts).encode("utf-8")
    ).hexdigest()

    # Source tracking
    record["report_date"] = today_str
    record["report_adid"] = f"tempe-arcgis-{date_str_for_adid}"
    record["source_file"] = "tempe-arcgis-api"

    return record


def categorize_permit(
    raw_permit_type: Optional[str] = None,
    raw_permit_type_desc: Optional[str] = None,
    description: Optional[str] = None,
    raw_permit_class: Optional[str] = None,
) -> str:
    """Categorize a Tempe permit into cross-jurisdiction category.

    Priority: raw_permit_class (most specific) → raw_permit_type →
              raw_permit_type_desc → description

    raw_permit_class contains codes like "106 New - Ten or more Family"
    (Residential), "437 - Additions and Alterations - Non-Residential"
    (Commercial), "330 - Commercial Buildings" (Commercial), etc.

    Returns one of: Residential, Commercial, Industrial, Mixed-Use,
    Infrastructure, Trade, Demolition, Other
    """
    # Collect all text sources, filtering out None
    candidates = [raw_permit_type, raw_permit_type_desc, description]
    text = " ".join(c.strip().lower() for c in candidates if c and c.strip())

    # Check raw_permit_class separately with higher priority
    pclass = (raw_permit_class or "").strip().lower()

    if not text and not pclass:
        return "Other"

    # Check class first (most specific signal)
    if pclass:
        # Check "non-residential" before "residential" to avoid substring overlap
        if "non-residential" in pclass:
            return "Commercial"
        if "residential" in pclass or "single family" in pclass or "ten or more family" in pclass:
            return "Residential"
        if "guesthouse" in pclass or "pool - residential" in pclass:
            return "Residential"
        if "commercial" in pclass or "pool - non-residential" in pclass:
            return "Commercial"
        if "industrial" in pclass:
            return "Industrial"
        if "mixed" in pclass:
            return "Mixed-Use"
        if "water" in pclass or "sewer" in pclass or "drainage" in pclass or "paving" in pclass:
            return "Infrastructure"
        # Trade permits (electrical, plumbing, mechanical, maintenance)
        # "No Value" suffix indicates trade work, not new construction
        if "electrical" in pclass or "plumbing" in pclass or "mechanical" in pclass:
            return "Trade"
        if "foundation only" in pclass:
            return "Commercial"
        if "hotel" in pclass or "motel" in pclass:
            return "Commercial"
        if "parking garage" in pclass:
            return "Commercial"
        if "educational" in pclass or "customer service" in pclass:
            return "Commercial"
        if "demolition" in pclass:
            return "Demolition"

        # Photovoltaic + Residential class code
        if "photovoltaic" in pclass and "residential" in pclass:
            return "Residential"
        if "photovoltaic" in pclass and "commercial" in pclass:
            return "Commercial"

    # Fall back to text fields
    if not text:
        return "Other"

    if "mixed" in text or "mixed use" in text or "mixed-use" in text:
        return "Mixed-Use"

    if "industrial" in text:
        return "Industrial"

    if "residential" in text:
        return "Residential"

    if "commercial" in text or "sign" in text:
        return "Commercial"

    if "infrastructure" in text or "street" in text or "water" in text or "sewer" in text:
        return "Infrastructure"

    return "Other"


def classify_work_type(
    raw_permit_class: Optional[str] = None,
    description: Optional[str] = None,
    raw_permit_type: Optional[str] = None,
) -> str:
    """Classify a Tempe permit by work type: New Construction, Addition,
    Alteration, Trade, Demolition, Infrastructure, or Unknown.

    Uses a priority chain: raw_permit_class (codes) → description (free text)
    """
    pclass = (raw_permit_class or "").strip().lower()
    desc = (description or "").upper().strip()
    ptype = (raw_permit_type or "").lower()

    # ── Trade permits ──
    if any(t in pclass for t in ["electrical", "plumbing", "mechanical", "photovoltaic"]):
        return "Trade"
    if pclass in ("uf - underground fire", "rae - engineering revisions", "st - street lights"):
        return "Trade"
    if ("replace" in desc.lower() or "panel upgrade" in desc.lower()) and "panel" in desc.lower():
        return "Trade"

    # ── Demolition ──
    if "demolition" in pclass or desc.startswith("DEMO") or "site demo" in desc.lower():
        return "Demolition"

    # ── Infrastructure ──
    if any(t in pclass for t in ["water", "sewer", "drainage", "paving"]):
        return "Infrastructure"
    if "sewer" in desc.lower() or "water" in desc.lower():
        return "Infrastructure"

    # ── New Construction ──
    if pclass.startswith("10") and "new" in pclass:
        return "New Construction"
    if "foundation only" in pclass:
        return "New Construction"
    if "hotel" in pclass or "motel" in pclass:
        return "New Construction"
    if pclass.startswith("330") and "commercial" in pclass:
        return "New Construction"
    if "industrial or warehouse" in pclass or "churches" in pclass:
        return "New Construction"
    if "mobile home" in pclass:
        return "New Construction"
    if "customer service" in pclass:
        return "New Construction"
    # ── Addition (check BEFORE general "NEW" to catch ADU/guesthouse) ──
    if "guesthouse" in pclass:
        return "Addition"
    if "carport" in pclass:
        return "Addition"
    if "pool" in pclass or "spa" in pclass:
        return "Addition"
    if "walls or fences" in pclass:
        return "Addition"
    if "new garage" in pclass or "new carport" in pclass:
        return "Addition"
    if desc:
        if "ADDITION" in desc and "REMOV" not in desc:
            return "Addition"
        if "NEW ADU" in desc or "NEW GUEST HOUSE" in desc or "NEW DETACHED" in desc:
            return "Addition"
        if "NEW SHADE" in desc or "NEW CANOP" in desc:
            return "Addition"

    # ── New Construction ──
    if desc:
        if desc.startswith("NEW ") or desc.startswith("CONSTRUCT NEW "):
            return "New Construction"
        if "PHASED" in desc and ("FOUNDATION" in desc or "CONSTRUCTION" in desc):
            return "New Construction"
        if "AT RISK GRADING" in desc:
            return "New Construction"

    # ── Alteration (renovation of existing space) ──
    if "additions and alterations - non-residential" in pclass:
        return "Alteration"
    if "additions or alterations - residential" in pclass:
        return "Alteration"
    if desc:
        if desc.startswith("TI") or "TENANT IMPROVEMENT" in desc:
            return "Alteration"
        if "REMODEL" in desc or "RENOVATION" in desc or "RENOVATE" in desc:
            return "Alteration"
        if "UPGRADE" in desc or "REPLACEMENT" in desc or "REPLACE" in desc:
            return "Alteration"
        if "REPAIR" in desc:
            return "Alteration"
        if "RESTORE" in desc or "INTERIOR" in desc:
            return "Alteration"
        if "INSTALL" in desc:
            return "Alteration"

    if "educational" in pclass:
        return "Alteration"
    if "renewal" in pclass:
        return "Alteration"
    if "sign" in pclass:
        return "Alteration"
    if "non-structural" in pclass:
        return "Alteration"

    # ── Ambiguous ──
    if pclass.startswith("999") or pclass.startswith("misc"):
        if desc:
            if "GRADING" in desc and ("NEW" in desc or "AT RISK" in desc):
                return "New Construction"
            if "DEMO" in desc or "DEMOLITION" in desc:
                return "Demolition"
            if "CONSTRUCT PEDESTRIAN" in desc:
                return "Infrastructure"
        return "Unknown"

    if "single family residence" in pclass or pclass in ("", None):
        return "Unknown"

    return "Unknown"


# ── Inspection ──────────────────────────────────────────────────────────────

def inspect_source(limit: int = 5) -> None:
    """Fetch sample records from ArcGIS and print field names + values for inspection."""
    records = fetch_all(limit=limit)

    if not records:
        print("No records returned from ArcGIS endpoint.")
        return

    print(f"Fetched {len(records)} sample record(s):\n")

    for i, row in enumerate(records):
        print(f"--- Record {i + 1} ---")
        for key, value in sorted(row.items()):
            print(f"  {key}: {value}")
        print()

    # Print all unique field names across all records
    all_fields: set[str] = set()
    for row in records:
        all_fields.update(row.keys())
    print(f"\nAll fields ({len(all_fields)}): {', '.join(sorted(all_fields))}")


# ── Database sync ───────────────────────────────────────────────────────────

def _get_db():
    import db as _db
    return _db


def sync_permits(
    session,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Fetch Tempe permits from ArcGIS, normalize, and upsert into the database.

    Uses bulk operations for performance: pre-loads existing records into a
    lookup dict, then iterates to determine inserts vs updates.

    Returns a summary dict with keys: fetched, inserted, updated, errors.
    """
    log.info(
        "Fetching Tempe permits (start=%s, end=%s, limit=%s, dry_run=%s)",
        start_date, end_date, limit, dry_run,
    )

    db_mod = _get_db()
    Permit = db_mod.Permit

    records = fetch_all(start_date=start_date, end_date=end_date, limit=limit)
    log.info("Fetched %d records from ArcGIS", len(records))

    if dry_run:
        log.info("Dry run: %d records would be processed", len(records))
        return {"fetched": len(records), "inserted": 0, "updated": 0, "errors": 0}

    from sqlalchemy import select

    # Pre-load all existing Tempe permits into a lookup dict
    # Key: (source_system, source_record_id) → Permit object
    existing_rows = session.execute(
        select(Permit).where(Permit.source_system == SOURCE_SYSTEM)
    ).scalars().all()

    existing_map = {}
    for p in existing_rows:
        key = (p.source_system or "", str(p.source_record_id or ""))
        existing_map[key] = p

    log.info("Loaded %d existing Tempe permits for dedup", len(existing_map))

    summary = {"fetched": len(records), "inserted": 0, "updated": 0, "errors": 0}
    batch_insert = []
    batch_update = []

    for arcgis_row in records:
        try:
            normalized = normalize_row(arcgis_row)
        except Exception as e:
            log.error("Normalization error: %s", e)
            summary["errors"] += 1
            continue

        source_system = normalized.get("source_system")
        source_record_id = normalized.get("source_record_id")

        if not source_system or not source_record_id:
            log.warning("Skipping row without source_system/source_record_id: %s", normalized.get("permit_number"))
            summary["errors"] += 1
            continue

        key = (source_system, str(source_record_id))
        existing = existing_map.get(key)

        if existing:
            # Update fields that may have changed
            for col, val in normalized.items():
                if col in ("row_hash", "report_date", "report_adid", "source_file"):
                    continue
                setattr(existing, col, val)
            summary["updated"] += 1
        else:
            permit = Permit(**normalized)
            session.add(permit)
            summary["inserted"] += 1

    session.commit()
    log.info(
        "Sync complete: %d inserted, %d updated, %d errors (elapsed=%.1fs)",
        summary["inserted"], summary["updated"], summary["errors"],
        time.time() - _t0 if '_t0' in dir() else 0,
    )

    return summary
