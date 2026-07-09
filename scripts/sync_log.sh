#!/bin/bash
# =============================================================================
# sync_log.sh — Run daily_sync.py with structured logging
#
# Usage:
#   ./scripts/sync_log.sh                    # Use system python
#   PYTHON=/path/to/python ./scripts/sync_log.sh   # Override python binary
#
# What it does:
#   1. Runs a lightweight DB pre-check (counts meetings, recent syncs)
#   2. Runs daily_sync.py, capturing stdout+stderr
#   3. Runs a DB post-check to show what changed
#   4. Saves full gzipped log → data/sync/YYYY-MM-DD.log.gz
#   5. Saves plain-text summary  → data/sync/YYYY-MM-DD-summary.txt
#   6. Cleans up logs older than 90 days
#   7. Exits with daily_sync.py's exit code
#
# Environment:
#   POLISCOPIC_DB_TIER  — set to "development" (default if unset)
#   PYTHON              — python interpreter (default: from .venv or system)
#   PYTHONPATH          — defaults to <project>/scripts
# =============================================================================

set -euo pipefail

# ── Project root (where scripts/, data/, .venv/ live) ──────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# ── Settings ────────────────────────────────────────────────────────────────
LOG_DIR="data/sync"
LOG_RETENTION_DAYS=90

# Ensure the log directory exists
mkdir -p "$LOG_DIR"

# Date stamp for filenames
DATE_STAMP=$(date "+%Y-%m-%d")
LOG_FILE="$LOG_DIR/$DATE_STAMP.log.gz"
SUMMARY_FILE="$LOG_DIR/$DATE_STAMP-summary.txt"

# ── Python interpreter and path ─────────────────────────────────────────────
# Prefer project .venv, fall back to PYTHON env var, then system python
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
else
    PYTHON="${PYTHON:-python3}"
fi

# PYTHONPATH must include scripts/ so 'from db import ...' works
export PYTHONPATH="${PYTHONPATH:-$PROJECT_ROOT/scripts}"

# Database — db/config.py loads .env which supplies DATABASE_URL.
# For backwards compat, set tier too (harmless when DATABASE_URL is set).
export POLISCOPIC_DB_TIER="${POLISCOPIC_DB_TIER:-development}"

# ── Timestamp helpers ──────────────────────────────────────────────────────
START_EPOCH=$(date +%s)
START_ISO=$(date -Iseconds)

log_info() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ── Step 1: DB pre-check ───────────────────────────────────────────────────
log_info "Running database pre-check..."

PRE_CHECK=$(
    "$PYTHON" -c "
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname('$SCRIPT_DIR'), 'scripts'))
sys.path.insert(0, '$PROJECT_ROOT/scripts')

from db import get_engine, get_session
from db.models import Meeting, AgendaItem
from sqlalchemy import func, text

engine = get_engine()
session = get_session()

# Total meetings in DB
total_meetings = session.query(func.count(Meeting.id)).scalar()

# Meetings with sync_status = 'complete'
completed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'complete'
).scalar()

# Meetings that failed
failed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'error'
).scalar()

# Pending meetings (never synced)
pending = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'pending'
).scalar()

# Meetings synced in the last 24 hours
from datetime import datetime, timezone, timedelta
since = datetime.now(timezone.utc) - timedelta(hours=24)
recent_syncs = session.query(func.count(Meeting.id)).filter(
    Meeting.last_synced_at >= since
).scalar()

# Total agenda items
total_items = session.query(func.count(AgendaItem.id)).scalar()

session.close()
engine.dispose()

print(f'TOTAL_MEETINGS={total_meetings}')
print(f'COMPLETED={completed}')
print(f'FAILED={failed}')
print(f'PENDING={pending}')
print(f'RECENT_SYNCS={recent_syncs}')
print(f'TOTAL_ITEMS={total_items}')
" 2>&1
)

echo "$PRE_CHECK"
eval "$PRE_CHECK" 2>/dev/null || true

PRE_TOTAL="${TOTAL_MEETINGS:-?}"
PRE_COMPLETED="${COMPLETED:-?}"
PRE_FAILED="${FAILED:-?}"
PRE_PENDING="${PENDING:-?}"
PRE_RECENT="${RECENT_SYNCS:-?}"
PRE_ITEMS="${TOTAL_ITEMS:-?}"

# ── Step 2: Run daily_sync.py ─────────────────────────────────────────────
log_info "Starting daily_sync.py..."

# We run daily_sync.py and capture everything to a temp file,
# then compress + save it after.  We also tee to stdout so the
# operator can see progress.
TEMP_LOG=$(mktemp -t sync_log.XXXXXX)
trap 'rm -f "$TEMP_LOG"' EXIT

# Disable set -e for the sync run so we capture the exit code
set +e
"$PYTHON" "$PROJECT_ROOT/scripts/daily_sync.py" 2>&1 | tee "$TEMP_LOG"
SYNC_EXIT=${PIPESTATUS[0]}
set -e

