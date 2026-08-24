"""
City of Phoenix PDD Issued Permit integration via ArcGIS Planning_Permit MapServer.

Fetches permit data from Phoenix's ArcGIS MapServer Layer 1 (Permits)
and normalizes it into the Poliscopic Permit model schema.

ArcGIS endpoint:  /1/query
Pagination:       resultOffset + resultRecordCount (max 1000)
Date format:      epoch milliseconds (JavaScript-style 13-digit integers)
Geometry:         Points in Web Mercator (EPSG:3857) → WGS84 (EPSG:4326)

Source: https://maps.phoenix.gov/pub/rest/services/Public/Planning_Permit/MapServer/1
"""

import csv
import hashlib
import io
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

BASE_URL = (
    "https://maps.phoenix.gov/pub/rest/services/Public/Planning_Permit/MapServer"
)
LAYER_ID = 1  # Permits
MAX_RECORD_COUNT = 1000
SOURCE_SYSTEM = "phoenix_arcgis_planning_permit"
PDD_SOURCE_SYSTEM = "phoenix_pdd"

# ── PDD CSV Export API ──────────────────────────────────────────────────────

PDD_BASE_URL = "https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit"
COFO_BASE_URL = "https://apps-secure.phoenix.gov/PDD/Search/CertOfOccupancyDataDownload"


# ── Phoenix permit type code classification ─────────────────────────────────
# Phoenix uses short type codes (RSF, BLD, SGNP, etc.) mapped against the
# city's PDD unit/valuation/sqft CSVs and ArcGIS Planning_Permit layer.
#
# Code prefix patterns:
#   R*   — Residential (RSF=Single Family, RM=Multi-family, RSME=Alteration, RE=Existing, etc.)
#   C*   — Commercial (CSW=Shell Work, CCO=Commercial CO, CPA=Plan Amend, etc.)
#   BLD  — Building (commercial or mixed-use construction)
#   LP*  — Land-use permits (LPRM=Plan Review, LPRN=New, etc.)
#   LS*  — Land-use / subdivision
#   SGNP — Sign Permit
#   F*   — Fire / safety systems (F193, FPPD, etc.)
#   ELEC — Electrical
#   PLMB — Plumbing
#   MECH — Mechanical
#   WS*  — Water / sewer infrastructure
#   DEM  — Demolition
#   RDEM — Residential Demolition
#   OE/OP/OS/OBLD — Other / existing building work


PHX_CATEGORY_MAP: dict[str, str] = {
    # Residential type codes
    "RSF": "Residential", "RS": "Residential", "RSME": "Residential",
    "RSP": "Residential", "RM": "Residential", "RSTD": "Residential",
    "RSFA": "Residential", "RSFC": "Residential", "RSFI": "Residential",
    "RE": "Residential", "REM": "Residential", "REC": "Residential",
    "RPV": "Residential", "RSE": "Residential", "RWH": "Residential",
    "RFEN": "Residential", "RPBI": "Residential", "RPSC": "Residential",
    "RPRL": "Residential", "RPRS": "Residential", "RPRC": "Residential",
    "RPRM": "Residential", "RPRU": "Residential", "RDEM": "Residential",
    "RMC": "Residential", "RNSP": "Residential", "RPDR": "Residential",
    "RCIT": "Residential",
    # RV* prefix = REVISION TO PLAN in the current PDD Online system
    "RVSX": "Plan Review", "RVSN": "Plan Review",
    "RVSC": "Plan Review", "RVCA": "Plan Review",
    # RPDR = RESIDENTIAL PLAN REVIEW - DESIGN REVIEW
    "RPDR": "Plan Review",
    "RSM": "Residential",
    "RSC": "Residential",
    # Commercial type codes
    "BLD": "Commercial", "BLDS": "Commercial", "BLDA": "Commercial",
    "BLSC": "Commercial", "SGNP": "Commercial", "SGNT": "Commercial",
    "SGN": "Commercial", "SGNV": "Commercial",
    # C-prefix commercial codes
    "C": "Commercial",
    # LP-prefix land-use codes (mostly commercial development)
    "LP": "Commercial",
    # LS-prefix land-use / subdivision codes
    "LS": "Commercial",
    # Trade / specialty
    "ELEC": "Trade", "PLMB": "Trade", "MECH": "Trade",
    "ELFT": "Trade", "ELEV": "Trade", "EHYD": "Trade",
    # Infrastructure
    "WS": "Infrastructure",
    # Demolition
    "DEM": "Demolition", "ABND": "Demolition",
    # Other / misc
    "OE": "Other", "OP": "Other", "OS": "Other",
    "OME": "Other", "OPE": "Other", "OPME": "Other",
    "OSE": "Other", "OSP": "Other", "OSPE": "Other",
    "OM": "Other", "OMOV": "Other", "OPBF": "Other",
    "OBLD": "Other",
    # Pool
    "POOL": "Other",
}

