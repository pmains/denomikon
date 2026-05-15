from __future__ import annotations

import argparse
import datetime as dt
import sys

from scraper.utils import log

def _print_top_level_help() -> None:
    """Print a comprehensive help message listing all supported boards, then exit."""
    print("usage: agenda_scraper.py <subcommand> [options]")
    print()
    print("Scrape meeting materials from Maricopa County and City of Tempe public governance boards.")
    print()
    print("Subcommands:")
    print("  bos       Board of Supervisors (default when no subcommand given)")
    print("  pz        Planning & Zoning Commission")
    print("  adj       Board of Adjustment")
    print("  drain     Drainage Review Board (2011\u20132013, defunct)")
    print("  health    Board of Health")
    print("  tab       Transportation Advisory Board")
    print("  ida       Industrial Development Authority (WordPress site)")
    print("  tempe     City of Tempe (Council, DRC, BOA, HPC, etc.)")
    print("  all       Sync all Maricopa County boards: BOS, PZ, ADJ, Drain, Health, TAB, IDA")
    print()
    print("Common options (varies per subcommand; use <subcommand> --help for details):")
    print("  --sync                    Search online, extract, and persist to database")
    print("  --year=YYYY               Sync an entire year (e.g. --year=2026)")
    print("  --month=YYYY-MM           Sync an entire month (e.g. --month=2026-04)")
    print("  --start-date=YYYY-MM-DD  Start date for search")
    print("  --end-date=YYYY-MM-DD    End date for search")
    print("  --date=YYYY-MM-DD        Single day shorthand")
    print("  --meeting-id=ID           Single meeting to sync (bypasses date search)")
    print("  --retry-failed            Re-sync meetings with failed/partial/pending status")
    print("  --init-db                 Create database tables")
    print("  --status                  Print sync status summary")
    print("  --failed                  List failed/partial meetings")
    print("  --force                   Re-sync even if status is complete")
    print("  --headed                  Run Playwright in headed mode")
    print("  --limit=N                 Max meetings to process")
    print("  -h, --help                Show this help message and exit")
    print()
    print("Date range precedence: --year > --month > --date > --start-date/--end-date")
    print("These flags are mutually exclusive.")
    raise SystemExit(0)


def _normalize_year_month(args, parser) -> None:
    """Expand --year and --month into --start-date and --end-date.

    Called after argparse parsing, before returning args.
    Precedence: --year > --month > --date > --start-date/--end-date
    """
    import calendar

    year_val = getattr(args, "year", None)
    month_val = getattr(args, "month", None)

    if year_val and month_val:
        parser.error("--year and --month are mutually exclusive")

    if year_val:
        if args.start_date or args.end_date or args.date:
            parser.error("--year cannot be combined with --date, --start-date, or --end-date")
        args.start_date = f"{year_val}-01-01"
        args.end_date = f"{year_val}-12-31"

    elif month_val:
        if args.start_date or args.end_date or args.date:
            parser.error("--month cannot be combined with --date, --start-date, or --end-date")
        parts = month_val.split("-")
        if len(parts) != 2:
            parser.error("--month must be in YYYY-MM format (e.g. --month=2026-04)")
        year = int(parts[0])
        month = int(parts[1])
        if month < 1 or month > 12:
            parser.error(f"Invalid month: {month}. Must be 1-12.")
        last_day = calendar.monthrange(year, month)[1]
        args.start_date = f"{year:04d}-{month:02d}-01"
        args.end_date = f"{year:04d}-{month:02d}-{last_day:02d}"


