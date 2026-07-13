#!/usr/bin/env python3
"""Merge duplicate business entities (developer, law_firm, planning_firm, person, org)
caused by inconsistent &→and normalization.

Phase 1.3 normalization handled & inconsistently — some entities got
stripped ("gammage burnham") while others got replaced ("gammage and burnham").
"""

import sys
from pathlib import Path
from collections import defaultdict

_here = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_here))

from db.core import get_engine
from sqlalchemy import text

# Only merge these entity types — skip address/parcel/case noise
TARGET_TYPES = {"developer", "law_firm", "planning_firm", "person", "organization", "utility", "advocacy_group"}


def normalize(name: str) -> str:
    """Normalize a name for dedup comparison."""
    n = name.lower().strip()
    n = n.replace(" & ", " and ")
    n = n.replace(" &", " and ")
    for suffix in [", p.l.c.", ", plc", ", p.c.", ", l.l.c.", ", llc", ", l.p.",
                   " p.l.c.", " plc", " p.c.", " l.l.c.", " llc", " l.p.",
                   " company", " inc.", " inc", " corporation", " corp."]:
        n = n.replace(suffix, "")
    parts = n.split()
    return " ".join(parts)


def main():
    engine = get_engine()

    groups = defaultdict(list)
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT id, name, entity_type, mention_count FROM entities")
        ).fetchall()

    for r in rows:
        if r[2] in TARGET_TYPES:
            key = normalize(r[1])
            groups[key].append(r)

    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"Found {len(duplicates)} duplicate groups in [{', '.join(sorted(TARGET_TYPES))}]\n")

    total_merged = 0
    total_removed = 0

    with engine.begin() as conn:
        for key, group in sorted(duplicates.items()):
            group.sort(key=lambda x: (-x[3], x[0]))
            canonical = group[0]
            dupes = group[1:]

            names = ", ".join(f"{r[1]} (id={r[0]}, {r[3]} mentions, {r[2]})" for r in group)
            print(f"  [{key}]")
            print(f"    Canonical: {canonical[1]} (id={canonical[0]}, {canonical[3]} mentions)")
            for d in dupes:
                print(f"    Merging:   {d[1]} (id={d[0]}, {d[3]} mentions, {d[2]})", end="")
                sys.stdout.flush()

                cid = canonical[0]
                did = d[0]

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
                ).scalar()
                conn.execute(
                    text("UPDATE entities SET mention_count = :mc WHERE id = :cid"),
                    {"cid": cid, "mc": total_mentions},
                )

                # Delete duplicate entity
                conn.execute(text("DELETE FROM entities WHERE id = :did"), {"did": did})

                total_merged += 1
                total_removed += mentions_moved
                print(f" → {mentions_moved} mentions, {rels_from} out-rels, {rels_to} in-rels")

    print(f"\nDone. Merged {total_merged} duplicates, {total_removed} total mentions reassigned.")


if __name__ == "__main__":
    main()
