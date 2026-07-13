#!/usr/bin/env python3
"""
Phase 2 — Parcel (APN) and address extraction.

Extracts Assessor Parcel Numbers and street addresses from:
  - agenda item titles
  - supporting document text

Then creates entity records and links them to case entities.

Usage:
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract_parcels.py
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract_parcels.py --dry-run
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db.core import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("parcels")

# ── Maricopa County APN formats ──
# Primary: XXX-XX-XXX optionally followed by letter suffix
# Also: PAB-XXXX (plan amendment boundary refs)
# Also: 12-digit with letters (collapsed format)
APN_PATTERNS = [
    # Standard Maricopa APN: 123-45-678 or 123-45-678A
    re.compile(r"\b(\d{3})[-](\d{2})[-](\d{3}[A-Z]?)\b"),
    # In text: "APN 123-45-678" or "Parcel 123-45-678"
    re.compile(r"\b(?:APN|PARCEL)\s+(\d{3}[-]\d{2}[-]\d{3}[A-Z]?)\b", re.IGNORECASE),
    # PAB reference: PAB-0300
    re.compile(r"\bPAB[-](\d{4})\b"),
    # Collapsed 12+ digit format: 123456789A
    re.compile(r"\b(\d{11,12}[A-Z]?)\b(?![-])"),
]

# ── Address patterns (Arizona) ──
CITIES_AZ = [
    "phoenix", "mesa", "chandler", "glendale", "scottsdale", "gilbert",
    "tempe", "peoria", "surprise", "avondale", "goodyear", "buckeye",
    "el mirage", "litchfield park", "fountain hills", "paradise valley",
    "cave creek", "carefree", "tolleson", "youngtown", "wickenburg",
    "queen creek", "maricopa",
]
CITY_PATTERN = "|".join(c.replace(" ", "\\s+") for c in sorted(CITIES_AZ, key=len, reverse=True))

# Address patterns — prioritize simple street-address formats
ADDRESS_PATTERNS = [
    # "123 N Main St, City, AZ 85001"
    re.compile(
        r"\b(\d{1,5}\s+(?:N|S|E|W|NORTH|SOUTH|EAST|WEST)?\.?\s*"
        r"[A-Z][a-zA-Z]+"
        r"(?:\s+(?:ST|DR|AVE|RD|BLVD|LN|WAY|CIR|TER|PL|CT|PKWY|TRL|DRIVE|AVENUE|ROAD|BOULEVARD|STREET|LANE|CIRCLE|TERRACE|PLACE|COURT|PARKWAY|TRAIL))"
        r"\.?\s*,?\s*(?:" + CITY_PATTERN + r")\s*,?\s*(?:AZ|ARIZONA)?\s*,?\s*\d{5}(?:-\d{4})?\b)",
        re.IGNORECASE,
    ),
    # "2630 W. Rio Salado Parkway, Mesa, AZ 85201" — multi-word street name
    re.compile(
        r"\b(\d{1,5}\s+(?:N|S|E|W|NORTH|SOUTH|EAST|WEST)?\.?\s+"
        r"(?:[A-Z][a-zA-Z']+\.?\s+){1,3}"  # up to 3 street name words (atomic length)
        r"(?:ST|DR|AVE|RD|BLVD|LN|WAY|CIR|TER|PL|CT|PKWY|TRL|DRIVE|AVENUE|ROAD|BOULEVARD|STREET|LANE|CIRCLE|TERRACE|PLACE|COURT|PARKWAY|TRAIL)"
        r"\.?\s*,?\s*(?:" + CITY_PATTERN + r")\s*,?\s*(?:AZ|ARIZONA)?\s*,?\s*\d{5}(?:-\d{4})?\b)",
        re.IGNORECASE,
    ),
    # Simpler: "301 W Jefferson St" alone (no city) — lower confidence
    re.compile(
        r"\b(\d{1,5}\s+(?:N|S|E|W|NORTH|SOUTH|EAST|WEST)?\.?\s*"
        r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*"
        r"\s+(?:ST|DR|AVE|RD|BLVD|LN|WAY|CIR|TER|PL|CT|PKWY|TRL)\b)",
        re.IGNORECASE,
    ),
]

NOISE_ADDRESSES = re.compile(
    r"(RETURN|MAILING|EMAIL|FAX|PHONE|CELL|SIGNATURE|NOTARY|ACKNOWLEDGE|CAN|MAY|SHALL|WILL|BEEN|BEING|HAVE|HAS|HAD|THAT|THIS|THESE|THOSE|WHICH|WHERE|HEREBY|THEREFORE|WHEREAS)",
    re.IGNORECASE,
)

# Common government building addresses to exclude
GOVT_ADDRESSES = {addr.upper() for addr in [
    "301 W. Jefferson St",
    "301 W Jefferson St",
    "301 W. Jefferson Street",
    "2801 West Durango Street",
    "2801 W. Durango St",
    "2801 W. Durango Street",
    "5850 W. Glendale Ave",
    "5850 W. Glendale Avenue",
    "5850 West Glendale Avenue",
    "4041 N. Central Ave",
    "4041 N Central Ave",
    "4041 North Central Avenue",
    "405 W. 5th St",
    "200 W. Washington St",
    "200 W Washington St",
]}


def normalize_apn(apn: str) -> str:
    """Normalize APN to XXX-XX-XXX format."""
    apn = apn.strip().upper()
    # Remove APN/PARCEL prefix if present
    apn = re.sub(r"^(?:APN|PARCEL)\s+", "", apn, flags=re.IGNORECASE)
    # If already in XXX-XX-XXX format, just return
    if re.match(r"^\d{3}-\d{2}-\d{3}[A-Z]?$", apn):
        return apn
    # If it's PAB-NNNN, keep as-is
    if re.match(r"^PAB-\d{4}$", apn):
        return apn
    # If it's a collapsed format (12+ digits), try to parse
    digits = re.sub(r"[^\dA-Z]", "", apn)
    if len(digits) >= 11:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return apn


def extract_apns(text: str) -> list[str]:
    """Extract all APN strings from text."""
    if not text:
        return []
    found: list[str] = []
    seen = set()

    for pat in APN_PATTERNS:
        for m in pat.finditer(text):
            apn = normalize_apn(m.group(0))
            if apn not in seen:
                seen.add(apn)
                found.append(apn)

    return found


def extract_addresses(text: str) -> list[str]:
    """Extract street address strings from text.
    Only returns addresses that include a city name.
    """
    if not text or len(text) < 20:
        return []
    found: list[str] = []
    seen = set()

    # Only use patterns that include city context
    for pat in ADDRESS_PATTERNS[:2]:  # first two patterns require city
        for m in pat.finditer(text):
            addr = m.group(0).strip()
            if NOISE_ADDRESSES.search(addr):
                continue
            if addr.upper().strip(". ") in GOVT_ADDRESSES:
                continue
            norm = addr.upper().strip().rstrip(".")
            if norm not in seen:
                seen.add(norm)
                found.append(norm)

    return found


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parcel & address extraction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--items-batch", type=int, default=1000)
    args = parser.parse_args()

    engine = get_engine()

    # ── Step 1: Scan agenda item titles ──
    with engine.connect() as c:
        items = c.execute(
            text("SELECT id, agenda_item_title FROM agenda_items ORDER BY id")
        ).fetchall()

    log.info("Scanning %d agenda item titles...", len(items))
    apn_counts: dict[str, int] = {}
    addr_counts: dict[str, int] = {}
    total_apn_items = 0
    total_addr_items = 0

    limit = args.limit or len(items)
    for idx, item in enumerate(items):
        if idx >= limit:
            break
        item_id = item[0]
        title = item[1] or ""

        apns = extract_apns(title)
        if apns:
            total_apn_items += 1
            for a in apns:
                apn_counts[a] = apn_counts.get(a, 0) + 1

        addrs = extract_addresses(title)
        if addrs:
            total_addr_items += 1
            for a in addrs:
                addr_counts[a] = addr_counts.get(a, 0) + 1

    log.info("Titles: %d items with APNs (%d unique), %d items with addresses (%d unique)",
             total_apn_items, len(apn_counts), total_addr_items, len(addr_counts))

    # ── Step 2: Scan supporting document text ──
    with engine.connect() as c:
        total_docs = c.execute(
            text("SELECT COUNT(*) FROM supporting_documents WHERE text_content IS NOT NULL")
        ).scalar()

    log.info("Scanning %d supporting document texts (batches of %d)...",
             total_docs, args.items_batch)

    scan_start = time.time()
    apn_doc_counts: dict[str, int] = {}
    addr_doc_counts: dict[str, int] = {}
    total_apn_docs = 0
    total_addr_docs = 0
    scanned_docs = 0

    offset = 0
    while offset < total_docs:
        with engine.connect() as c:
            batch = c.execute(
                text("""
                    SELECT id, agenda_item_id, LEFT(text_content, 3000) AS text_content
                    FROM supporting_documents
                    WHERE text_content IS NOT NULL
                    ORDER BY id
                    LIMIT :limit OFFSET :offset
                """),
                {"limit": args.items_batch, "offset": offset},
            ).fetchall()

        if not batch:
            break

        for doc in batch:
            doc_id, item_id = doc[0], doc[1]
            text_content = doc[2] or ""

            if len(text_content) < 20:
                scanned_docs += 1
                continue

            try:
                apns = extract_apns(text_content)
                if apns:
                    total_apn_docs += 1
                    for a in apns:
                        apn_doc_counts[a] = apn_doc_counts.get(a, 0) + 1
            except Exception as e:
                log.warning("  APN extraction error for doc %d: %s", doc_id, e)

            try:
                addrs = extract_addresses(text_content)
                if addrs:
                    total_addr_docs += 1
                    for a in addrs:
                        addr_doc_counts[a] = addr_doc_counts.get(a, 0) + 1
            except Exception as e:
                log.warning("  Address extraction error for doc %d: %s", doc_id, e)

            scanned_docs += 1

        offset += args.items_batch
        elapsed = time.time() - scan_start
        log.info("  scanned %d / %d docs (%d APN docs, %d addr docs) in %.1fs",
                 scanned_docs, total_docs, total_apn_docs, total_addr_docs, elapsed)

    # ── Merge counts ──
    all_apns: dict[str, int] = {}
    for apn, count in apn_counts.items():
        all_apns[apn] = count
    for apn, count in apn_doc_counts.items():
        all_apns[apn] = all_apns.get(apn, 0) + count

    all_addrs: dict[str, int] = {}
    for addr, count in addr_counts.items():
        all_addrs[addr] = count
    for addr, count in addr_doc_counts.items():
        all_addrs[addr] = all_addrs.get(addr, 0) + count

    # ── Report ──
    log.info("")
    log.info("╔══════════════════════════════════════╗")
    log.info("║  Phase 2 — Extraction Summary       ║")
    log.info("╠══════════════════════════════════════╣")
    log.info("║  Unique APNs:     %-17d ║", len(all_apns))
    log.info("║  Unique Addresses: %-17d ║", len(all_addrs))
    log.info("║  APN in titles:   %-17d ║", total_apn_items)
    log.info("║  APN in docs:     %-17d ║", total_apn_docs)
    log.info("║  Addr in titles:  %-17d ║", total_addr_items)
    log.info("║  Addr in docs:    %-17d ║", total_addr_docs)
    log.info("╚══════════════════════════════════════╝")

    # Top APNs
    top_apns = sorted(all_apns.items(), key=lambda x: -x[1])[:15]
    log.info("── Top 15 APNs ──")
    for apn, count in top_apns:
        log.info("  %-18s  %d mentions", apn, count)

    # Top addresses
    top_addrs = sorted(all_addrs.items(), key=lambda x: -x[1])[:15]
    log.info("── Top 15 addresses ──")
    for addr, count in top_addrs:
        log.info("  %-50s  %d", addr[:50], count)

    # ── Persist to entity tables ──
    if not args.dry_run:
        with engine.connect() as c:
            existing_apns = {
                r[0] for r in c.execute(
                    text("SELECT normalized_name FROM entities WHERE entity_type = 'parcel'")
                ).fetchall()
            }
            existing_addrs = {
                r[0] for r in c.execute(
                    text("SELECT normalized_name FROM entities WHERE entity_type = 'address'")
                ).fetchall()
            }

        # Persist APNs
        new_apns = [a for a in all_apns if a not in existing_apns]
        log.info("Persisting %d new parcel entities...", len(new_apns))
        persisted = 0
        for apn, count in all_apns.items():
            with engine.begin() as c:
                try:
                    c.execute(
                        text("""
                            INSERT INTO entities
                                (entity_type, name, normalized_name, is_government,
                                 first_seen_at, last_seen_at, mention_count,
                                 created_at, updated_at)
                            VALUES ('parcel', :name, :norm, false,
                                    NOW(), NOW(), :count, NOW(), NOW())
                            ON CONFLICT (normalized_name) DO UPDATE
                                SET mention_count = :count2,
                                    last_seen_at = NOW()
                        """),
                        {"name": apn, "norm": normalize_apn(apn),
                         "count": count, "count2": count},
                    )
                    persisted += 1
                except Exception as e:
                    log.warning("  Failed to persist APN %s: %s", apn, e)
        log.info("Persisted %d parcel entities", persisted)

        # Persist addresses
        new_addrs = [a for a in all_addrs if a not in existing_addrs]
        log.info("Persisting %d new address entities...", len(new_addrs))
        persisted = 0
        for addr, count in all_addrs.items():
            with engine.begin() as c:
                try:
                    c.execute(
                        text("""
                            INSERT INTO entities
                                (entity_type, name, normalized_name, is_government,
                                 first_seen_at, last_seen_at, mention_count,
                                 created_at, updated_at)
                            VALUES ('address', :name, :norm, false,
                                    NOW(), NOW(), :count, NOW(), NOW())
                            ON CONFLICT (normalized_name) DO UPDATE
                                SET mention_count = :count2,
                                    last_seen_at = NOW()
                        """),
                        {"name": addr, "norm": addr.upper().strip().rstrip("."),
                         "count": count, "count2": count},
                    )
                    persisted += 1
                except Exception as e:
                    log.warning("  Failed to persist address %s: %s", addr, e)
        log.info("Persisted %d address entities", persisted)

    # Final tally
    with engine.connect() as c:
        total_apn = c.execute(
            text("SELECT COUNT(*) FROM entities WHERE entity_type = 'parcel'")
        ).scalar()
        total_addr = c.execute(
            text("SELECT COUNT(*) FROM entities WHERE entity_type = 'address'")
        ).scalar()
    log.info("Final entity counts: %d parcels, %d addresses", total_apn, total_addr)


if __name__ == "__main__":
    main()
