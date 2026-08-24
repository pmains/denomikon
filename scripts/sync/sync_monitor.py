#!/usr/bin/env python3
"""
sync_monitor.py — Post-sync diagnostics, auto-remediation, and report generation.

Runs after run_pipeline.py (or standalone) to:
  1. Diagnose sync health (counts by status, recent failures, stuck jobs, orphans)
  2. Pattern-match errors and attempt auto-remediation
  3. Generate a structured morning-ready report → data/sync/YYYY-MM-DD-monitor.txt

Usage:
    python scripts/sync_monitor.py                   # Full: diagnose + remediate + report
    python scripts/sync_monitor.py --report-only     # Regenerate report from existing data
    python scripts/sync_monitor.py --quiet           # Suppress stdout, only write files

Environment:
    POLISCOPIC_DB_TIER — "development" (default), "test", "production"
    PYTHONPATH          — must include scripts/ so 'from db import ...' works
"""

import argparse
import datetime
import os
import re
import sys
import time

# ── Path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from db import get_engine
from sqlalchemy import text

# ── Constants ───────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(PROJECT_ROOT, "data", "sync")

# Error pattern matching — ordered by specificity
ERROR_PATTERNS = [
    (r"UNIQUE constraint failed", "duplicate_item"),
    (r"No agenda items found", "no_items"),
    (r"Unsupported agenda format", "format_error"),
    (r"Timeout|timed out", "timeout"),
    (r"database disk image is malformed", "db_corrupt"),
]

# Policy-interest keywords (case-insensitive matching on agenda_item_title + text)
POLICY_KEYWORDS = [
    "housing", "zoning", "rezon", "apartment", "affordable",
    "bike", "bicycle", "transit", "light rail",
    "water", "drought", "groundwater",
    "solar", "renewable", "shade", "heat", "climate",
    "budget", "contract", "bond",
    "police", "fire",
    "license plate reader", "surveillance",
]


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Diagnostics
# ══════════════════════════════════════════════════════════════════════════════

def format_dt(dt_str):
    """Return a short human-friendly datetime string or 'N/A'."""
    if dt_str:
        try:
            return str(dt_str)[:19]
        except Exception:
            return str(dt_str)
    return "N/A"


def categorize_error(error_text):
    """Return an error category string based on pattern matching."""
    if not error_text:
        return "none"
    for pattern, category in ERROR_PATTERNS:
        if re.search(pattern, str(error_text), re.IGNORECASE):
            return category
    return "unknown"


