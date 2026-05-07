from __future__ import annotations

import argparse
import datetime as dt
import sys

from scraper.utils import log

def parse_args(argv=None) -> argparse.Namespace:
    """Two-pass argparse: detect source subcommand first, then parse with the right parser.

    Supports:
        bos --sync --start-date=2026-01-01
        pz --sync --start-date=2026-01-01
        --sync --start-date=2026-01-01           (defaults to bos)
        --sync-pz --pz-start-date=01/01/2026     (deprecated, kept for backward compat)
    """
    source = "bos"
    rest = list(argv if argv is not None else sys.argv[1:])

    if rest and rest[0] in ("bos", "pz"):
        source = rest.pop(0)

    if source == "bos":
        args = _parse_bos_args(rest)
    else:
        args = _parse_pz_args(rest)
    args.source = source
    return args


def _parse_bos_args(rest: list[str]) -> argparse.Namespace:
    """Parse BOS (Board of Supervisors) arguments."""
    p = argparse.ArgumentParser(description="Scrape Maricopa BOS agenda materials", prog="bos")
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--download", action="store_true", help="Download agenda/supporting files")
    p.add_argument("--extract-agenda-items", action="store_true", help="Extract agenda items from stored HTML agenda pages")
    p.add_argument("--extract-raw-agenda-blocks", action="store_true", help="Extract raw agenda-item blocks from stored HTML agenda pages")
    p.add_argument("--split-raw-agenda-blocks", action="store_true", help="Split raw agenda blocks into structured agenda items")
    p.add_argument("--self-test-splitter", action="store_true", help="Run splitter self-tests and exit")
    p.add_argument("--debug-agenda-html", action="store_true", help="Write diagnostics for the first agenda HTML page selected for item extraction")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--count-agenda-items", action="store_true", help="Visit agenda pages, count items, and print a summary table")
    p.add_argument("--list-agenda-items", action="store_true", help="Visit agenda pages and list numbered items with titles")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--persist", action="store_true", help="Persist extracted agenda items from CSV to database")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist directly to database (bypasses CSVs)")
    p.add_argument("--meeting-id", help="Single meeting ID to sync (e.g. 4449). Used with --sync to skip date search.")
    p.add_argument("--offline", action="store_true", help="Sync from a locally saved HTML file instead of the live server. Use with --sync --meeting-id.")
    p.add_argument("--from-file", help="Path to a local agenda HTML file to parse offline. Used with --sync.")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts for network/page operations (default 3)")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--sync-votes", action="store_true", help="Extract vote results from meeting summaries")
    # Deprecated PZ flags (kept for backward compatibility)
    p.add_argument("--sync-pz", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--pz-limit", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--pz-start-date", help=argparse.SUPPRESS)
    p.add_argument("--pz-end-date", help=argparse.SUPPRESS)
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    return args


def _parse_pz_args(rest: list[str]) -> argparse.Namespace:
    """Parse PZ (Planning & Zoning) arguments."""
    p = argparse.ArgumentParser(description="Scrape Maricopa Planning & Zoning agenda materials", prog="pz")
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist to database")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--meeting-id", help="Single meeting ID to sync")
    p.add_argument("--offline", action="store_true", help="Sync from a locally saved HTML file instead of the live server")
    p.add_argument("--from-file", help="Path to a local agenda HTML file to parse offline")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts for network/page operations (default 3)")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    return args


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)

