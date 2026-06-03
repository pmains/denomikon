#!/usr/bin/env python3
"""
Daily sync runner — called by cron to keep all jurisdictions current.

Usage:
  POLISCOPIC_DB_TIER=development python scripts/daily_sync.py

Design:
  - Tier 1 & 2 (daily): 3-day rolling window — quick check for anything
    newly posted in the last few days. This avoids re-checking hundreds
    of completed meetings that never change.
  - Tier 3 (weekly/Sunday): 30-day rolling window — safety net for
    meetings that were posted further out than the 3-day window.
  - After both tiers, a minutes check pass re-visits completed meetings
    without minutes_url to see if minutes have been published since.
"""

import logging
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("daily_sync")

# ── Rolling windows ──────────────────────────────────────────────────────

DAILY_WINDOW_DAYS = 3        # Tier 1 & 2 scan this far back
WEEKLY_WINDOW_DAYS = 30      # Tier 3 (Sunday) scan


def _window_args(days: int) -> list[str]:
    """Return ['--start-date=YYYY-MM-DD', '--end-date=YYYY-MM-DD'] for an N-day window."""
    today = date.today()
    start = today - timedelta(days=days)
    return [f"--start-date={start}", f"--end-date={today}"]


def _daily_args(label: str, *extra: str) -> list[str]:
    """Arg list for daily (3-day) sync."""
    return [label] + _window_args(DAILY_WINDOW_DAYS) + list(extra)


def _weekly_args(label: str, *extra: str) -> list[str]:
    """Arg list for weekly (30-day) sync."""
    return [label] + _window_args(WEEKLY_WINDOW_DAYS) + list(extra)


# ── Sync config ──────────────────────────────────────────────────────────
# Tier-1 = twice-daily (active jurisdictions)
# Tier-2 = daily (all jurisdictions)
# Tier-3 = weekly safety net (all jurisdictions, 30-day window)

TIER_1: list[list[str]] = [
    _daily_args("chandler", "--sync"),
    _daily_args("tempe", "--sync"),
    # tempe-subcommittees has its own args parser and doesn't support dates
    ["tempe-subcommittees", "--sync"],
]

TIER_2: list[list[str]] = [
    _daily_args("bos", "--sync"),
    _daily_args("pz", "--sync"),
    _daily_args("adj", "--sync"),
    _daily_args("health", "--sync"),
    _daily_args("drain", "--sync"),
    _daily_args("tab", "--sync"),
    _daily_args("ida", "--sync"),
    _daily_args("mesa", "--sync"),
    _daily_args("phoenix", "--sync"),
    _daily_args("scottsdale", "--sync"),
    _daily_args("glendale", "--sync"),
    _daily_args("glendale-new", "--sync"),
    _daily_args("peoria", "--sync"),
    _daily_args("surprise", "--sync"),
    _daily_args("surprise-civicclerk", "--sync",
                "--bodies=surprise-pz,surprise-arts,surprise-veterans,surprise-library,surprise-parks,surprise-psprs-fire,surprise-psprs-police,surprise-health-benefits,surprise-nominations,surprise-audit,surprise-tourism,surprise-judicial-selection"),
    _daily_args("gilbert", "--sync"),
    _daily_args("tucson", "--sync"),
    _daily_args("tucson-pc", "--sync"),
    _daily_args("avondale", "--sync"),
    _daily_args("goodyear", "--sync"),
    _daily_args("el-mirage", "--sync"),
    _daily_args("paradise-valley", "--sync"),
    _daily_args("queen-creek", "--sync"),
    _daily_args("buckeye-granicus", "--sync"),
]

