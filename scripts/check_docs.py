#!/usr/bin/env python3
"""
Standalone document availability check runner.

Usage:
  python scripts/check_docs.py [--minutes] [--docs]

Runs minutes URL discovery and/or supporting-doc availability checks
independently of the daily sync.  Default is both.  Can be run in
parallel with scripts/run_pipeline.py.
"""

import argparse
import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def run_minutes_check() -> tuple[str, int | None]:
    """Check completed meetings for newly available minutes URLs."""
    log.info("--- Minutes check pass ---")
    try:
        from sqlalchemy import text
        from db import get_engine
        from db.minutes_check import check_all as check_minutes

        engine = get_engine()
        with engine.connect() as conn:
            total_pending = conn.execute(
                text(
                    "SELECT COUNT(*) FROM meetings "
                    "WHERE minutes_url IS NULL AND sync_status = 'complete'"
                )
            ).scalar()
        log.info("  %d meetings without minutes_url", total_pending)
        updated = check_minutes(engine)
        if updated:
            log.info("  \u2705 %d meetings now have minutes_url", updated)
        else:
            log.info("  No new minutes found")
        return ("minutes_check", updated)
    except Exception as e:
        log.warning("  Minutes check pass skipped: %s", e, exc_info=True)
        return ("minutes_check", None)


def run_doc_check() -> tuple[str, dict | None]:
    """Seed and run the supporting-doc availability check."""
    log.info("--- Doc availability check ---")
    try:
        from scraper.common.doc_check import seed_next_doc_check, run_doc_check
        from db import get_session

        session = get_session()
        seeded = seed_next_doc_check(session)
        session.commit()
        session.close()
        log.info("  Seeded %d meeting(s) for doc check", seeded)
        stats = run_doc_check(dry_run=False)
        log.info(
            "  Checked: %d | Available: %d | Missing: %d | Sunsets: %d",
            stats["checked"],
            stats["docs_available"],
            stats["docs_still_missing"],
            stats["sunsets"],
        )
        return ("doc_check", stats)
    except Exception as e:
        log.warning("  Doc check skipped: %s", e, exc_info=True)
        return ("doc_check", None)


def main():
    parser = argparse.ArgumentParser(
        description="Run minutes and/or doc availability checks.",
    )
    parser.add_argument(
        "--minutes",
        action="store_true",
        help="Only run minutes check",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="Only run doc availability check",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run minutes and doc checks concurrently",
    )
    args = parser.parse_args()

    # Default: run both if neither flag is specified
    run_minutes = args.minutes or (not args.minutes and not args.docs)
    run_docs = args.docs or (not args.minutes and not args.docs)

    log.info("=== Doc check run ===")
    start = time.time()

    if args.parallel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = []
            if run_minutes:
                futs.append(pool.submit(run_minutes_check))
            if run_docs:
                futs.append(pool.submit(run_doc_check))
            for future in as_completed(futs):
                name, result = future.result()
                log.info("  \u2713 %s complete", name)
    else:
        if run_minutes:
            run_minutes_check()
        if run_docs:
            run_doc_check()

    elapsed = time.time() - start
    log.info("=== Doc check run complete (%d seconds) ===", elapsed)


if __name__ == "__main__":
    main()