def diagnose(engine):
    """Run all diagnostic queries and return a dict of results."""
    report = {}

    with engine.connect() as conn:
        # 1. Meeting counts by sync_status
        rows = conn.execute(
            text("SELECT sync_status, COUNT(*) FROM meetings GROUP BY sync_status ORDER BY sync_status")
        ).fetchall()
        report["status_counts"] = {r[0]: r[1] for r in rows}
        total_meetings = sum(report["status_counts"].values())
        report["total_meetings"] = total_meetings

        # 2. Failed in last 24 hours (by last_attempted_at)
        rows = conn.execute(
            text("""
                SELECT id, body, meeting_id, meeting_date, sync_status,
                       last_attempted_at, last_error, retry_count
                FROM meetings
                WHERE sync_status = 'failed'
                  AND last_attempted_at >= NOW() - INTERVAL '1 day'
                ORDER BY last_attempted_at DESC
            """)
        ).fetchall()
        report["recent_failures"] = [
            {
                "id": r.id, "body": r.body, "meeting_id": r.meeting_id,
                "meeting_date": r.meeting_date, "last_attempted_at": str(r.last_attempted_at) if r.last_attempted_at else None,
                "last_error": str(r.last_error) if r.last_error else None,
                "retry_count": r.retry_count,
                "error_category": categorize_error(r.last_error),
            }
            for r in rows
        ]

        # 3. Meetings stuck "in_progress" for more than 2 hours
        rows = conn.execute(
            text("""
                SELECT id, body, meeting_id, meeting_date, last_attempted_at
                FROM meetings
                WHERE sync_status = 'in_progress'
                  AND (last_attempted_at IS NULL
                       OR last_attempted_at <= NOW() - INTERVAL '2 hours')
                ORDER BY last_attempted_at ASC
            """)
        ).fetchall()
        report["stuck_in_progress"] = [
            {
                "id": r.id, "body": r.body, "meeting_id": r.meeting_id,
                "meeting_date": r.meeting_date,
                "last_attempted_at": str(r.last_attempted_at) if r.last_attempted_at else "never",
            }
            for r in rows
        ]

        # 4. Orphans — meetings without last_synced_at, meeting_date older than 7 days
        orphan_count = conn.execute(
            text("""
                SELECT COUNT(*) FROM meetings
                WHERE last_synced_at IS NULL
                  AND last_attempted_at IS NULL
                  AND meeting_date IS NOT NULL
                  AND NULLIF(meeting_date, '')::date < CURRENT_DATE - INTERVAL '7 days'
            """)
        ).scalar()
        report["orphan_count"] = orphan_count

        # 5. All failed meetings (for remediation phase)
        rows = conn.execute(
            text("""
                SELECT id, body, meeting_id, meeting_date, sync_status,
                       last_attempted_at, last_error, retry_count
                FROM meetings
                WHERE sync_status = 'failed'
                ORDER BY last_attempted_at DESC NULLS LAST
            """)
        ).fetchall()
        report["all_failures"] = [
            {
                "id": r.id, "body": r.body, "meeting_id": r.meeting_id,
                "meeting_date": r.meeting_date, "sync_status": r.sync_status,
                "last_attempted_at": str(r.last_attempted_at) if r.last_attempted_at else None,
                "last_error": str(r.last_error) if r.last_error else None,
                "retry_count": r.retry_count,
                "error_category": categorize_error(r.last_error),
            }
            for r in rows
        ]

    return report


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Auto-Remediation
# ══════════════════════════════════════════════════════════════════════════════