# Exact-match work type for key Phoenix type codes
PHX_WORK_TYPE_MAP: dict[str, str] = {
    # Residential new construction
    "RSF": "New Construction", "RS": "New Construction",
    "RSME": "Alteration", "RSP": "New Construction",
    "RM": "New Construction", "RSTD": "New Construction",
    "RSFA": "New Construction", "RSFC": "New Construction",
    "RSFI": "New Construction", "RSC": "New Construction",
    "RSM": "New Construction",
    # RV* prefix = REVISION TO PLAN in the current PDD Online system
    "RVSN": "Plan Review",
    "RVSX": "Plan Review", "RVSC": "Plan Review",
    "RVCA": "Plan Review",
    # RPDR = RESIDENTIAL PLAN REVIEW - DESIGN REVIEW
    "RPDR": "Plan Review",
    "COND": "New Construction",
    # Residential alterations
    "RE": "Alteration", "REM": "Alteration", "REC": "Alteration",
    "RPV": "Alteration", "RSE": "Alteration", "RWH": "Alteration",
    "RFEN": "Alteration", "RPBI": "Alteration", "RPSC": "Alteration",
    "RPRL": "Alteration", "RPRS": "Alteration", "RPRC": "Alteration",
    "RPRM": "Alteration", "RPRU": "Alteration", "RMC": "Alteration",
    "RNSP": "Alteration", "RPDR": "Alteration", "RCIT": "Alteration",
    # Demolition
    "RDEM": "Demolition", "DEM": "Demolition",
    "ABND": "Demolition",
    # Commercial new construction
    "BLD": "New Construction", "BLDS": "New Construction",
    "BLDA": "New Construction", "BLSC": "New Construction",
    "CSW": "New Construction", "CWT": "New Construction",
    "CSL": "New Construction", "CSE": "Alteration",
    "CSIT": "Alteration", "CP": "New Construction",
    "CGD": "New Construction", "CCO": "New Construction",
    "CPR": "New Construction", "CES": "New Construction",
    "CDF": "New Construction", "CLS": "New Construction",
    "CLT": "New Construction", "CMC": "New Construction",
    "CMOD": "Alteration", "CMW": "New Construction",
    "CPA": "New Construction", "CPGD": "New Construction",
    "CPSW": "New Construction", "CPSE": "New Construction",
    "CPST": "New Construction", "CPWT": "New Construction",
    "CPPA": "New Construction", "CST": "New Construction",
    "CC": "New Construction", "CCPR": "New Construction",
    "CDW": "New Construction", "CEF": "New Construction",
    "CFL": "New Construction", "CFB": "New Construction",
    # Signs (trade)
    "SGNP": "Trade", "SGNT": "Trade", "SGN": "Trade",
    "SGNV": "Trade", "LPSG": "Trade",
    # Trade permits
    "ELEC": "Trade", "PLMB": "Trade", "MECH": "Trade",
    "ELFT": "Trade", "ELEV": "Trade", "EHYD": "Trade",
    "ENVR": "Trade", "ETRC": "Trade", "EXTR": "Trade",
    # Fire/safety (F-prefix digit codes)
    "F": "Trade",
    # Infrastructure
    "WS": "Infrastructure",
    # Plans / pre-development (mostly LP/LS prefix)
    "LPRM": "New Construction", "LPRR": "New Construction",
    "LPRS": "New Construction", "LPRT": "New Construction",
    "LPRX": "New Construction", "LPRD": "New Construction",
    "LPRN": "New Construction", "LPR": "New Construction",
    "LP": "New Construction", "LSPL": "New Construction",
    "LPSC": "New Construction", "LS": "New Construction",
    "LSAL": "New Construction", "LSIN": "New Construction",
    "LSIS": "New Construction", "PLAT": "New Construction",
    "PLZA": "New Construction", "PHAS": "New Construction",
    "PAPP": "New Construction", "PE": "New Construction",
    "PME": "New Construction", "MDHM": "New Construction",
    "MHZ": "New Construction",
    # Other building alterations
    "OBLD": "Alteration", "OE": "Alteration", "OM": "Alteration",
    "OME": "Alteration", "OP": "Alteration", "OPE": "Alteration",
    "OPME": "Alteration", "OS": "Alteration", "OSE": "Alteration",
    "OSPE": "Alteration", "OSP": "Alteration",
    # Certificates of occupancy
    "COFO": "New Construction", "COFC": "New Construction",
    # Pool additions
    "POOL": "Addition",
    # Fire system (all F-prefix digit codes)
    "FBB": "Trade", "FBBR": "Trade", "FCPR": "Trade",
    "FITM": "Trade", "FPPR": "Trade", "FPPD": "Trade",
    "FPPX": "Trade", "FPSR": "Trade", "FPST": "Trade",
    "FPAP": "Trade", "FPAX": "Trade", "FOCS": "Trade",
}