SYNC_END_EPOCH=$(date +%s)
SYNC_DURATION=$((SYNC_END_EPOCH - START_EPOCH))

# ── Step 3: DB post-check ──────────────────────────────────────────────────
log_info "Running database post-check..."

POST_CHECK=$(
    "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT/scripts')

from db import get_engine, get_session
from db.models import Meeting, AgendaItem
from sqlalchemy import func
from datetime import datetime, timezone, timedelta

engine = get_engine()
session = get_session()

total_meetings = session.query(func.count(Meeting.id)).scalar()
completed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'complete'
).scalar()
failed = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'error'
).scalar()
pending = session.query(func.count(Meeting.id)).filter(
    Meeting.sync_status == 'pending'
).scalar()

since = datetime.now(timezone.utc) - timedelta(hours=24)
recent_syncs = session.query(func.count(Meeting.id)).filter(
    Meeting.last_synced_at >= since
).scalar()

# Meetings synced in the last 24 hours that were NOT synced before
# (i.e., first-time syncs — meetings that got their first last_synced_at)
# We approximate by counting all that were synced within this run
run_start = datetime.fromtimestamp($START_EPOCH, tz=timezone.utc)
synced_in_run = session.query(func.count(Meeting.id)).filter(
    Meeting.last_synced_at >= run_start
).scalar()

# New meetings discovered (created in this run)
# We approximate by counting meetings created_at >= run_start
new_meetings = session.query(func.count(Meeting.id)).filter(
    Meeting.created_at >= run_start
).scalar()

# New agenda items
total_items = session.query(func.count(AgendaItem.id)).scalar()
new_items = session.query(func.count(AgendaItem.id)).filter(
    AgendaItem.created_at >= run_start
).scalar() if hasattr(AgendaItem, 'created_at') else 0

session.close()
engine.dispose()

print(f'TOTAL_MEETINGS={total_meetings}')
print(f'COMPLETED={completed}')
print(f'FAILED={failed}')
print(f'PENDING={pending}')
print(f'RECENT_SYNCS={recent_syncs}')
print(f'SYNCED_IN_RUN={synced_in_run}')
print(f'NEW_MEETINGS={new_meetings}')
print(f'TOTAL_ITEMS={total_items}')
print(f'NEW_ITEMS={new_items}')
" 2>&1
)

echo "$POST_CHECK"
eval "$POST_CHECK" 2>/dev/null || true

POST_TOTAL="${TOTAL_MEETINGS:-$PRE_TOTAL}"
POST_COMPLETED="${COMPLETED:-$PRE_COMPLETED}"
POST_FAILED="${FAILED:-$PRE_FAILED}"
POST_PENDING="${PENDING:-$PRE_PENDING}"
POST_RECENT="${RECENT_SYNCS:-$PRE_RECENT}"
POST_SYNCED="${SYNCED_IN_RUN:-0}"
POST_NEW_MEETINGS="${NEW_MEETINGS:-0}"
POST_ITEMS="${TOTAL_ITEMS:-$PRE_ITEMS}"
POST_NEW_ITEMS="${NEW_ITEMS:-0}"

# ── Determine completion status ────────────────────────────────────────────
# We consider the sync successful if we got at least some new syncs and
# no catastrophic errors from daily_sync.py
if [ $SYNC_EXIT -eq 0 ]; then
    COMPLETION_STATUS="success"
elif [ $SYNC_EXIT -gt 0 ] && [ $POST_SYNCED -gt 0 ]; then
    COMPLETION_STATUS="partial"   # Some syncs ran but some failed
else
    COMPLETION_STATUS="failed"
fi

# Count errors from the log
ERROR_COUNT=$(grep -ci '\bERROR\b' "$TEMP_LOG" 2>/dev/null || echo 0)

# ── Step 4: Write gzipped full log ─────────────────────────────────────────
log_info "Saving full log → $LOG_FILE"
gzip -c "$TEMP_LOG" > "$LOG_FILE"

# ── Step 5: Write summary file ─────────────────────────────────────────────
log_info "Writing summary → $SUMMARY_FILE"

{
    echo "# Sync Summary — $DATE_STAMP"
    echo "# Generated: $START_ISO"
    echo ""
    echo "start_time: $START_ISO"
    echo "duration_seconds: $SYNC_DURATION"
    echo "exit_code: $SYNC_EXIT"
    echo "completion_status: $COMPLETION_STATUS"
    echo "error_count: $ERROR_COUNT"
    echo ""
    echo "# ── DB pre-check ──"
    echo "pre_total_meetings: $PRE_TOTAL"
    echo "pre_completed: $PRE_COMPLETED"
    echo "pre_failed: $PRE_FAILED"
    echo "pre_pending: $PRE_PENDING"
    echo "pre_recent_24h_syncs: $PRE_RECENT"
    echo "pre_total_items: $PRE_ITEMS"
    echo ""
    echo "# ── DB post-check ──"
    echo "post_total_meetings: $POST_TOTAL"
    echo "post_completed: $POST_COMPLETED"
    echo "post_failed: $POST_FAILED"
    echo "post_pending: $POST_PENDING"
    echo "post_recent_24h_syncs: $POST_RECENT"
    echo "post_total_items: $POST_ITEMS"
    echo ""
    echo "# ── Changes during this run ──"
    echo "meetings_synced_this_run: $POST_SYNCED"
    echo "new_meetings_discovered: $POST_NEW_MEETINGS"
    echo "new_agenda_items: $POST_NEW_ITEMS"
    echo ""
    echo "# ── Computed deltas ──"
    echo "delta_total_meetings: $((POST_TOTAL - PRE_TOTAL))"
    echo "delta_completed: $((POST_COMPLETED - PRE_COMPLETED))"
    echo "delta_items: $((POST_ITEMS - PRE_ITEMS))"

} > "$SUMMARY_FILE"

