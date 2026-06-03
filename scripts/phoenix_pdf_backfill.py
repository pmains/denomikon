#!/usr/bin/env python3
"""Full backfill of Phoenix meeting agenda items from PDF archives.

Processes all available formal and policy session meetings from 2021 onward,
extracting full item descriptions from the official PDF agendas.

Usage:
    POLISCOPIC_DB_TIER=development PYTHONPATH=scripts python scripts/phoenix_pdf_backfill.py

Logs progress for each meeting. Run time: ~20-30 minutes for 75+ PDFs.
"""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("phoenix_backfill")

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

from db import get_session, init_db
from db.models import AgendaItem, Meeting
from scraper.phoenix_pdf import (
    fetch_pdf_bytes, extract_pdf_text, parse_agenda_items,
    fetch_all_meetings, COUNCIL_TYPES, BODY_CODE,
)
from sqlalchemy import select


def main():
    init_db()
    session = get_session()

    # Get all available meetings from the PDF JSON API
    log.info("Fetching meeting list from Phoenix JSON API...")
    all_json = fetch_all_meetings()
    council_meetings = [
        m for m in all_json
        if m["meeting_type"] in COUNCIL_TYPES
        and m["meeting_date"] >= "2021-01-01"
        and m.get("agenda_url")
    ]
    log.info("Found %d council meetings with PDFs since 2021", len(council_meetings))

    total_created = 0
    total_enriched = 0
    skipped_no_match = 0
    skipped_already = 0
    skipped_no_pdf = 0

    for idx, jm in enumerate(council_meetings, 1):
        date_str = jm["meeting_date"]
        mtype = jm["meeting_type"]
        url = jm["agenda_url"]
        log.info("[%d/%d] %s %s — checking...", idx, len(council_meetings), date_str, mtype[:20])

        # Find matching meeting in DB by date — try multiple type formats
        db_meeting = session.execute(
            select(Meeting).where(
                Meeting.body.like("phoenix%"),
                Meeting.meeting_date == date_str,
            )
        ).scalar_one_or_none()

        if not db_meeting:
            log.info("  No matching DB meeting found by date — creating new record")
            # Create a new meeting record from the PDF API data
            from db.models import Base
            from db.core import get_engine
            from sqlalchemy import insert as sql_insert
            
            # Build meeting_id from date
            new_id = f"phoenix-pdf-{date_str}"
            body_code = BODY_CODE
            
            from db.models import Base
            db_meeting = Meeting(
                body=body_code,
                meeting_id=new_id,
                meeting_date=date_str,
                meeting_type=mtype,
                meeting_title=mtype,
                source_url=url,
                source_system="phoenix_pdf",
                sync_status="complete",
                item_count_actual=0,
                supporting_doc_count=0,
                items_extracted=False,
                supporting_docs_extracted=False,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                retry_count=0,
            )
            session.add(db_meeting)
            session.flush()
            log.info("  Created new meeting record with id=%s", new_id)

        # Check if items already have substantial text
        existing_items = session.execute(
            select(AgendaItem).where(
                AgendaItem.body == db_meeting.body,
                AgendaItem.meeting_id == db_meeting.meeting_id,
            )
        ).scalars().all()

        if existing_items:
            avg_len = sum(len(i.agenda_item_text or "") for i in existing_items) / len(existing_items)
            if avg_len > 500:
                log.info("  Already enriched (avg %d chars) — skipping", int(avg_len))
                skipped_already += 1
                continue

        # Download and parse PDF
        log.info("  Downloading PDF...")
        pdf_bytes = fetch_pdf_bytes(url)
        if not pdf_bytes:
            log.warning("  Failed to download PDF")
            skipped_no_pdf += 1
            continue

        text = extract_pdf_text(pdf_bytes)
        if not text:
            log.warning("  Failed to extract text")
            continue

        pdf_items = parse_agenda_items(text)
        if not pdf_items:
            log.warning("  No items parsed from PDF")
            continue

        if not existing_items:
            # Create new item records
            body_code = db_meeting.body or BODY_CODE
            created = 0
            for pi in pdf_items:
                an = pi.get("agenda_item_number", "") or ""
                item = AgendaItem(
                    body=body_code,
                    meeting_id=db_meeting.meeting_id,
                    agenda_item_number=an,
                    agenda_item_id=f"{body_code}-{db_meeting.meeting_id}_{an}",
                    agenda_item_title=pi.get("agenda_item_title", "")[:256],
                    agenda_item_text=pi.get("agenda_item_text", ""),
                    sort_order=pi.get("sort_order", 0),
                    vote_or_action="",
                    source_body=body_code,
                    source_url=url,
                    c_number="",
                    c_number_base="",
                    case_number="",
                    created_at=datetime.now(),
                )
                session.add(item)
                created += 1

            # Update meeting count
            db_meeting.item_count_actual = created
            session.commit()
            total_created += created
            log.info("  Created %d new items", created)
        else:
            # Enrich existing items
            pdf_lookup = {pi["agenda_item_number"].strip(): pi["agenda_item_text"] for pi in pdf_items}
            enriched = 0
            for item in existing_items:
                an = str(item.agenda_item_number).strip()
                if an in pdf_lookup and len(pdf_lookup[an]) > len(item.agenda_item_text or ""):
                    item.agenda_item_text = pdf_lookup[an]
                    enriched += 1
            if enriched:
                session.commit()
                total_enriched += enriched
                log.info("  Enriched %d items", enriched)
            else:
                log.info("  No items to enrich")

        # Rate limit: avoid hammering Phoenix's servers
        time.sleep(2)

    session.close()
    log.info("=" * 60)
    log.info("Backfill complete!")
    log.info("  Items created:   %d", total_created)
    log.info("  Items enriched:  %d", total_enriched)
    log.info("  Skipped (no match): %d", skipped_no_match)
    log.info("  Skipped (already):  %d", skipped_already)
    log.info("  Skipped (no PDF):   %d", skipped_no_pdf)


if __name__ == "__main__":
    main()