def categorize_phoenix_type(native_type: Optional[str]) -> tuple:
    """Classify a Phoenix PDD native_type code into (category, work_type).

    Uses exact-match maps for common codes, then falls back to prefix
    matching for less common codes. Returns (None, None) when unmapped
    (caller defaults to Other/Unknown).
    """
    if not native_type:
        return None, None

    code = native_type.upper().strip()
    category = PHX_CATEGORY_MAP.get(code)
    work_type = PHX_WORK_TYPE_MAP.get(code)

    if category is None:
        # Prefix-based fallback for category
        if code.startswith("R") and not code.startswith("RV"):
            category = "Residential"
        elif code.startswith("RV"):
            category = "Plan Review"
        elif code.startswith("C"):
            category = "Commercial"
        elif code.startswith("LP") or code.startswith("LS"):
            category = "Commercial"
        elif code.startswith("SGN") or code.startswith("SGNP"):
            category = "Commercial"
        elif code.startswith("F") and len(code) >= 2 and code[1:].isdigit():
            category = "Commercial"
        elif code.startswith("WS"):
            category = "Infrastructure"
        elif code.startswith("EL") or code.startswith("PL") or code == "MECH":
            category = "Trade"

    if work_type is None:
        # Prefix-based fallback for work type
        if code.startswith("F") and len(code) >= 2 and code[1:].isdigit():
            work_type = "Trade"
        elif code.startswith("RE") or code.startswith("RSE") or code.startswith("REM"):
            work_type = "Alteration"
        elif code.startswith("RS") or code.startswith("RM"):
            work_type = "New Construction"
        elif code.startswith("R") and not code.startswith("RV"):
            work_type = "Alteration"
        elif code.startswith("RV"):
            work_type = "Plan Review"
        elif code.startswith("SGN"):
            work_type = "Trade"
        elif code.startswith("EL") or code.startswith("PL") or code == "MECH":
            work_type = "Trade"
        elif code.startswith("LP") or code.startswith("LS"):
            work_type = "New Construction"
        elif code.startswith("WS"):
            work_type = "Infrastructure"
        elif code.startswith("C"):
            work_type = "New Construction"
        elif code == "BLD":
            work_type = "New Construction"

    return category, work_type


# ── PDD CSV date parsing ────────────────────────────────────────────────────

_PDD_DATE_PATTERN = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}:\d{2}\s+(AM|PM)$",
    re.I,
)


def _parse_pdd_date(value: Optional[str]) -> Optional[str]:
    """Parse a PDD CSV date string like '5/12/2026 8:35:46 AM'
    into YYYY-MM-DD format.

    Returns None for empty/null/unparseable values.
    """
    if not value:
        return None
    s = str(value).strip().strip('"')
    if not s:
        return None
    m = _PDD_DATE_PATTERN.match(s)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _parse_pdd_currency(value: Optional[str]) -> Optional[str]:
    """Parse a PDD CSV currency string like '$8,117,070.01'
    into a clean number string like '8117070.01'.

    Returns None for empty/null.
    """
    if not value:
        return None
    s = str(value).strip().strip('"')
    if not s or s == "$":
        return None
    # Strip leading $ and all commas
    s = s.replace("$", "").replace(",", "").strip()
    if not s:
        return None
    return s


def _parse_pdd_numeric(value: Optional[str]) -> Optional[str]:
    """Parse a PDD CSV numeric string into a clean number string.

    Handles both integer and decimal values.
    """
    if value is None:
        return None
    s = str(value).strip().strip('"')
    if not s:
        return None
    # Remove commas
    s = s.replace(",", "").strip()
    if not s:
        return None
    return s


# ── PDD CSV API ────────────────────────────────────────────────────────────