def remediate(engine, diagnostics, quiet=False):
    """
    Attempt auto-remediation for known failure patterns.
    Returns a list of remediation action dicts.
    """
    actions = []

    with engine.begin() as conn:
        for failure in diagnostics["all_failures"]:
            meeting_id = failure["id"]
            body_val = failure["body"]
            juris_meeting_id = failure["meeting_id"]
            meeting_date = failure["meeting_date"]
            error_cat = failure["error_category"]
            last_error = failure["last_error"]

            action = {
                "meeting_db_id": meeting_id,
                "body": body_val,
                "meeting_id": juris_meeting_id,
                "meeting_date": meeting_date,
                "error_category": error_cat,
                "attempted": False,
                "result": None,
            }

            # ── UNIQUE constraint on agenda_items ──────────────────────
            if error_cat == "duplicate_item":
                # Check if the meeting already has agenda items
                item_count = conn.execute(
                    text("SELECT COUNT(*) FROM agenda_items WHERE meeting_db_id = :mid"),
                    {"mid": meeting_id},
                ).scalar()

                if item_count > 0:
                    # Meeting already has items — mark as complete
                    conn.execute(
                        text("""
                            UPDATE meetings
                            SET sync_status = 'complete', last_error = NULL, updated_at = NOW()
                            WHERE id = :mid
                        """),
                        {"mid": meeting_id},
                    )
                    conn.execute(
                        text("""
                            UPDATE meetings SET retry_count = 0 WHERE id = :mid
                        """),
                        {"mid": meeting_id},
                    )
                    action["attempted"] = True
                    action["result"] = f"cleared → complete (found {item_count} existing items)"
                else:
                    # No items found — reset to pending for re-scrape
                    conn.execute(
                        text("""
                            UPDATE meetings
                            SET sync_status = 'pending', last_error = NULL, updated_at = NOW()
                            WHERE id = :mid
                        """),
                        {"mid": meeting_id},
                    )
                    action["attempted"] = True
                    action["result"] = "retried → pending (no items found, reset for re-scrape)"

            # ── No agenda items / Unsupported format ───────────────────
            elif error_cat in ("no_items", "format_error"):
                # Check meeting_date vs today
                today = datetime.date.today().isoformat()
                is_past = meeting_date and meeting_date < today
                is_old = meeting_date and meeting_date < (
                    datetime.date.today() - datetime.timedelta(days=30)
                ).isoformat()

                if is_past and not is_old:
                    # Past but not ancient — just add info about --force
                    conn.execute(
                        text("""
                            UPDATE meetings
                            SET last_error = :error, updated_at = NOW()
                            WHERE id = :mid
                        """),
                        {
                            "mid": meeting_id,
                            "error": f"Past meeting — try re-scrape with --force flag. ({error_cat})",
                        },
                    )
                    action["attempted"] = True
                    action["result"] = (
                        f"{error_cat} → noted for re-scrape with --force"
                    )
                elif is_old:
                    # Older than 30 days — mark manual_review
                    conn.execute(
                        text("""
                            UPDATE meetings
                            SET sync_status = 'manual_review', updated_at = NOW()
                            WHERE id = :mid
                        """),
                        {"mid": meeting_id},
                    )
                    action["attempted"] = True
                    action["result"] = f"{error_cat} → manual_review (meeting older than 30 days)"
                else:
                    # Future or today meeting — leave as failed, flag for review
                    action["attempted"] = False
                    action["result"] = f"{error_cat} → left for review (today/future meeting)"

            # ── Timeout ────────────────────────────────────────────────
            elif error_cat == "timeout":
                action["attempted"] = True
                action["result"] = "timeout → left for review (will retry on next sync)"
                # Reset to pending so the next sync will pick it up again
                conn.execute(
                    text("""
                        UPDATE meetings
                        SET sync_status = 'pending', last_error = NULL, updated_at = NOW()
                        WHERE id = :mid
                    """),
                    {"mid": meeting_id},
                )

            # ── DB corrupt ─────────────────────────────────────────────
            elif error_cat == "db_corrupt":
                action["attempted"] = False
                action["result"] = "db_corrupt → needs VACUUM or manual recovery (left as failed)"

            # ── Unknown ────────────────────────────────────────────────
            else:
                action["attempted"] = False
                action["result"] = "unknown → left for human review"

            log_remediation(action, quiet)
            actions.append(action)

    # ── Remediate stuck in_progress (reset to pending) ─────────────────────
    with engine.begin() as conn:
        for stuck in diagnostics["stuck_in_progress"]:
            conn.execute(
                text("""
                    UPDATE meetings
                    SET sync_status = 'pending', last_error = 'Reset from in_progress (stuck >2h)',
                        last_attempted_at = NULL, updated_at = NOW()
                    WHERE id = :mid
                """),
                {"mid": stuck["id"]},
            )
            action = {
                "meeting_db_id": stuck["id"],
                "body": stuck["body"],
                "meeting_id": stuck["meeting_id"],
                "meeting_date": stuck["meeting_date"],
                "error_category": "stuck_in_progress",
                "attempted": True,
                "result": "cleared → pending (stuck > 2 hours)",
            }
            log_remediation(action, quiet)
            actions.append(action)

    return actions


def log_remediation(action, quiet=False):
    """Log a single remediation action — both to stdout (if not quiet) and Python log."""
    meeting_ref = (
        f"{action['body']}/{action['meeting_id']} (db_id={action['meeting_db_id']})"
    )
    msg = (
        f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"REMEDIATE {meeting_ref}: {action['result']}"
    )
    if not quiet:
        print(msg)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Report Generation
# ══════════════════════════════════════════════════════════════════════════════

def get_upcoming_meetings(engine, max_results=15):
    """Return upcoming meetings (meeting_date >= today), newest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, body, meeting_id, meeting_date, meeting_type, sync_status
                FROM meetings
                WHERE NULLIF(meeting_date, '')::date >= CURRENT_DATE
                ORDER BY meeting_date ASC, body ASC
                LIMIT :limit
            """),
            {"limit": max_results},
        ).fetchall()
        return [
            {
                "id": r.id, "body": r.body, "meeting_id": r.meeting_id,
                "meeting_date": r.meeting_date, "meeting_type": r.meeting_type,
                "sync_status": r.sync_status,
            }
            for r in rows
        ]


