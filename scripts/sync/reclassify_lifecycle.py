#!/usr/bin/env python3
"""
reclassify_lifecycle.py — Re-run lifecycle classification on existing items.

Use after updating patterns in lifecycle.py.  Reclassifies all items
or a specific body.

Usage:
    # Reclassify all items
    python3 scripts/sync/reclassify_lifecycle.py

    # Reclassify a specific body
    python3 scripts/sync/reclassify_lifecycle.py --body phoenix-cc

    # Reclassify only items currently marked 'unknown'
    python3 scripts/sync/reclassify_lifecycle.py --only-unknown
"""

import sys, os, time, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "scraper"))

from db import get_engine
from scraper.common.lifecycle import classify_lifecycle
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reclassify-lifecycle")

BATCH = 1000

def main():
    engine = get_engine()
    body = None
    only_unknown = "--only-unknown" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--body" and i + 1 < len(sys.argv):
            body = sys.argv[i + 1]

    where_clauses = []
    if only_unknown:
        where_clauses.append("(lifecycle_status IS NULL OR lifecycle_status = '' OR lifecycle_status = 'unknown')")
    if body:
        where_clauses.append(f"body = '{body}'")

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    with engine.connect() as conn:
        total = conn.execute(text(
            f"SELECT COUNT(*) FROM agenda_items WHERE {where_sql}"
        )).scalar()
    log.info("Items to reclassify: %s", f"{total:,}")

    offset = 0
    classified = 0
    start = time.time()
    distribution = {}

    while offset < total:
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT id, agenda_item_text, agenda_item_title "
                f"FROM agenda_items WHERE {where_sql} ORDER BY id LIMIT {BATCH} OFFSET {offset}"
            )).fetchall()

        if not rows:
            break

        updates = []
        for row in rows:
            txt = (row[1] or "") + " " + (row[2] or "")
            status = classify_lifecycle(txt)
            updates.append((status, row[0]))
            distribution[status] = distribution.get(status, 0) + 1

        if updates:
            with engine.begin() as conn:
                case_parts = []
                for status, item_id in updates:
                    es = status.replace("'", "''")
                    case_parts.append(f"WHEN {item_id} THEN '{es}'")
                ids = ",".join(str(uid) for _, uid in updates)
                conn.execute(text(f"""
                    UPDATE agenda_items SET lifecycle_status = CASE id
                        {' '.join(case_parts)} ELSE lifecycle_status END
                    WHERE id IN ({ids})
                """))

        offset += BATCH
        classified += len(updates)
        elapsed = time.time() - start
        log.info("  %d/%d (%.0f/s)", classified, total, classified / elapsed if elapsed else 0)

    elapsed = time.time() - start
    log.info("DONE: %d items reclassified in %.0fs", classified, elapsed)
    log.info("Distribution: %s", dict(sorted(distribution.items(), key=lambda x: -x[1])[:10]))


if __name__ == "__main__":
    main()