def _export_csv_url(date_range: tuple[date, date], endpoint: str) -> str:
    """Build the ExportToCSV URL for a given date range and endpoint."""
    start_str = date_range[0].strftime("%m/%d/%Y")
    end_str = date_range[1].strftime("%m/%d/%Y")
    return f"{endpoint}/ExportToCSV"


def _build_csv_body(start_date: date, end_date: date) -> str:
    """Build the form-encoded POST body for the CSV export endpoint."""
    return (
        f"PermitType=&StructureClass=%25"
        f"&StartDate={start_date.strftime('%m/%d/%Y')}"
        f"&EndDate={end_date.strftime('%m/%d/%Y')}"
        f"&SortBy=PER_ISSUE_DATE"
    )


def _fetch_csv_content(
    start_date: date, end_date: date, endpoint: str = PDD_BASE_URL,
    max_retries: int = 3,
) -> str:
    """POST to the PDD ExportToCSV endpoint and return the raw CSV text.

    Retries on transient network errors (ConnectionResetError, timeout).
    """
    url = _export_csv_url((start_date, end_date), endpoint)
    body = _build_csv_body(start_date, end_date).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
            # Try UTF-8 first, fall back to latin-1 for encoding issues
            try:
                return raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        except (urllib.error.URLError, ConnectionResetError, TimeoutError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(
                    "PDD CSV fetch failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1, max_retries, e, wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "PDD CSV fetch failed after %d attempts: %s",
                    max_retries, e,
                )
                raise


def _parse_csv_rows(csv_text: str, limit: Optional[int] = None) -> list[list[str]]:
    """Parse the PDD CSV text, skipping the first two metadata/header rows.

    Returns a list of row lists (strings), with empty rows skipped.
    """
    # Strip NUL bytes that sometimes appear in PDD CSV exports
    clean_text = csv_text.replace("\x00", "")
    reader = csv.reader(io.StringIO(clean_text))
    rows = list(reader)

    # Skip first two rows: metadata line and column headers
    data_rows = rows[2:] if len(rows) > 2 else []

    parsed = []
    for row in data_rows:
        # Skip empty rows
        if not row or all(cell.strip() == "" for cell in row):
            continue
        parsed.append(row)
        if limit is not None and len(parsed) >= limit:
            break

    return parsed


def _normalize_pdd_row(csv_row: list[str], report_id: str = "") -> dict:
    """Map a single PDD CSV row (23 columns) to Permit model fields.

    Column indices (0-indexed):
    0: Type (permit type code)
    1: Number (permit number)
    2: Issue Date (M/d/yyyy h:mm:ss AM/PM)
    3: Status
    4: Final Date (M/d/yyyy h:mm:ss AM/PM or empty)
    5: Struct Class (structure class code)
    6: Census (census tract)
    7: Use (PRIVATE, COMMERCIAL, etc.)
    8: Subdivision
    9: Lot
    10: Address
    11: Parcel (APN)
    12: QtrSec
    13: Floor Area (numeric string)
    14: Units (numeric string)
    15: Total Fees (currency string)
    16: Zoning
    17: Valuation (currency string)
    18: Owner
    19: Owner Address
    20: Contractor
    21: Contr. Phone
    22: Plan Num
    """
    today_str = date.today().isoformat()

    def _val(idx):
        return csv_row[idx].strip() if idx < len(csv_row) else ""

    # Core fields
    raw_type = _val(0) or None
    permit_number = _val(1) or None
    issue_date_raw = _val(2) or None
    struct_class = _val(5) or None  # PDD Struct Class (001-997 series)
    permit_status = _val(3) or None
    completed_date_raw = _val(4) or None
    address = _val(10) or None
    parcel = _val(11) or None
    floor_area_raw = _val(13) or None
    units_raw = _val(14) or None
    total_fees_raw = _val(15) or None
    zoning = _val(16) or None
    valuation_raw = _val(17) or None
    owner = _val(18) or None
    contractor = _val(20) or None
    contractor_phone = _val(21) or None
    plan_num = _val(22) or None
    use_code = _val(7) or None
    subdivision = _val(8) or None
    lot = _val(9) or None

    # Parse dates
    permit_issue_date = _parse_pdd_date(issue_date_raw)
    completed_date = _parse_pdd_date(completed_date_raw)

    # Parse numeric/currency fields
    floor_area = _parse_pdd_numeric(floor_area_raw)
    units = _parse_pdd_numeric(units_raw)
    total_fees = _parse_pdd_currency(total_fees_raw)
    valuation = _parse_pdd_currency(valuation_raw)

    # Source record ID: prefer Plan Num, fall back to Number
    source_record_id = plan_num or permit_number

    today_str = date.today().isoformat()
    # Use provided report_id for dedup; fall back to today's date
    adid = report_id or today_str.replace("-", "")

    # Row hash
    hash_parts = [permit_number or "", today_str]
    row_hash = hashlib.sha256(
        "||".join(hash_parts).encode("utf-8")
    ).hexdigest()

    # Phoenix-specific classification using native_type codes
    phx_cat, phx_wt = categorize_phoenix_type(raw_type)

    record = {
        "permit_number": permit_number,
        "permit_issue_date": permit_issue_date,
        "permit_status": permit_status,
        "completed_date": completed_date,
        "job_address": address,
        "parcel_no": parcel,
        "permit_square_feet": floor_area,
        "units": units,
        "permit_valuation": valuation,
        "owner_name": owner,
        "contractor_name": contractor,
        "contractor_phone": contractor_phone,
        "zone": zoning,
        "fee": total_fees,
        "source_record_id": source_record_id,
        "native_type": raw_type,
        "raw_permit_class": use_code,
        "struct_class": struct_class,
        "subdivision": subdivision,
        "lot": lot,
        # Phoenix PDD type code classification
        "normalized_category": phx_cat or "Other",
        "work_type": phx_wt or "Unknown",
        "row_hash": row_hash,
        "report_date": today_str,
        "report_adid": f"pdd-csv-export-{adid}",
        "source_system": PDD_SOURCE_SYSTEM,
        "jurisdiction": "City of Phoenix",
    }

    return record


def sync_pdd_permits(
    session,
    start_date: date,
    end_date: date,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Fetch PDD issued permits for a date range from the CSV export endpoint,
    normalize, and upsert into the database.

    Uses bulk operations for performance: pre-loads existing records into a
    lookup dict, then iterates to determine inserts vs updates.

    Args:
        session: SQLAlchemy session.
        start_date: Earliest issue date (datetime.date).
        end_date: Latest issue date (datetime.date), inclusive.
        limit: Cap on records to process (for testing).
        dry_run: Preview only, no database writes.

    Returns:
        Summary dict with keys: fetched, inserted, updated, errors.
    """
    log.info(
        "Fetching PDD permits (start=%s, end=%s, limit=%s, dry_run=%s)",
        start_date, end_date, limit, dry_run,
    )

    db_mod = _get_db()
    Permit = db_mod.Permit

    # Fetch CSV
    csv_text = _fetch_csv_content(start_date, end_date)
    csv_rows = _parse_csv_rows(csv_text, limit=limit)
    log.info("Fetched %d rows from PDD CSV export", len(csv_rows))

    if dry_run:
        log.info("Dry run: %d records would be processed", len(csv_rows))
        return {"fetched": len(csv_rows), "inserted": 0, "updated": 0, "errors": 0}

    from sqlalchemy import select

    # Pre-load all existing PDD permits into a lookup dict
    existing_rows = session.execute(
        select(Permit).where(Permit.source_system == PDD_SOURCE_SYSTEM)
    ).scalars().all()

    existing_map = {}
    for p in existing_rows:
        key = (p.source_system or "", str(p.source_record_id or ""))
        existing_map[key] = p

    log.info("Loaded %d existing PDD permits for dedup", len(existing_map))

    summary = {"fetched": len(csv_rows), "inserted": 0, "updated": 0, "errors": 0, "skipped": 0}
    seen_hashes: set = set()  # dedup within the current CSV chunk

    for csv_row in csv_rows:
        try:
            normalized = _normalize_pdd_row(csv_row, report_id=start_date.strftime("%Y%m%d"))
        except Exception as e:
            log.error("Normalization error: %s", e)
            summary["errors"] += 1
            continue

        source_system = normalized.get("source_system")
        source_record_id = normalized.get("source_record_id")

        if not source_system or not source_record_id:
            log.warning(
                "Skipping row without source: %s", normalized.get("permit_number")
            )
            summary["errors"] += 1
            continue

        # In-chunk dedup: skip if same (adid, hash) already seen in this CSV
        r_adid = normalized.get("report_adid", "")
        r_hash = normalized.get("row_hash", "")
        chunk_key = (r_adid, r_hash)
        if chunk_key in seen_hashes:
            summary["skipped"] += 1
            continue
        seen_hashes.add(chunk_key)

        key = (source_system, str(source_record_id))
        existing = existing_map.get(key)

        if existing:
            # Update fields that may have changed
            for col, val in normalized.items():
                if col in ("row_hash", "report_date", "report_adid"):
                    continue
                setattr(existing, col, val)
            summary["updated"] += 1
        else:
            permit = Permit(**normalized)
            session.add(permit)
            summary["inserted"] += 1

    session.commit()
    log.info(
        "PDD sync complete: %d inserted, %d updated, %d errors, %d skipped (in-chunk dedup)",
        summary["inserted"], summary["updated"], summary["errors"], summary["skipped"],
    )

    return summary


def _iterate_date_chunks(
    start: date, end: date, chunk_days: int = 180
):
    """Yield (chunk_start, chunk_end) date pairs covering [start, end)
    in non-overlapping chunks of up to chunk_days each."""
    current = start
    while current < end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        yield current, chunk_end
        current = chunk_end


def sync_pdd_all(
    session,
    limit: Optional[int] = None,
    dry_run: bool = False,
    start_date: Optional[date] = None,
) -> dict:
    """Run a full historical sync of PDD permits from start_date to today+30.

    Iterates 180-day chunks to avoid overloading the PDD export endpoint.
    Each chunk calls sync_pdd_permits() and passes limit along (for testing).

    Default start_date is 2004-01-01.

    Returns aggregate summary across all chunks.
    """
    if start_date is None:
        start_date = date(2004, 1, 1)
    end_date = date.today() + timedelta(days=30)

    log.info(
        "Running full PDD sync from %s to %s (limit=%s, dry_run=%s)",
        start_date, end_date, limit, dry_run,
    )

    aggregate = {"fetched": 0, "inserted": 0, "updated": 0, "errors": 0}
    chunks = 0
    remaining = limit

    for chunk_start, chunk_end in _iterate_date_chunks(start_date, end_date):
        chunk_limit = None
        if remaining is not None:
            chunk_limit = remaining

        print(f"  Chunk {chunks + 1}: {chunk_start} to {chunk_end}...", end=" ", flush=True)

        summary = sync_pdd_permits(
            session,
            chunk_start,
            chunk_end,
            limit=chunk_limit,
            dry_run=dry_run,
        )
        chunks += 1
        for k in aggregate:
            aggregate[k] += summary.get(k, 0)

        print(
            f"fetched={summary.get('fetched', 0)} "
            f"inserted={summary.get('inserted', 0)} "
            f"updated={summary.get('updated', 0)} "
            f"errors={summary.get('errors', 0)} "
            f"skipped={summary.get('skipped', 0)}",
            flush=True,
        )

        if remaining is not None:
            remaining -= summary.get("fetched", 0)
            if remaining <= 0:
                break

        # Rate-limit between chunks
        time.sleep(0.5)

    log.info(
        "Full PDD sync complete: %d chunks, %d fetched, %d inserted, %d updated, %d errors",
        chunks,
        aggregate["fetched"],
        aggregate["inserted"],
        aggregate["updated"],
        aggregate["errors"],
    )

    return aggregate


def inspect_pdd_source(limit: int = 10) -> None:
    """Fetch a short sample of PDD CSV data (1 week) and print the parsed rows."""
    end = date.today()
    start = end - timedelta(days=7)

    print(f"Fetching PDD CSV from {start} to {end}...")
    csv_text = _fetch_csv_content(start, end)
    csv_rows = _parse_csv_rows(csv_text, limit=limit)

    if not csv_rows:
        print("No records returned.")
        return

    print(f"Parsed {len(csv_rows)} data rows (limit={limit}):\n")

    # Print column headers from the raw CSV
    reader = csv.reader(io.StringIO(csv_text))
    all_rows = list(reader)
    if len(all_rows) >= 2:
        print("Metadata row:", all_rows[0])
        print("Header row:", all_rows[1])
        print()

    for i, row in enumerate(csv_rows):
        norm = _normalize_pdd_row(row)
        print(f"--- Row {i + 1} ---")
        for key, value in sorted(norm.items()):
            print(f"  {key}: {value}")
        print()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# ── Coordinate transformation ──────────────────────────────────────────────

def _web_mercator_to_wgs84(x: float, y: float):
    """Convert Web Mercator (EPSG:3857) coordinates to WGS84 (EPSG:4326) lat/lng.

    Args:
        x: Easting in Web Mercator meters
        y: Northing in Web Mercator meters

    Returns:
        (latitude, longitude) tuple in decimal degrees, or (None, None) on failure.
    """
    if x is None or y is None:
        return None, None

    try:
        import pyproj
        src_crs = pyproj.CRS("EPSG:3857")
        tgt_crs = pyproj.CRS("EPSG:4326")
        transformer = pyproj.Transformer.from_crs(src_crs, tgt_crs, always_xyz=True)
        lon, lat = transformer.transform(x, y)
        return lat, lon
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: direct Mercator formula
    try:
        x_f = float(x)
        y_f = float(y)
        lon = x_f * 180.0 / 20037508.34
        lat = (2.0 * math.atan(math.exp(y_f * math.pi / 20037508.34)) - math.pi / 2.0) * 180.0 / math.pi
        return round(lat, 6), round(lon, 6)
    except (ValueError, TypeError, OverflowError):
        return None, None


# ── Date parsing ────────────────────────────────────────────────────────────

_ARCGIS_MS_NUMERIC = re.compile(r"^(\d{13})")


def _parse_arcgis_date(value) -> Optional[str]:
    """Parse an ArcGIS date field value (epoch ms) into YYYY-MM-DD string.

    Handles:
    1.  ``1420553833000`` — raw 13-digit epoch millisecond integers
    2.  ``/Date(1711929600000)/`` — millisecond timestamp pattern
    3.  ISO date strings

    Returns None for null/empty/parse-failure.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("", "None", "null"):
        return None

    # Try raw 13-digit millisecond integer first
    m = _ARCGIS_MS_NUMERIC.match(s)
    if m:
        try:
            ts_ms = int(m.group(1))
            dt = datetime.utcfromtimestamp(ts_ms / 1000)
            if dt.year < 1970 or dt.year > 2100:
                return None
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None

    # Try /Date(milliseconds)/ pattern
    ms_match = re.match(r"/Date\((\d+)\)/", s)
    if ms_match:
        try:
            ts_ms = int(ms_match.group(1))
            return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return None

    # Try ISO date
    try:
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
    return_geometry: bool = True,
) -> str:
    """Build a MapServer query URL with pagination."""
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "resultOffset": str(offset),
        "resultRecordCount": str(count),
        "f": "json",
    }
    qs = "&".join(f"{k}={urllib.request.quote(v, safe='')}" for k, v in params.items())
    return f"{BASE_URL}/{LAYER_ID}/query?{qs}"


