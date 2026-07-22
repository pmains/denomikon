#!/usr/bin/env python3
"""
Rescrape all 2025 Buckeye meetings via Granicus pipeline (packet pass only).

Background:
  Buckeye 2025 meetings were synced via the old NovusAgenda scraper (buckeye.py).
  This script re-runs them through the Granicus pipeline (buckeye_granicus.py)
  which:
    - Downloads agenda packet PDFs from CloudFront
    - Extracts items via pdftotext (richer text than HTML parse)
    - Skips the heavy supporting-doc extraction (that's a separate pass)

Usage:
  nohup python3 -u scripts/ops/rescrape_buckeye_2025_packets.py \
    > data/rescrape-buckeye-2025-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import logging
import sys
import time
from datetime import date

sys.path.insert(0, "scripts")

from db import get_session, init_db, replace_meeting_data_safe, update_sync_status
from db import Meeting as MeetingModel
from scraper.buckeye_granicus import (
    search_buckeye_meetings,
    fetch_and_parse_agenda,
    extract_supporting_docs,
    SOURCE_SYSTEM,
)
from sqlalchemy import select

log = logging.getLogger("rescrape_buckeye")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

YEAR = 2025


def main():
    log.info("=== Rescrape Buckeye %d (Granicus packet pass) ===", YEAR)
    t_start = time.time()

    init_db()
    session = get_session()

    # Step 1: Discover meetings on Granicus for 2026
    log.info("Discovering Buckeye meetings on Granicus for %d...", YEAR)
    t0 = time.time()
    meetings = search_buckeye_meetings(year=YEAR, use_html=True)
    log.info("Discovery: %d meetings found in %.1fs", len(meetings), time.time() - t0)

    # Step 2: Process each meeting
    total_items = 0
    skipped = 0
    parsed = 0
    failed = 0

    for idx, m in enumerate(meetings, 1):
        meeting_id = str(m.get("event_id", m["meeting_id"]))
        meeting_date = m["meeting_date"]
        body_code = m.get("body_code", "buckeye-cc")
        source_url = m.get("agenda_url", "") or m.get("source_url", "")
        meeting_type = m.get("meeting_type", "")
        meeting_title = m.get("meeting_title", "")
        has_packet = bool(m.get("packet_url"))

        meeting_dict = {
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "meeting_type": meeting_type,
            "meeting_title": meeting_title,
            "source_url": source_url,
            "source_system": SOURCE_SYSTEM,
        }

        # Check if already synced via Granicus (source_system set)
        existing = session.execute(
            select(MeetingModel).where(
                MeetingModel.body == body_code,
                MeetingModel.meeting_id == meeting_id,
            )
        ).scalar_one_or_none()

        if existing and existing.source_system == SOURCE_SYSTEM and existing.sync_status == "complete":
            skipped += 1
            log.info(
                "[%d/%d] %s %s %s: already Granicus-synced (%d items) — skip",
                idx, len(meetings), meeting_id, meeting_date, body_code,
                existing.item_count_actual or 0,
            )
            continue

        # Process this meeting
        log.info(
            "[%d/%d] %s %s %s (packet=%s)",
            idx, len(meetings), meeting_id, meeting_date, body_code,
            "yes" if has_packet else "no",
        )

        try:
            # Step 2a: Parse items from packet PDF
            items = fetch_and_parse_agenda(m)
            if items:
                for it in items:
                    an = (it.get("agenda_item_number", "") or "").strip()
                    it["agenda_item_id"] = f"{body_code}-{meeting_id}_{an}"
                    it["meeting_id"] = meeting_id
                    it["meeting_date"] = meeting_date
                    it["meeting_type"] = meeting_type
                    it["source_body"] = body_code
                    it["source_url"] = source_url
                log.info("  -> %d items from packet", len(items))

            # Step 2b: Persist (no supporting docs this pass)
            replace_meeting_data_safe(
                session, body_code, meeting_id, meeting_dict, items,
                supporting_doc_dicts=[],
            )
            total_items += len(items)
            parsed += 1
            log.info(
                "  -> persisted %d items",
                len(items),
            )

        except Exception as e:
            failed += 1
            log.error("  -> FAILED: %s", e, exc_info=True)
            try:
                update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
            except Exception:
                pass

    # Summary
    elapsed = time.time() - t_start
    log.info("=" * 60)
    log.info("Complete: %d meetings, %d items total", len(meetings), total_items)
    log.info("  Parsed:  %d", parsed)
    log.info("  Skipped: %d (already Granicus-synced)", skipped)
    log.info("  Failed:  %d", failed)
    log.info("  Elapsed: %.0fs (%.1f min)", elapsed, elapsed / 60)
    log.info("=" * 60)

    session.close()


if __name__ == "__main__":
    main()
