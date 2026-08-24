#!/usr/bin/env python3
"""
Standalone minutes-URL discovery runner.

Checks completed meetings for newly available minutes (PDF) URLs from
jurisdiction agenda portals.  Runs independently of the daily pipeline.

Usage:
  python scripts/check_minutes.py
"""

import argparse
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Discover newly posted meeting minutes URLs.",
    )
    parser.parse_args()

    start = time.time()
    log.info("=== Minutes check ===")

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
    except Exception as e:
        log.warning("  Minutes check failed: %s", e, exc_info=True)

    elapsed = time.time() - start
    log.info("=== Minutes check complete (%d seconds) ===", elapsed)


if __name__ == "__main__":
    main()
