#!/usr/bin/env python3
"""
Rescrape Buckeye 2026 supporting documents via Granicus pipeline.

Re-uses existing meetings in the DB and extracts supporting documents
(hyperlinked item report PDFs + attachments) from each meeting's agenda
PDF on Granicus.

Usage:
  nohup python3 -u scripts/ops/rescrape_buckeye_2026_supporting_docs.py \
    > data/rescrape-buckeye-2026-sd-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import logging
import sys
import time

sys.path.insert(0, "scripts")

from db import get_session, init_db, replace_meeting_data_safe
from db import Meeting as MeetingModel
from scraper.jurisdictions.buckeye_granicus import extract_supporting_docs, SOURCE_SYSTEM
from sqlalchemy import select

log = logging.getLogger("rescrape_buckeye_sd")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

YEAR = 2026


def main():
    log.info("=== Rescrape Buckeye %d supporting docs (Granicus) ===", YEAR)
    t_start = time.time()

    init_db()
    session = get_session()

    # Load all Buckeye 2026 meetings that have an agenda URL (source_url)
    meetings = session.execute(
        select(MeetingModel).where(
            MeetingModel.body.like("buckeye%"),
            MeetingModel.meeting_date >= f"{YEAR}-01-01",
            MeetingModel.meeting_date <= f"{YEAR}-12-31",
        )
    ).scalars().all()

    # Filter to those with source_url (needed for supporting doc extraction)
    meetings_with_url = [m for m in meetings if m.source_url]
    log.info(
        "Loaded %d Buckeye %d meetings (%d with agenda URLs)",
        len(meetings), YEAR, len(meetings_with_url),
    )

    total_docs = 0
    meetings_with_docs = 0
    failed = 0
    skipped = 0

    for idx, m in enumerate(meetings_with_url, 1):
        body_code = m.body
        meeting_id = m.meeting_id
        meeting_date = m.meeting_date
        source_url = m.source_url

        # Check if supporting docs already extracted
        if m.supporting_docs_extracted and m.supporting_doc_count > 0:
            log.info(
                "[%d/%d] %s %s %s: already has %d supporting docs — skip",
                idx, len(meetings_with_url), meeting_id, meeting_date, body_code,
                m.supporting_doc_count,
            )
            skipped += 1
            continue

        log.info(
            "[%d/%d] %s %s %s — extracting supporting docs...",
            idx, len(meetings_with_url), meeting_id, meeting_date, body_code,
        )

        try:
            t1 = time.time()
            docs = extract_supporting_docs(source_url)
            elapsed = time.time() - t1
            log.info(
                "  -> %d supporting doc(s) in %.1fs",
                len(docs), elapsed,
            )

            if docs:
                # Also note which item number each doc belongs to
                for doc in docs[:3]:
                    log.info("     [item %s] %s", doc.get("agenda_item_number", "?"), doc.get("document_title", "")[:80])
                if len(docs) > 3:
                    log.info("     ... and %d more", len(docs) - 3)

            # Build the meeting dict (needed by replace_meeting_data_safe)
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": m.meeting_type,
                "meeting_title": m.meeting_title,
                "source_url": source_url,
                "source_system": SOURCE_SYSTEM,
            }

            # Re-set existing items with the supporting docs attached
            existing_items = []
            for item in m.agenda_items:
                existing_items.append({
                    "agenda_item_id": f"{body_code}-{meeting_id}_{item.agenda_item_number}",
                    "meeting_id": meeting_id,
                    "agenda_item_number": item.agenda_item_number,
                    "agenda_item_title": item.agenda_item_title,
                    "agenda_item_text": item.agenda_item_text or "",
                    "source_body": body_code,
                    "source_url": source_url,
                    "c_number": item.c_number or "",
                    "c_number_base": item.c_number_base or "",
                    "case_number": item.case_number or "",
                })

            replace_meeting_data_safe(
                session, body_code, meeting_id, meeting_dict,
                existing_items,
                supporting_doc_dicts=docs,
            )
            total_docs += len(docs)
            meetings_with_docs += 1

        except Exception as e:
            failed += 1
            log.error("  -> FAILED: %s", e, exc_info=True)

    elapsed = time.time() - t_start
    log.info("=" * 60)
    log.info("Complete")
    log.info("  Meetings processed: %d", len(meetings_with_url))
    log.info("  Meetings with docs: %d", meetings_with_docs)
    log.info("  Total docs extracted: %d", total_docs)
    log.info("  Skipped (already had docs): %d", skipped)
    log.info("  Failed: %d", failed)
    log.info("  Elapsed: %.0fs (%.1f min)", elapsed, elapsed / 60)
    log.info("=" * 60)

    session.close()


if __name__ == "__main__":
    main()
