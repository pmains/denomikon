#!/usr/bin/env python3
"""
backfill_agenda_text.py — Backfill empty agenda_item_text from supporting_documents.

One-shot script: For specified bodies, copies text_content from linked
supporting_documents into agenda_item_text where it's currently empty.

Usage:
    PYTHONPATH=scripts python3 scripts/sync/backfill_agenda_text.py
    PYTHONPATH=scripts python3 scripts/sync/backfill_agenda_text.py --dry-run
    PYTHONPATH=scripts python3 scripts/sync/backfill_agenda_text.py --body glendale-cc
"""

import argparse
import logging
import sys
import time

sys.path.insert(0, "scripts")
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("backfill_agenda_text")

# Bodies where we know supporting_docs exist with text_content
# Map source_body → known document_type to prefer
BACKFILL_TARGETS = {
    "glendale-cc": None,        # Any doc type
}

BATCH_SIZE = 500


def count_pending(engine, body: str | None = None) -> int:
    """Count empty agenda items with linked docs that have text_content."""
    conditions = "ai.agenda_item_id IS NOT NULL AND ai.agenda_item_id != ''"
    if body:
        conditions += f" AND ai.source_body = '{body}'"

    with engine.connect() as c:
        return c.execute(text(f"""
            SELECT COUNT(DISTINCT ai.id)::int
            FROM agenda_items ai
            JOIN supporting_documents sd
                ON sd.agenda_item_id = ai.agenda_item_id
            WHERE (ai.agenda_item_text IS NULL OR ai.agenda_item_text = '')
              AND sd.text_content IS NOT NULL AND sd.text_content != ''
              AND {conditions}
        """)).scalar()


def backfill(engine, body: str | None = None, dry_run: bool = False) -> dict:
    """Copy supporting_doc text_content into empty agenda_item_text."""
    conditions = "ai.agenda_item_id IS NOT NULL AND ai.agenda_item_id != ''"
    if body:
        conditions += f" AND ai.source_body = '{body}'"

    stats = {"backfilled": 0, "errors": 0, "skipped_no_text": 0}

    # For Glendale, use the longest supporting_doc text per agenda item
    # (meeting results are short; staff reports have more detail)
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT DISTINCT ON (ai.id)
                ai.id AS agenda_item_id,
                sd.text_content
            FROM agenda_items ai
            JOIN supporting_documents sd
                ON sd.agenda_item_id = ai.agenda_item_id
            WHERE (ai.agenda_item_text IS NULL OR ai.agenda_item_text = '')
              AND sd.text_content IS NOT NULL AND sd.text_content != ''
              AND {conditions}
            ORDER BY ai.id, LENGTH(sd.text_content) DESC
        """)).fetchall()

    if not rows:
        log.info("No agenda items to backfill.")
        return stats

    log.info("Found %d agenda items to backfill.", len(rows))

    if dry_run:
        total_chars = sum(len(str(r[1] or "")) for r in rows)
        log.info(
            "DRY RUN — would backfill %d items with %d total chars.",
            len(rows), total_chars,
        )
        return stats

    # Update in batches
    batch_updates = []
    for row in rows:
        batch_updates.append({"id": int(row[0]), "text": str(row[1])})

    updated = 0
    for i in range(0, len(batch_updates), BATCH_SIZE):
        batch = batch_updates[i:i + BATCH_SIZE]
        with engine.begin() as c:
            for item in batch:
                result = c.execute(
                    text(
                        "UPDATE agenda_items SET agenda_item_text = :text "
                        "WHERE id = :id "
                        "AND (agenda_item_text IS NULL OR agenda_item_text = '')"
                    ),
                    {"id": item["id"], "text": item["text"]},
                )
                if result.rowcount > 0:
                    updated += 1

        if (i // BATCH_SIZE) % 5 == 0:
            log.info("  Progress: %d / %d", i + len(batch), len(batch_updates))

    stats["backfilled"] = updated
    total_chars = sum(len(item["text"]) for item in batch_updates)
    log.info(
        "Backfilled %d agenda items (%d total chars). %d had errors.",
        updated, total_chars, stats["errors"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill agenda_item_text from supporting_documents")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pending work without writing")
    parser.add_argument("--body", type=str, default=None,
                        help="Only backfill this body (e.g., glendale-cc)")
    parser.add_argument("--all", action="store_true",
                        help="Backfill all bodies in BACKFILL_TARGETS")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    engine = get_engine()

    # Count pending
    if args.all:
        bodies = list(BACKFILL_TARGETS.keys())
    elif args.body:
        bodies = [args.body]
    else:
        bodies = [None]  # All

    total_pending = 0
    for b in bodies:
        pending = count_pending(engine, body=b)
        label = b or "all bodies"
        log.info("Pending for %s: %d items", label, pending)
        total_pending += pending

    if total_pending == 0:
        log.info("Nothing to backfill.")
        return

    # Backfill
    for b in bodies:
        log.info("Backfilling %s...", b or "all bodies")
        backfill(engine, body=b, dry_run=args.dry_run)

    log.info("Done.")


if __name__ == "__main__":
    main()