def fetch_page(
    offset: int = 0,
    count: int = MAX_RECORD_COUNT,
    where: str = "1=1",
    return_geometry: bool = True,
) -> dict:
    """Fetch one page of permit records from the ArcGIS MapServer.

    Returns the parsed JSON response dict.
    """
    url = _build_query_url(
        offset=offset,
        count=count,
        where=where,
        return_geometry=return_geometry,
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all(
    limit: Optional[int] = None,
) -> list[dict]:
    """Fetch all permit records from Phoenix ArcGIS, paginating through results.

    Returns a flat list of feature attribute dicts, each with an optional
    ``_geometry`` key holding the point geometry dict.

    Args:
        limit: Maximum total records to fetch.
    """
    all_records: list[dict] = []
    offset = 0

    while limit is None or len(all_records) < limit:
        batch_count = MAX_RECORD_COUNT
        if limit is not None:
            remaining = limit - len(all_records)
            batch_count = min(batch_count, remaining)

        data = fetch_page(offset=offset, count=batch_count, return_geometry=True)
        features = data.get("features", [])

        if not features:
            break

        for feat in features:
            attributes = feat.get("attributes", {})
            geometry = feat.get("geometry")
            if geometry is not None:
                attributes["_geometry"] = geometry
            all_records.append(attributes)

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

    # ── Core fields ──
    permit_number = arcgis_row.get("PER_NUM")
    if permit_number is not None:
        permit_number = str(permit_number).strip() or None

    source_record_id = str(arcgis_row.get("OBJECTID") or "")
    if not source_record_id:
        source_record_id = None

    # ── Permit type ──
    raw_permit_type = arcgis_row.get("PER_TYPE") or None
    raw_permit_type_desc = arcgis_row.get("PER_TYPE_DESC") or None
    permit_name = arcgis_row.get("PERMIT_NAME") or None
    scope_description = arcgis_row.get("SCOPE_DESC") or None
    mod_desc = arcgis_row.get("MOD_DESC") or None  # "Building", etc.

    # ── Status ──
    permit_status = arcgis_row.get("PERMIT_STAT") or None  # OPEN, etc.

    # ── Dates (epoch ms) ──
    permit_issue_date = _parse_arcgis_date(arcgis_row.get("PER_ISSUE_DATE"))
    applied_date = _parse_arcgis_date(arcgis_row.get("PER_ENT_DATE"))
    completed_date = _parse_arcgis_date(arcgis_row.get("PER_COMPL_DATE"))
    permit_expiration_date = _parse_arcgis_date(arcgis_row.get("PER_EXPIRE_DATE"))

    # ── Address ──
    job_address = arcgis_row.get("STREET_FULL_NAME") or None

    # ── Contractor ──
    contractor_name = arcgis_row.get("PROFESS_NAME") or None

    # ── Project name combining available text fields ──
    project_name = permit_name
    description = scope_description

    # ── Coordinates (Web Mercator → WGS84) ──
    geometry = arcgis_row.get("_geometry")
    latitude, longitude = None, None
    if geometry:
        x = geometry.get("x")
        y = geometry.get("y")
        if x is not None and y is not None:
            latitude, longitude = _web_mercator_to_wgs84(x, y)

    # ── Build normalized record ──
    record = {
        "permit_number": permit_number,
        "permit_description": description,
        "project_name": project_name,
        "permit_issue_date": permit_issue_date,
        "applied_date": applied_date,
        "completed_date": completed_date,
        "permit_expiration_date": permit_expiration_date,
        "permit_status": permit_status,
        "job_address": job_address,
        "raw_permit_type": raw_permit_type,
        "raw_permit_type_description": raw_permit_type_desc,
        "raw_permit_class": mod_desc,
        "contractor_name": contractor_name,
        "latitude": str(latitude) if latitude is not None else None,
        "longitude": str(longitude) if longitude is not None else None,
        "jurisdiction": "City of Phoenix",
        "source_system": SOURCE_SYSTEM,
        "source_record_id": source_record_id,
    }

    # ── Phoenix-specific classification using ArcGIS permit type / code ──
    phx_cat, phx_wt = categorize_phoenix_type(raw_permit_type)
    record["normalized_category"] = phx_cat or "Other"
    record["work_type"] = phx_wt or "Unknown"

    # ── Row hash for dedup ──
    hash_parts = [
        record.get("permit_number") or "",
        record.get("source_record_id") or "",
        record.get("permit_issue_date") or "",
        record.get("job_address") or "",
    ]
    record["row_hash"] = hashlib.sha256(
        "||".join(hash_parts).encode("utf-8")
    ).hexdigest()

    # ── Source tracking ──
    record["report_date"] = today_str
    record["report_adid"] = f"phoenix-arcgis-{date_str_for_adid}"
    record["source_file"] = "phoenix-arcgis-api"

    return record


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
            if key == "_geometry":
                print(f"  {key}: <present>")
                continue
            print(f"  {key}: {value}")
        print()

    # Print all unique field names across all records
    all_fields: set[str] = set()
    for row in records:
        for key in row:
            if key != "_geometry":
                all_fields.add(key)
    print(f"\nAll fields ({len(all_fields)}): {', '.join(sorted(all_fields))}")


# ── Database sync ───────────────────────────────────────────────────────────

def _get_db():
    import db as _db
    return _db


def sync_permits(
    session,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Fetch Phoenix permits from ArcGIS, normalize, and upsert into the database.

    Uses bulk operations for performance: pre-loads existing records into a
    lookup dict, then iterates to determine inserts vs updates.

    Returns a summary dict with keys: fetched, inserted, updated, errors.
    """
    log.info(
        "Fetching Phoenix permits (limit=%s, dry_run=%s)",
        limit, dry_run,
    )

    db_mod = _get_db()
    Permit = db_mod.Permit

    records = fetch_all(limit=limit)
    log.info("Fetched %d records from ArcGIS", len(records))

    if dry_run:
        log.info("Dry run: %d records would be processed", len(records))
        return {"fetched": len(records), "inserted": 0, "updated": 0, "errors": 0}

    from sqlalchemy import select

    # Pre-load all existing Phoenix permits into a lookup dict
    existing_rows = session.execute(
        select(Permit).where(Permit.source_system == SOURCE_SYSTEM)
    ).scalars().all()

    existing_map = {}
    for p in existing_rows:
        key = (p.source_system or "", str(p.source_record_id or ""))
        existing_map[key] = p

    log.info("Loaded %d existing Phoenix permits for dedup", len(existing_map))

    summary = {"fetched": len(records), "inserted": 0, "updated": 0, "errors": 0}

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
            log.warning(
                "Skipping row without source_system/source_record_id: %s",
                normalized.get("permit_number"),
            )
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
        "Sync complete: %d inserted, %d updated, %d errors",
        summary["inserted"], summary["updated"], summary["errors"],
    )

    return summary