# ── Step 6: Doc availability check ─────────────────────────────────────────
# Lightweight probe for Tempe OnBase meetings that may have newly-published
# item-level supporting documents.  Only runs if the main sync succeeded.
if [ "$COMPLETION_STATUS" != "failed" ]; then
    log_info "Seeding doc check for newly-synced Tempe meetings..."
    # Seed next_doc_check_at for Tempe meetings that:
    #   - have items extracted but no supporting docs
    #   - are from the last 30 days
    #   - don't already have next_doc_check_at set
    #   - aren't known doc-less types (Executive, Cancelled)
    $PYTHON -c "
import sys; sys.path.insert(0, '.')
from db import get_session
from db.models import Meeting
from sqlalchemy import or_
from datetime import date, datetime, timedelta, timezone

session = get_session()
now = datetime.now(timezone.utc)
today = date.today()
thirty_days_ago = today - timedelta(days=30)

rows = session.query(Meeting).filter(
    Meeting.body.like('tempe%'),
    Meeting.items_extracted == True,
    Meeting.supporting_docs_extracted == False,
    Meeting.next_doc_check_at.is_(None),
    Meeting.meeting_date >= thirty_days_ago.isoformat(),
    Meeting.sync_status.in_(['complete', 'pending']),
).all()

skip_types = ['executive', 'cancelled', 'canceled']
seeded = 0
for m in rows:
    mt = (m.meeting_type or '').lower()
    if any(s in mt for s in skip_types):
        continue
    # Future meeting: check 1 day after meeting date
    # Past meeting: check in 2 days
    try:
        md = date.fromisoformat(m.meeting_date) if m.meeting_date else today
    except (ValueError, TypeError):
        md = today
    if md >= today:
        m.next_doc_check_at = datetime(md.year, md.month, md.day, tzinfo=timezone.utc)
    else:
        m.next_doc_check_at = now + timedelta(days=2)
    seeded += 1

session.commit()
session.close()
print(f'Seeded {seeded} meeting(s) for doc check')
" 2>&1 || true

    log_info "Checking for newly-available supporting documents..."
    DOC_CHECK_OUTPUT="$($PYTHON scripts/doc_check.py --apply 2>&1)" || true
    echo "$DOC_CHECK_OUTPUT" | while IFS= read -r line; do log_info "  $line"; done

    # ── Step 7: Extract text from newly scraped documents ───────────────────
    log_info "Running text extraction for newly scraped documents..."
    EXTRACT_LOG="$LOG_DIR/$DATE_STAMP-extract.log"
    $PYTHON -u "$PROJECT_ROOT/scripts/docs/downloader.py" \
        --workers 5 --limit 500 2>&1 | tee "$EXTRACT_LOG"
    log_info "Text extraction complete. Log → $EXTRACT_LOG"
fi

# ── Step 8: Clean up old logs ──────────────────────────────────────────────
log_info "Cleaning up logs older than $LOG_RETENTION_DAYS days..."
find "$LOG_DIR" -name '*.log.gz' -type f -mtime +$LOG_RETENTION_DAYS -exec rm -v {} \;
find "$LOG_DIR" -name '*-summary.txt' -type f -mtime +$LOG_RETENTION_DAYS -exec rm -v {} \;

# ── Final output ───────────────────────────────────────────────────────────
log_info "=== Sync complete ==="
echo "  Status:     $COMPLETION_STATUS (exit code $SYNC_EXIT)"
echo "  Duration:   ${SYNC_DURATION}s"
echo "  Errors:     $ERROR_COUNT"
echo "  Meetings:   $POST_TOTAL total (Δ $((POST_TOTAL - PRE_TOTAL)))"
echo "  Completed:  $POST_COMPLETED (Δ $((POST_COMPLETED - PRE_COMPLETED)))"
echo "  Synced now: $POST_SYNCED"
echo "  New meets:  $POST_NEW_MEETINGS"
echo "  New items:  $POST_NEW_ITEMS"
echo "  Full log:   $LOG_FILE"
echo "  Summary:    $SUMMARY_FILE"

exit $SYNC_EXIT