TIER_3: list[list[str]] = [
    # All jurisdictions, 30-day window — catches anything posted
    # outside the daily 3-day window.
    _weekly_args("chandler", "--sync"),
    _weekly_args("tempe", "--sync"),
    # tempe-subcommittees doesn't support date args; it defaults to
    # whatever the scraper considers current.
    ["tempe-subcommittees", "--sync"],
    _weekly_args("bos", "--sync"),
    _weekly_args("pz", "--sync"),
    _weekly_args("adj", "--sync"),
    _weekly_args("health", "--sync"),
    _weekly_args("drain", "--sync"),
    _weekly_args("tab", "--sync"),
    _weekly_args("ida", "--sync"),
    _weekly_args("mesa", "--sync"),
    _weekly_args("phoenix", "--sync"),
    _weekly_args("scottsdale", "--sync"),
    _weekly_args("glendale", "--sync"),
    _weekly_args("glendale-new", "--sync"),
    _weekly_args("peoria", "--sync"),
    _weekly_args("surprise", "--sync"),
    _weekly_args("surprise-civicclerk", "--sync",
                 "--bodies=surprise-pz,surprise-arts,surprise-veterans,surprise-library,surprise-parks,surprise-psprs-fire,surprise-psprs-police,surprise-health-benefits,surprise-nominations,surprise-audit,surprise-tourism,surprise-judicial-selection"),
    _weekly_args("gilbert", "--sync"),
    _weekly_args("tucson", "--sync"),
    _weekly_args("tucson-pc", "--sync"),
    _weekly_args("avondale", "--sync"),
    _weekly_args("goodyear", "--sync"),
    _weekly_args("el-mirage", "--sync"),
    _weekly_args("paradise-valley", "--sync"),
    _weekly_args("queen-creek", "--sync"),
    _weekly_args("buckeye-granicus", "--sync"),
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
    is_sunday_morning = weekday == 6 and hour < 6

    log.info("=== Daily sync start ===")

    daily_window = _window_args(DAILY_WINDOW_DAYS)
    log.info("  Daily window: %s to %s (last %d days)",
             daily_window[0].split("=")[1], daily_window[1].split("=")[1],
             DAILY_WINDOW_DAYS)

    results: list[str] = []

    # Tier 1: twice-daily (always run)
    log.info("--- Tier 1 (active jurisdictions) ---")
    for arglist in TIER_1:
        code, summary = run_sync(arglist, arglist[0])
        results.append(summary)
        log.info(summary)

    # Tier 2: daily (run once per day)
    log.info("--- Tier 2 (all jurisdictions) ---")
    for arglist in TIER_2:
        code, summary = run_sync(arglist, arglist[0])
        results.append(summary)
        log.info(summary)

    # Tier 3: weekly safety net (Sunday morning)
    if is_sunday_morning:
        weekly_window = _window_args(WEEKLY_WINDOW_DAYS)
        log.info("--- Tier 3 (weekly safety net) ---")
        log.info("  Weekly window: %s to %s (last %d days)",
                 weekly_window[0].split("=")[1], weekly_window[1].split("=")[1],
                 WEEKLY_WINDOW_DAYS)
        for arglist in TIER_3:
            code, summary = run_sync(arglist, arglist[0])
            results.append(summary)
            log.info(summary)
    else:
        log.info("--- No Tier 3 (next Sunday <%d>6 UTC) ---", hour)

    # ── Minutes check pass ──
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

    # ── Backfill votes from minutes PDFs ──
    log.info("--- Backfill votes (Destiny jurisdictions) ---")
    for jname in ["el-mirage", "glendale"]:
        try:
            from scraper.backfill_votes import backfill_jurisdiction
            cnt = backfill_jurisdiction(jname, limit=5)
            if cnt:
                log.info("  ✅ %s: votes backfilled for %d meeting(s)", jname, cnt)
            else:
                log.info("  %s: no new votes found", jname)
        except Exception as e:
            log.warning("  %s backfill skipped: %s", jname, e)

    # ── Summary ──
    log.info("=== Daily sync complete ===")
    log.info("  %d jurisdictions synced today, %d on Sunday",
             len(TIER_1) + len(TIER_2), len(TIER_3) if is_sunday_morning else 0)
    for r in results:
        log.info("  %s", r)


if __name__ == "__main__":
    main()
