#!/usr/bin/env python3
"""
Merge duplicate address entities caused by inconsistent formatting.

Addresses are extracted from agenda item titles and supporting documents
via regex. The same physical address can appear as:
  - "10000 N EL MIRAGE RD"
  - "10000 N. EL MIRAGE RD."
  - "10000 NORTH EL MIRAGE ROAD"
  - "10000 N EL MIRAGE ROAD EL MIRAGE, AZ 85335"
  - "10000 N EL MIRAGE RD, EL MIRAGE, AZ 85335-3607"
  - "10000 N. EL MIRAGE ROAD, EL MIRAGE, ARIZONA 85335"
  - "10000 N    EL MIRAGE RD\nEL MIRAGE, AZ 85335"  (whitespace/linebreak chaos)
  - "01 E LINCOLN DR"  vs  "1 E LINCOLN DR"  (leading zeros)

This normalizer strips all these variations down to a canonical key and
merges duplicate address entities into the one with the most mentions.

Usage:
    .venv/bin/python scripts/dedup_addresses.py
    .venv/bin/python scripts/dedup_addresses.py --dry-run
    .venv/bin/python scripts/dedup_addresses.py --limit=500
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

_here = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_here))

from db.core import get_engine
from sqlalchemy import text

# ── Street type mapping (full → standard abbreviation) ──
_STREET_TYPES = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "DRIVE": "DR",
    "ROAD": "RD",
    "BOULEVARD": "BLVD",
    "LANE": "LN",
    "CIRCLE": "CIR",
    "COURT": "CT",
    "PLACE": "PL",
    "PARKWAY": "PKWY",
    "TRAIL": "TRL",
    "TERRACE": "TER",
    "WAY": "WAY",  # already short
    "HIGHWAY": "HWY",
    "ROUTE": "RTE",
    "FREEWAY": "FWY",
}

# ── Directional mapping ──
_DIRECTIONALS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}


def normalize_address(raw: str) -> str:
    """Normalize an address string to a canonical form for dedup comparison.

    Steps:
      1. Replace non-breaking spaces with regular space
      2. Uppercase
      3. Strip leading zeros from house numbers
      4. Abbreviate directionals (NORTH → N)
      5. Abbreviate street types (STREET → ST)
      6. Strip trailing periods from abbreviations (N. → N, ST. → ST)
      7. Normalize state name (ARIZONA → AZ)
      8. Strip ZIP+4 extension (85335-3607 → 85335)
      9. Normalize comma placement
     10. Collapse all whitespace to single space
     11. Strip leading/trailing whitespace
    """
    n = raw.strip()
    if not n:
        return ""

    # 1. Replace NBSP and other unusual whitespace
    n = n.replace("\u00a0", " ").replace("\u200b", "").replace("\u2009", " ")
    n = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", n)

    # 2. Uppercase
    n = n.upper()

    # 3. Strip leading zeros from house numbers (at start of string)
    n = re.sub(r"^0+(\d)", r"\1", n)
    # Also after directional: "N 01" → "N 1"
    n = re.sub(r"\b([NSEW])\s+0+(\d)", r"\1 \2", n)

    # 4. Abbreviate directionals — full word → letter
    for full, abbr in sorted(_DIRECTIONALS.items(), key=lambda x: -len(x[0])):
        # Word boundary on both sides, handle trailing period
        n = re.sub(r"\b" + full + r"\b", abbr, n)

    # 5. Abbreviate street types — full word → abbr
    for full, abbr in sorted(_STREET_TYPES.items(), key=lambda x: -len(x[0])):
        n = re.sub(r"\b" + full + r"\b", abbr, n)

    # 6. Strip trailing periods from all abbreviations (N. → N, W. → W, ST. → ST, AVE. → AVE)
    n = re.sub(r"\b([A-Z])\.", r"\1", n)    # single-char directionals: N., S., E., W.
    n = re.sub(r"\b([A-Z]{2,})\.", r"\1", n)  # multi-char: ST., AVE., RD., BLVD.

    # 7a. State name normalization
    n = re.sub(r"\bARIZONA\b", "AZ", n)

    # 7b. Strip ZIP+4 extension
    n = re.sub(r"\b(\d{5})-\d{4}\b", r"\1", n)

    # 8. Normalize commas — ensure there's a comma between street and city
    #    "RD EL MIRAGE" → "RD, EL MIRAGE" if followed by a known city
    #    (This is tricky — instead, just ensure consistent comma placement)
    #    Strategy: strip commas, then re-add between all major segments
    n = n.replace(",", " ")
    n = re.sub(r"\s+", " ", n).strip()

    # 9. Tokenize and rejoin, stripping known noise
    tokens = n.split()
    # Remove standalone single-letter tokens that aren't directionals (likely noise)
    # e.g. "A" as a stray character
    cleaned = []
    for tok in tokens:
        if len(tok) == 1 and tok not in ("N", "S", "E", "W", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
            continue
        cleaned.append(tok)

    return " ".join(cleaned)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Merge duplicate address entities")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be merged without doing it")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N duplicate groups (for testing)")
    args = parser.parse_args()

    engine = get_engine()

    # Fetch all address entities
    groups = defaultdict(list)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, normalized_name, mention_count FROM entities WHERE entity_type = 'address'")
        ).fetchall()

    for r in rows:
        key = normalize_address(r[1])
        groups[key].append((r[0], r[1], r[2], r[3]))  # (id, name, normalized_name, mention_count)

    # Find duplicates (keys with >1 entity)
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    # Sort by group size descending
    sorted_groups = sorted(duplicates.items(), key=lambda x: -len(x[1]))

    if args.limit:
        sorted_groups = sorted_groups[:args.limit]

    total_duplicate_groups = len(duplicates)
    total_duplicate_rows = sum(len(v) for v in duplicates.values())
    total_merged = 0
    total_removed = 0

    print(f"Address entities found: {len(rows)}")
    print(f"Duplicate groups: {total_duplicate_groups}")
    print(f"Duplicate rows to merge: {total_duplicate_rows}")
    print(f"Unique addresses after cleanup: {len(groups) - total_duplicate_groups}")
    if args.dry_run:
        print("\n⚠️  DRY RUN — no changes will be made\n")
    print()

    with engine.begin() as conn:
        for key, group in sorted_groups:
            # Sort: most mentions first, then by id (older wins tie)
            group.sort(key=lambda x: (-x[3], x[0]))
            canonical = group[0]
            dupes = group[1:]

            cid, cname, cnorm, ccount = canonical

            print(f"  [{len(group)} variants] {key}")
            print(f"    Keep:  {cname} (id={cid}, {ccount} mentions)")

            for did, dname, dnorm, dcount in dupes:
                print(f"    Merge: {dname} (id={did}, {dcount} mentions)", end="")
                if args.dry_run:
                    print()
                    continue

                sys.stdout.flush()

                # Reassign entity_mentions
                result = conn.execute(
                    text("UPDATE entity_mentions SET entity_id = :cid WHERE entity_id = :did"),
                    {"cid": cid, "did": did},
                )
                mentions_moved = result.rowcount

                # Reassign entity_relationships (from_entity_id) — skip conflicts
                conn.execute(
                    text("""
                        DELETE FROM entity_relationships
                        WHERE from_entity_id = :did
                        AND (to_entity_id, relationship) IN (
                            SELECT to_entity_id, relationship
                            FROM entity_relationships
                            WHERE from_entity_id = :cid
                        )
                    """),
                    {"cid": cid, "did": did},
                )
                result = conn.execute(
                    text("UPDATE entity_relationships SET from_entity_id = :cid WHERE from_entity_id = :did"),
                    {"cid": cid, "did": did},
                )
                rels_from = result.rowcount

                # Reassign entity_relationships (to_entity_id)
                conn.execute(
                    text("""
                        DELETE FROM entity_relationships
                        WHERE to_entity_id = :did
                        AND (from_entity_id, relationship) IN (
                            SELECT from_entity_id, relationship
                            FROM entity_relationships
                            WHERE to_entity_id = :cid
                        )
                    """),
                    {"cid": cid, "did": did},
                )
                result = conn.execute(
                    text("UPDATE entity_relationships SET to_entity_id = :cid WHERE to_entity_id = :did"),
                    {"cid": cid, "did": did},
                )
                rels_to = result.rowcount

                # Self-referential cleanup
                conn.execute(
                    text("DELETE FROM entity_relationships WHERE from_entity_id = :cid AND to_entity_id = :cid"),
                    {"cid": cid},
                )

                # Update mention_count on canonical
                total_mentions = conn.execute(
                    text("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = :cid"),
                    {"cid": cid},
                ).scalar() or 0
                conn.execute(
                    text("UPDATE entities SET mention_count = :mc WHERE id = :cid"),
                    {"cid": cid, "mc": total_mentions},
                )

                # Update normalized_name to the canonical key if the canonical
                # entity doesn't already have a useful one
                if not cnorm or cnorm == cname.lower():
                    conn.execute(
                        text("UPDATE entities SET normalized_name = :key WHERE id = :cid"),
                        {"cid": cid, "key": key.lower()},
                    )

                # Delete duplicate entity
                conn.execute(text("DELETE FROM entities WHERE id = :did"), {"did": did})

                total_merged += 1
                total_removed += mentions_moved
                print(f" → {mentions_moved} mentions, {rels_from} out-rels, {rels_to} in-rels")

            print()

    if not args.dry_run:
        print(f"\nDone. Merged {total_merged} duplicates, {total_removed} total mentions reassigned.")
    else:
        print(f"\nDry run complete. Would merge {total_merged} duplicates (shown above).")
        print(f"Total: {total_duplicate_groups} groups, {total_duplicate_rows} rows.")


if __name__ == "__main__":
    main()
