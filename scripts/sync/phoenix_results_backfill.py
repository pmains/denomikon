#!/usr/bin/env python3
"""
phoenix_results_backfill.py — One-time backfill for Phoenix AEM meeting results.

Fetches all ~4,196 past meeting result PDF metadata from the AEM results
endpoint and syncs them to the database incrementally.

Usage:
    python3 scripts/sync/phoenix_results_backfill.py
"""

import sys
import os
import time
import logging

# Ensure scraper modules are importable
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "scraper", "jurisdictions"))

import urllib.parse

from phoenix_aem import RESULTS_BASE, _build_url, fetch_json, convert_to_meeting_dict, resolve_body
from db import get_engine, replace_meeting_data_safe
from db.models import Meeting
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text as sql_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("phoenix-results-backfill")

PAGE_SIZE = 10
MAX_TOTAL = 5000


def meeting_exists(conn, body: str, meeting_id: str) -> bool:
    """Check if a meeting already exists using raw SQL."""
    row = conn.execute(
        sql_text("SELECT 1 FROM meetings WHERE body = :body AND meeting_id = :mid"),
        {"body": body, "mid": meeting_id},
    ).fetchone()
    return row is not None


def insert_meeting(conn, body: str, meeting_id: str, md: dict) -> None:
    """Insert a meeting row using raw SQL."""
    conn.execute(
        sql_text("""
            INSERT INTO meetings (
                body, meeting_id, meeting_date, meeting_type,
                meeting_title, source_url, sync_status,
                retry_count, supporting_doc_count,
                items_extracted, supporting_docs_extracted,
                votes_extracted,
                _multi_jurisdiction_backfilled,
                _body_backfilled,
                created_at, updated_at
            ) VALUES (
                :body, :meeting_id, :meeting_date, :meeting_type,
                :meeting_title, :source_url, 'complete',
                0, 0,
                FALSE, FALSE,
                FALSE,
                FALSE,
                FALSE,
                NOW(), NOW()
            )
            ON CONFLICT (body, meeting_id) DO NOTHING
        """),
        {
            "body": body,
            "meeting_id": meeting_id,
            "meeting_date": md.get("meeting_date", "") or "",
            "meeting_type": "Result",
            "meeting_title": (md.get("meeting_title", "") or "")[:512],
            "source_url": (md.get("source_url", "") or "")[:1024],
        },
    )


def main():
    log.info("Connecting to DB...")
    engine = get_engine()
    log.info("DB ready.")

    offset = 0
    total_fetched = 0
    total_new = 0
    total_skipped = 0
    errors = 0
    start_ts = time.time()

    with engine.begin() as conn:
        while total_fetched < MAX_TOTAL:
            url = _build_url(RESULTS_BASE, "", offset)
            try:
                data = fetch_json(url)
            except Exception as e:
                log.warning("Failed at offset %d: %s", offset, e)
                errors += 1
                if errors > 5:
                    log.error("Too many errors, aborting")
                    break
                offset += PAGE_SIZE
                time.sleep(2)
                continue

            errors = 0
            results = data.get("results", [])
            if not results:
                log.info("No more results at offset %d, done", offset)
                break

            page_new = 0
            for raw in results:
                total_fetched += 1
                title = (raw.get("title") or "").strip()
                if not title:
                    total_skipped += 1
                    continue

                slug, code = resolve_body(title)
                meeting_dict = convert_to_meeting_dict(raw, slug, code)
                meeting_dict["meeting_type"] = "Result"

                meeting_id = meeting_dict["meeting_id"]
                body_code = meeting_dict["body_code"]

                if meeting_exists(conn, body_code, meeting_id):
                    total_skipped += 1
                    continue

                insert_meeting(conn, body_code, meeting_id, meeting_dict)
                total_new += 1
                page_new += 1

            elapsed = time.time() - start_ts
            rate = total_fetched / elapsed if elapsed > 0 else 0
            log.info(
                "offset=%4d  fetched=%4d  new=%4d  skipped=%4d  err=%d  (%.0fs, %.1f/s)",
                offset, total_fetched, total_new, total_skipped, errors, elapsed, rate,
            )

            if len(results) < PAGE_SIZE:
                log.info("Last page (only %d results), done", len(results))
                break

            offset += PAGE_SIZE
            time.sleep(0.5)

    elapsed = time.time() - start_ts
    log.info(
        "DONE: %d fetched, %d new, %d skipped, %d errors in %.0fs",
        total_fetched, total_new, total_skipped, errors, elapsed,
    )


if __name__ == "__main__":
    main()