def get_policy_items(engine, max_results=25):
    """Find agenda items matching policy-interest keywords in the next 7 days."""
    keyword_clauses = []
    for kw in POLICY_KEYWORDS:
        escaped = kw.replace("'", "''")
        keyword_clauses.append(
            f"(LOWER(ai.agenda_item_title) LIKE '%{escaped}%' "
            f"OR LOWER(ai.agenda_item_text) LIKE '%{escaped}%')"
        )
    where_keywords = " OR ".join(keyword_clauses)

    query = text(f"""
        SELECT m.id, m.body, m.meeting_id, m.meeting_date, m.meeting_type,
               ai.agenda_item_number, ai.agenda_item_title, ai.agenda_item_text
        FROM meetings m
        JOIN agenda_items ai ON ai.meeting_db_id = m.id
        WHERE NULLIF(m.meeting_date, '')::date >= CURRENT_DATE
          AND NULLIF(m.meeting_date, '')::date <= CURRENT_DATE + INTERVAL '7 days'
          AND ({where_keywords})
        ORDER BY m.meeting_date ASC, m.body ASC, ai.agenda_item_number ASC
        LIMIT :limit
    """)

    with engine.connect() as conn:
        rows = conn.execute(query, {"limit": max_results}).fetchall()
        return [
            {
                "meeting_id": r.id, "body": r.body, "meeting_id_juris": r.meeting_id,
                "meeting_date": r.meeting_date, "meeting_type": r.meeting_type,
                "item_number": r.agenda_item_number,
                "item_title": r.agenda_item_title,
                "item_text": (str(r.agenda_item_text)[:120] + "...") if r.agenda_item_text and len(str(r.agenda_item_text)) > 120 else r.agenda_item_text,
            }
            for r in rows
        ]


