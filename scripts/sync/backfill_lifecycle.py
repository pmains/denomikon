#!/usr/bin/env python3
"""
backfill_lifecycle.py — Backfill lifecycle_status for all existing agenda items.

Classifies every agenda item's lifecycle status based on its text,
then stores the result in the new `lifecycle_status` column.

Usage:
    python3 scripts/sync/backfill_lifecycle.py
"""

import sys, os, time, logging

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "scraper"))

from db import get_engine
from db.models import AgendaItem
from scraper.common.lifecycle import classify_lifecycle
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text, inspect as sa_inspect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill-lifecycle")

BATCH_SIZE = 500


def main():
    log.info("Connecting to DB and ensuring lifecycle_status column...")
    engine = get_engine()

    # Add column if not exists (skip full init_db which is slow)
    inspector = sa_inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("agenda_items")}
    if "lifecycle_status" not in cols:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE agenda_items ADD COLUMN lifecycle_status VARCHAR(32) DEFAULT NULL"
            ))
            conn.commit()
        log.info("Added lifecycle_status column")
    else:
        log.info("lifecycle_status column already exists")

    Session = sessionmaker(bind=engine)
    session = Session()

    # Count items that need classification
    total = session.execute(text(
        "SELECT COUNT(*) FROM agenda_items "
        "WHERE (lifecycle_status IS NULL OR lifecycle_status = '')"
    )).scalar()
    log.info("Items needing classification: %d", total)

    offset = 0
    classified = 0
    errors = 0
    start_ts = time.time()
    distribution: dict[str, int] = {}

    while offset < total:
        items = session.query(AgendaItem).filter(
            (AgendaItem.lifecycle_status.is_(None)) | (AgendaItem.lifecycle_status == "")
        ).order_by(AgendaItem.id).limit(BATCH_SIZE).offset(offset).all()

        if not items:
            break

        for item in items:
            try:
                text_to_classify = (item.agenda_item_text or "") + " " + (item.agenda_item_title or "")
                status = classify_lifecycle(text_to_classify)
                item.lifecycle_status = status
                distribution[status] = distribution.get(status, 0) + 1
                classified += 1
            except Exception as e:
                errors += 1
                if errors <= 5:
                    log.warning("Error on item %d: %s", item.id, e)

        session.commit()
        offset += BATCH_SIZE
        elapsed = time.time() - start_ts
        rate = classified / elapsed if elapsed > 0 else 0
        log.info(
            "  %d/%d classified (%.0f/s, %d errors)",
            min(offset, total), total, rate, errors,
        )

    elapsed = time.time() - start_ts
    log.info("DONE: %d items classified in %.0fs with %d errors", classified, elapsed, errors)
    log.info("Distribution: %s", dict(sorted(distribution.items(), key=lambda x: -x[1])))

    session.close()


if __name__ == "__main__":
    main()
