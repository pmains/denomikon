#!/bin/bash
# =============================================================================
# sync_report.sh — Read the latest sync monitor report
#
# Usage:
#   ./scripts/sync_report.sh                  # Latest day's report
#   ./scripts/sync_report.sh 2026-06-03       # Specific date
#   ./scripts/sync_report.sh --json           # JSON output (latest)
#   ./scripts/sync_report.sh 2026-06-03 --json
#
# If the monitor report doesn't exist for the requested date, runs
# sync_monitor.py --report-only to generate it from existing DB data.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# ── Parse args ─────────────────────────────────────────────────────────────
DATE_ARG=""
JSON_MODE=false

for arg in "$@"; do
    case "$arg" in
        --json)
            JSON_MODE=true
            ;;
        *)
            if [[ "$DATE_ARG" == "" ]]; then
                DATE_ARG="$arg"
            fi
            ;;
    esac
done

# ── Determine date ─────────────────────────────────────────────────────────
if [[ "$DATE_ARG" != "" ]]; then
    DATE_STAMP="$DATE_ARG"
else
    DATE_STAMP=$(date "+%Y-%m-%d")
fi

# ── File paths ─────────────────────────────────────────────────────────────
LOG_DIR="$PROJECT_ROOT/data/sync"
MONITOR_FILE="$LOG_DIR/${DATE_STAMP}-monitor.txt"

# ── Generate report if missing ─────────────────────────────────────────────
if [[ ! -f "$MONITOR_FILE" ]]; then
    if [[ "$DATE_STAMP" != "$(date '+%Y-%m-%d')" ]]; then
        echo "No monitor report exists for $DATE_STAMP (reports are date-stamped to when they are generated)."
        echo "Try without a date to see today's report, or run sync_monitor.py first."
        exit 1
    fi
    echo "Monitor report for $DATE_STAMP not found. Generating from database..."
    POLISCOPIC_DB_TIER="${POLISCOPIC_DB_TIER:-development}" \
    PYTHONPATH="${PYTHONPATH:-$PROJECT_ROOT/scripts}" \
    "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/sync_monitor.py" --report-only --quiet 2>&1
    echo "---"
    echo ""
fi

# ── Output ─────────────────────────────────────────────────────────────────
if $JSON_MODE; then
    # JSON output: convert the monitor text into a simple structured JSON
    if [[ ! -f "$MONITOR_FILE" ]]; then
        echo '{"error": "Report not found and could not be generated."}'
        exit 1
    fi

    # Simple structured JSON extraction — extract first pure integer after colon
    _val() { grep "$1" "$MONITOR_FILE" | sed 's/.*: *//;s/ .*//;s/[^0-9]//g'; }
    TOTAL=$(_val '^  Total meetings:')
    COMPLETE=$(_val '^  Complete:')
    PENDING=$(_val '^  Pending:')
    FAILED_ALL=$(_val '^  Failed (all time):')
    FAILED_24H=$(_val '^  Failed (last 24h):')
    STUCK=$(_val '^  Stuck')
    ORPHANS=$(_val '^  Orphans')

    # Get the report date from the header line
    HEADER=$(head -1 "$MONITOR_FILE")
    REPORT_DATE="${HEADER#=== Sync Monitor Report — }"
    REPORT_DATE="${REPORT_DATE% ===}"

    # Build the JSON
    cat <<JSON
{
  "report_date": "$REPORT_DATE",
  "overall": {
    "total_meetings": ${TOTAL:-0},
    "complete": ${COMPLETE:-0},
    "pending": ${PENDING:-0},
    "failed_all_time": ${FAILED_ALL:-0},
    "failed_last_24h": ${FAILED_24H:-0},
    "stuck_in_progress": ${STUCK:-0},
    "orphans": ${ORPHANS:-0}
  },
  "monitor_file": "$MONITOR_FILE"
}
JSON
else
    # Text output
    if [[ ! -f "$MONITOR_FILE" ]]; then
        echo "Monitor report for $DATE_STAMP not found."
        echo "Run 'POLISCOPIC_DB_TIER=development PYTHONPATH=scripts .venv/bin/python scripts/sync_monitor.py' first."
        exit 1
    fi

    cat "$MONITOR_FILE"
fi