def parse_args(argv=None) -> argparse.Namespace:
    """Two-pass argparse: detect source subcommand first, then parse with the right parser.

    Supports:
        bos --sync --start-date=2026-01-01
        pz --sync --start-date=2026-01-01
        --sync --start-date=2026-01-01           (defaults to bos)
        --sync-pz --pz-start-date=01/01/2026     (deprecated, kept for backward compat)
        --help                                    (shows all subcommands)
    """
    source = "bos"
    rest = list(argv if argv is not None else sys.argv[1:])

    # Intercept top-level --help / -h (no subcommand given)
    if rest and rest[0] in ("-h", "--help"):
        _print_top_level_help()

    if rest and rest[0] in ("bos", "pz", "adj", "drain", "health", "tab", "ida", "tempe", "all"):
        source = rest.pop(0)

    if source == "bos":
        args = _parse_bos_args(rest)
    elif source == "adj":
        args = _parse_adj_args(rest)
    elif source == "drain":
        args = _parse_drain_args(rest)
    elif source == "health":
        args = _parse_health_args(rest)
    elif source == "tab":
        args = _parse_tab_args(rest)
    elif source == "ida":
        args = _parse_ida_args(rest)
    elif source == "tempe":
        args = _parse_tempe_args(rest)
    elif source == "all":
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
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--download", action="store_true", help="Download agenda/supporting files")
    p.add_argument("--extract-agenda-items", action="store_true", help="Extract agenda items from stored HTML agenda pages")
    p.add_argument("--extract-raw-agenda-blocks", action="store_true", help="Extract raw agenda-item blocks from stored HTML agenda pages")
    p.add_argument("--split-raw-agenda-blocks", action="store_true", help="Split raw agenda blocks into structured agenda items")
    p.add_argument("--self-test-splitter", action="store_true", help="Run splitter self-tests and exit")
    p.add_argument("--debug-agenda-html", action="store_true", help="Write diagnostics for the first agenda HTML page selected for item extraction")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--parallel", type=int, default=1, help="Process N meetings concurrently (default 1)")
    p.add_argument("--count-agenda-items", action="store_true", help="Visit agenda pages, count items, and print a summary table")
    p.add_argument("--list-agenda-items", action="store_true", help="Visit agenda pages and list numbered items with titles")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--persist", action="store_true", help="Persist extracted agenda items from CSV to database")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist directly to database (bypasses CSVs)")
    p.add_argument("--meeting-id", help="Single meeting ID to sync (e.g. 4449). Used with --sync to skip date search.")
    p.add_argument("--meeting-date", help="Meeting date (YYYY-MM-DD) for --meeting-id path")
    p.add_argument("--meeting-type", help="Meeting type (e.g. Formal) for --meeting-id path")
    p.add_argument("--meeting-title", help="Meeting title for --meeting-id path")
    p.add_argument("--meeting-url", help="Agenda URL for --meeting-id path")
    p.add_argument("--offline", action="store_true", help="Sync from a locally saved HTML file instead of the live server. Use with --sync --meeting-id.")
    p.add_argument("--from-file", help="Path to a local agenda HTML file to parse offline. Used with --sync.")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts for network/page operations (default 3)")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
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
    _normalize_year_month(args, p)
    return args


def _parse_pz_args(rest: list[str]) -> argparse.Namespace:
    """Parse PZ (Planning & Zoning) arguments."""
    p = argparse.ArgumentParser(description="Scrape Maricopa Planning & Zoning agenda materials", prog="pz")
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
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
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def _parse_adj_args(rest: list[str]) -> argparse.Namespace:
    """Parse ADJ (Board of Adjustment) arguments."""
    p = argparse.ArgumentParser(description="Scrape Maricopa Board of Adjustment agenda materials", prog="adj")
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
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
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def _parse_drain_args(rest: list[str]) -> argparse.Namespace:
    """Parse DRAIN (Drainage Review Board) arguments."""
    p = argparse.ArgumentParser(
        description="Scrape Maricopa Drainage Review Board agenda materials",
        prog="drain",
    )
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
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
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def _parse_health_args(rest: list[str]) -> argparse.Namespace:
    """Parse HEALTH (Board of Health) arguments."""
    p = argparse.ArgumentParser(
        description="Scrape Maricopa County Board of Health agenda materials",
        prog="health",
    )
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
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
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def _parse_tab_args(rest: list[str]) -> argparse.Namespace:
    """Parse TAB (Transportation Advisory Board) arguments."""
    p = argparse.ArgumentParser(
        description="Scrape Maricopa County Transportation Advisory Board agenda materials",
        prog="tab",
    )
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
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
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def _parse_tempe_args(rest: list[str]) -> argparse.Namespace:
    """Parse Tempe City Council arguments."""
    p = argparse.ArgumentParser(
        description="Scrape City of Tempe public meeting materials (via OnBase Agenda Online)",
        prog="tempe",
    )
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist to database")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--meeting-id", help="Single meeting ID to sync (bypasses date search)")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    args = p.parse_args(rest)
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def _parse_ida_args(rest: list[str]) -> argparse.Namespace:
    """Parse IDA (Industrial Development Authority) arguments."""
    p = argparse.ArgumentParser(
        description="Scrape Maricopa County Industrial Development Authority meeting materials",
        prog="ida",
    )
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--year", help="Sync an entire year (e.g. --year=2026)")
    p.add_argument("--month", help="Sync an entire month (e.g. --month=2026-04)")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist to database")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--meeting-id", help="Single meeting ID (date-based, e.g. 2026-03-10)")
    p.add_argument("--offline", action="store_true", help="Sync from a locally saved HTML file instead of the live server")
    p.add_argument("--from-file", help="Path to a local agenda HTML file to parse offline")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts for network/page operations (default 3)")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--download", action="store_true", help="Download agenda PDF and packet PDF")
    p.add_argument("--bodies", help="Body group to sync: council, drc, boa, hpc, all (default: all)")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    _normalize_year_month(args, p)
    return args


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)