def generate_report(diagnostics, remediations, engine, sync_duration, quiet=False):
    """Write the structured monitor report to data/sync/YYYY-MM-DD-monitor.txt."""
    now = datetime.datetime.now()
    date_stamp = now.strftime("%Y-%m-%d")
    report_path = os.path.join(LOG_DIR, f"{date_stamp}-monitor.txt")

    counts = diagnostics["status_counts"]
    total = diagnostics["total_meetings"]
    complete = counts.get("complete", 0)
    pending = counts.get("pending", 0)
    no_agenda = counts.get("no_agenda", 0)
    manual_review = counts.get("manual_review", 0)
    in_progress = counts.get("in_progress", 0)
    failed_all = counts.get("failed", 0)
    failed_recent = len(diagnostics["recent_failures"])
    stuck = len(diagnostics["stuck_in_progress"])
    orphans = diagnostics["orphan_count"]

    # Percentage
    pct_complete = round(complete / total * 100, 1) if total else 0.0

    lines = []
    lines.append(f"=== Sync Monitor Report — {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    lines.append("")
    lines.append("Overall Sync Status")
    lines.append(f"  Total meetings: {total}")
    lines.append(f"  Complete: {complete} ({pct_complete}%)")
    lines.append(f"  Pending: {pending}")
    lines.append(f"  Failed (all time): {failed_all}")
    lines.append(f"  Failed (last 24h): {failed_recent}")
    lines.append(f"  No agenda: {no_agenda}")
    lines.append(f"  Manual review: {manual_review}")
    lines.append(f"  In progress: {in_progress}")
    lines.append(f"  Stuck in progress (>2h): {stuck}")
    lines.append(f"  Orphans (no sync, >7 days old): {orphans}")
    lines.append("")

    # Failed in last 24h
    lines.append("Failed in Last 24h:")
    if diagnostics["recent_failures"]:
        lines.append("  body | meeting_date | meeting_id | error_category | retry_count")
        lines.append("  " + "-" * 70)
        for f in diagnostics["recent_failures"]:
            lines.append(
                f"  {f['body']} | {f['meeting_date'] or 'N/A'} | {f['meeting_id']} | "
                f"{f['error_category']} | {f['retry_count']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Auto-remediation attempted
    lines.append("Auto-Remediation Attempted:")
    if remediations:
        for r in remediations:
            ref = f"{r['body']}/{r['meeting_id']} (db_id={r['meeting_db_id']})"
            status_icon = "✓" if r["attempted"] else "⚠"
            lines.append(f"  {status_icon} {ref}: {r['result']}")
    else:
        lines.append("  (none)")
    lines.append("")

    # Remaining issues requiring human attention
    lines.append("Remaining Issues (require human attention):")
    remaining = [r for r in remediations if not r["attempted"]] if remediations else []
    # Also list any failed that weren't remediated
    remediated_ids = {r["meeting_db_id"] for r in (remediations or [])}
    unremediated_failures = [
        f for f in diagnostics["all_failures"]
        if f["id"] not in remediated_ids
    ]

    if remaining:
        for r in remaining:
            lines.append(
                f"  • {r['body']}/{r['meeting_id']} (db_id={r['meeting_db_id']}): {r['result']}"
            )
    if unremediated_failures:
        for f in unremediated_failures:
            lines.append(
                f"  • {f['body']}/{f['meeting_id']} (db_id={f['id']}): {f['error_category']} — {str(f['last_error'])[:100]}"
            )
    if not remaining and not unremediated_failures:
        lines.append("  (none — all issues resolved or auto-remediated)")
    lines.append("")

    # Upcoming meetings (new this week)
    lines.append("New Meetings This Week (upcoming):")
    upcoming = get_upcoming_meetings(engine)
    if upcoming:
        lines.append("  body | date | type | status")
        lines.append("  " + "-" * 60)
        for u in upcoming:
            lines.append(
                f"  {u['body']} | {u['meeting_date']} | {u['meeting_type'][:40] or 'N/A'} | {u['sync_status']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Policy interest items
    lines.append("Policy Interest Items (next 7 days):")
    policy_items = get_policy_items(engine)
    if policy_items:
        seen = set()
        for pi in policy_items:
            key = (pi["body"], pi["meeting_date"], pi["meeting_type"])
            if key not in seen:
                lines.append(
                    f"  [{pi['body']}] {pi['meeting_date']} — {pi['meeting_type']}"
                )
                seen.add(key)
            # Indent the item title
            lines.append(
                f"    Item {pi['item_number']}: {str(pi['item_title'])[:100]}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Duration
    lines.append(f"Sync Duration: {sync_duration} seconds")
    lines.append("")

    report_text = "\n".join(lines)

    # Write to file
    with open(report_path, "w") as f:
        f.write(report_text)

    if not quiet:
        print(f"\nReport written → {report_path}")
        print(report_text)

    return report_path, report_text


# ══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync monitoring, remediation, and reporting tool."
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Skip diagnostics and remediation; regenerate report from existing data.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout; only write to log files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    start_time = time.time()

    if not args.quiet:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
              f"Sync Monitor starting...")

    # ── Connect to database ─────────────────────────────────────────────
    try:
        engine = get_engine()
    except Exception as e:
        msg = f"FATAL: Could not connect to database — {e}"
        # Write a minimal error report
        date_stamp = datetime.date.today().isoformat()
        report_path = os.path.join(LOG_DIR, f"{date_stamp}-monitor.txt")
        with open(report_path, "w") as f:
            f.write(f"=== Sync Monitor Report — {date_stamp} ===\n\n")
            f.write(f"ERROR: Database connection failed — {e}\n")
        if not args.quiet:
            print(msg)
            print(f"Minimal error report written → {report_path}")
        sys.exit(1)

    # ── Phase 1: Diagnostics ────────────────────────────────────────────
    if args.report_only:
        diagnostics = diagnose(engine)
        remediations = []
        if not args.quiet:
            print("[Report-only mode — skipping diagnostics and remediation]")
    else:
        if not args.quiet:
            print("Phase 1: Running diagnostics...")
        diagnostics = diagnose(engine)

        # ── Phase 2: Auto-Remediation ───────────────────────────────────
        if not args.quiet:
            print("Phase 2: Running auto-remediation...")
        remediations = remediate(engine, diagnostics, quiet=args.quiet)

    # ── Phase 3: Generate report ────────────────────────────────────────
    if not args.quiet:
        print("Phase 3: Generating report...")

    sync_duration = round(time.time() - start_time, 1)
    generate_report(diagnostics, remediations, engine, sync_duration, quiet=args.quiet)

    if not args.quiet:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
              f"Sync Monitor complete ({sync_duration}s)")


if __name__ == "__main__":
    main()
