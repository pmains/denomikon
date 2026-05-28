#!/usr/bin/env python3
"""
Daily sync runner — called by cron to keep all jurisdictions current.

Usage:
  POLISCOPIC_DB_TIER=development python scripts/daily_sync.py

This runs the sync for every jurisdiction and logs what changed.
After the main sync it also runs a "minutes check" pass that re-visits
completed meetings to see if minutes have been published since the
initial scrape (minutes often appear days/weeks after the meeting).
"""

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("daily_sync")

# ── Jurisdiction sync config ──
# Each entry: (label, args...)
# Tier-1 = twice-daily (active jurisdictions)
# Tier-2 = daily (most jurisdictions)
# Tier-3 = weekly (low-frequency bodies)

TIER_1: list[list[str]] = [
    ["chandler", "--sync", "--year=2026"],
    ["tempe", "--sync", "--year=2026"],
]

TIER_2: list[list[str]] = [
    ["bos", "--sync", "--year=2026"],
    ["pz", "--sync", "--year=2026"],
    ["adj", "--sync", "--year=2026"],
    ["health", "--sync", "--year=2026"],
    ["drain", "--sync", "--year=2026"],
    ["tab", "--sync", "--year=2026"],
    ["ida", "--sync", "--year=2026"],
    ["mesa", "--sync", "--year=2026"],
    ["phoenix", "--sync", "--year=2026"],
    ["scottsdale", "--sync", "--year=2026"],
    ["glendale", "--sync", "--year=2026"],
    ["glendale-new", "--sync", "--year=2026"],
    ["peoria", "--sync", "--year=2026"],
    ["surprise", "--sync", "--year=2026"],
    ["gilbert", "--sync", "--year=2026"],
    ["avondale", "--sync", "--year=2026"],
    ["goodyear", "--sync", "--year=2026"],
    ["el-mirage", "--sync", "--year=2026"],
    ["buckeye-granicus", "--sync", "--year=2026"],
]

TIER_3: list[list[str]] = [
    # County boards that meet less frequently
    ["bos", "--sync", "--year=2025"],
    ["ida", "--sync", "--year=2025"],
    ["tab", "--sync", "--year=2025"],
    # Maricopa County on-base boards (monthly/quarerly)
    ["pz", "--sync", "--year=2025"],
    ["adj", "--sync", "--year=2025"],
    ["health", "--sync", "--year=2025"],
]


def run_sync(args: list[str], label: str) -> tuple[int, str]:
    """Run a single agenda_scraper sync and return (exit_code, summary_line)."""
    cmd = [sys.executable, "scripts/agenda_scraper.py"] + args
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
        elapsed = time.time() - start
        # Extract the last meaningful output line
        lines = result.stdout.strip().split("\n")
        summary = (lines[-1] if lines else "").strip()
        if not summary or "Synced" not in summary:
            summary = (lines[-2] if len(lines) > 1 else "").strip()
        return result.returncode, f"{label}: {summary} ({elapsed:.0f}s)"
    except subprocess.TimeoutExpired:
        return -1, f"{label}: TIMEOUT (>600s)"
    except Exception as e:
        return -1, f"{label}: ERROR {e}"


def main():
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon
    hour = now.hour

    log.info("=== Daily sync start ===")

    results: list[str] = []

    # Tier 1: twice-daily (always run)
    log.info("--- Tier 1 (active jurisdictions) ---")
    for args in TIER_1:
        code, summary = run_sync(args, args[0])
        results.append(summary)
        log.info(summary)

    # Tier 2: daily (run once per day)
    log.info("--- Tier 2 (all jurisdictions) ---")
    for args in TIER_2:
        code, summary = run_sync(args, args[0])
        results.append(summary)
        log.info(summary)

    # Tier 3: weekly historical backfill (Sunday morning)
    if weekday == 6 and hour < 6:
        log.info("--- Tier 3 (weekly historical backfill) ---")
        for args in TIER_3:
            code, summary = run_sync(args, args[0])
            results.append(summary)
            log.info(summary)

    # ── Minutes check pass ──
    # Re-visit completed meetings that have no minutes_url or item_count_actual=0
    # to see if minutes have been published since the original sync.
    log.info("--- Minutes check pass ---")
    try:
        from db import get_engine
        from db.minutes_check import check_all as check_minutes
        engine = get_engine()
        conn = engine.raw_connection()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM meetings WHERE minutes_url IS NULL AND sync_status = 'complete'"
        )
        total_pending = cursor.fetchone()[0]
        conn.close()
        log.info("  %d meetings without minutes_url", total_pending)
        updated = check_minutes(engine)
        if updated:
            log.info("  ✅ %d meetings now have minutes_url", updated)
        else:
            log.info("  No new minutes found")
    except Exception as e:
        log.warning("  Minutes check pass skipped: %s", e, exc_info=True)

    # ── Summary ──
    log.info("=== Daily sync complete ===")
    log.info("  %d jurisdictions synced", len(TIER_1) + len(TIER_2))
    for r in results:
        log.info("  %s", r)


if __name__ == "__main__":
    main()
