#!/bin/bash
# =============================================================================
# sync_checker.sh — Check if the daily sync finished, run analysis, re-run if
#                   issues found.
#
# Called periodically (every 15-30 minutes) by a cron job. This is the
# "brain" of the fire-and-forget pipeline:
#
#   1. Check if today's sync is still running, finished, or never launched.
#   2. If running → exit quietly (not done yet).
#   3. If finished → run sync_monitor.py (analysis + auto-remediation).
#   4. Read the monitor report to determine if re-run is needed.
#   5. If issues remain → re-launch the sync (loop back to launcher).
#   6. If clean → write analysis marker and report.
#
# This script is designed to be run from an OpenClaw isolated cron session.
# It should execute in < 10 seconds unless analysis is running (~5s).
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/data/sync"

# ── Python interpreter ─────────────────────────────────────────────────────
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
else
    PYTHON="${PYTHON:-python3}"
fi
export PYTHONPATH="${PYTHONPATH:-$PROJECT_ROOT/scripts}"
export POLISCOPIC_DB_TIER="${POLISCOPIC_DB_TIER:-development}"

DATE_STAMP=$(date "+%Y-%m-%d")
PID_FILE="$LOG_DIR/$DATE_STAMP.sync.pid"
LAUNCHED_FILE="$LOG_DIR/$DATE_STAMP.launched"
COMPLETE_MARKER="$LOG_DIR/$DATE_STAMP.complete"
ANALYSIS_MARKER="$LOG_DIR/$DATE_STAMP.analyzed"
SUMMARY_FILE="$LOG_DIR/$DATE_STAMP-summary.txt"
MONITOR_FILE="$LOG_DIR/$DATE_STAMP-monitor.txt"
LAUNCHER_LOG="$LOG_DIR/$DATE_STAMP.launcher.log"

# ══════════════════════════════════════════════════════════════════════════════
# Helper: check if the sync process is still alive
# ══════════════════════════════════════════════════════════════════════════════

is_sync_running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    EXISTING_PID=$(cat "$PID_FILE")
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        return 0
    fi
    # Stale PID — clean up
    rm -f "$PID_FILE"
    return 1
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: determine if the sync was successful
# ══════════════════════════════════════════════════════════════════════════════

get_sync_result() {
    if [ ! -f "$SUMMARY_FILE" ]; then
        echo "no_summary"
        return
    fi
    STATUS=$(grep '^completion_status:' "$SUMMARY_FILE" | awk '{print $2}')
    EXIT_CODE=$(grep '^exit_code:' "$SUMMARY_FILE" | awk '{print $2}')
    echo "${STATUS:-unknown} (exit $EXIT_CODE)"
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: determine if the sync found any new data
# ══════════════════════════════════════════════════════════════════════════════

sync_had_problems() {
    if [ ! -f "$SUMMARY_FILE" ]; then
        return 0  # no summary = problem
    fi
    STATUS=$(grep '^completion_status:' "$SUMMARY_FILE" | awk '{print $2}')
    if [ "$STATUS" = "failed" ]; then
        return 0  # problem
    fi
    ERRORS=$(grep '^error_count:' "$SUMMARY_FILE" | awk '{print $2}')
    if [ "${ERRORS:-0}" -gt 5 ]; then
        return 0  # too many errors
    fi
    return 1  # no problems
}

# ══════════════════════════════════════════════════════════════════════════════
# Helper: check if analysis says re-run is needed
# ══════════════════════════════════════════════════════════════════════════════

analysis_needs_rerun() {
    if [ ! -f "$MONITOR_FILE" ]; then
        return 0  # no analysis = run it
    fi
    # Check for un-remediated failures
    if grep -q "Remaining Issues.*human attention" "$MONITOR_FILE"; then
        local remaining_section=false
        while IFS= read -r line; do
            if [[ "$line" == *"Remaining Issues (require human attention):"* ]]; then
                remaining_section=true
                continue
            fi
            if $remaining_section; then
                if [[ "$line" =~ ^[[:space:]]*\(none ]]; then
                    return 1  # no remaining issues
                fi
                if [[ "$line" =~ ^[[:space:]]*• ]]; then
                    return 0  # has issues
                fi
                # End of section (next blank-section transition)
                if [[ "$line" == "" ]]; then
                    return 1
                fi
            fi
        done < "$MONITOR_FILE"
    fi
    # Check for failed status in last 24h
    if grep -q "Failed (last 24h): [1-9]" "$MONITOR_FILE"; then
        return 0  # has recent failures
    fi
    return 1  # no re-run needed
}

# ══════════════════════════════════════════════════════════════════════════════
# Main logic
# ══════════════════════════════════════════════════════════════════════════════

echo "=== Sync Checker — $DATE_STAMP ==="
echo ""

# ── Case 1: Sync is still running ─────────────────────────────────────────
if is_sync_running; then
    SYNC_PID=$(cat "$PID_FILE")
    echo "Sync is still running (PID $SYNC_PID). Nothing to do yet."
    echo "Will check again on next cycle."
    exit 0
fi

# ── Case 2: Never launched today (no launcher file, no summary) ───────────
if [ ! -f "$LAUNCHED_FILE" ] && [ ! -f "$SUMMARY_FILE" ]; then
    echo "No sync launched today. Checking time..."
    HOUR=$(date +%H)
    if [ "$HOUR" -ge 5 ] && [ "$HOUR" -lt 12 ]; then
        echo "It's between 5 AM and noon — launching sync now."
        bash "$PROJECT_ROOT/scripts/sync_launcher.sh"
        exit 0
    else
        echo "Outside sync window (5 AM - noon). Skipping."
        exit 0
    fi
fi

# ── Case 3: Launched but not yet complete (no summary) ────────────────────
if [ -f "$LAUNCHED_FILE" ] && [ ! -f "$SUMMARY_FILE" ]; then
    echo "Launched but sync hasn't written summary yet."
    if is_sync_running; then
        echo "Process still running — waiting for completion."
    else
        echo "Process ended but no summary found — sync may have crashed."
        echo "Checking launcher log for clues..."
        if [ -f "$LAUNCHER_LOG" ]; then
            tail -5 "$LAUNCHER_LOG"
        fi
        # Re-launch
        echo "Re-launching sync due to apparent crash."
        rm -f "$LAUNCHED_FILE" "$PID_FILE"
        bash "$PROJECT_ROOT/scripts/sync_launcher.sh"
    fi
    exit 0
fi

# ── Case 4: Sync complete, run analysis ───────────────────────────────────
if [ -f "$SUMMARY_FILE" ] && [ ! -f "$ANALYSIS_MARKER" ]; then
    RESULT=$(get_sync_result)
    echo "Sync complete! Result: $RESULT"
    echo ""
    echo "Running analysis (sync_monitor.py)..."
    $PYTHON "$PROJECT_ROOT/scripts/sync_monitor.py" 2>&1
    MONITOR_EXIT=$?
    echo ""
    if [ $MONITOR_EXIT -eq 0 ] && [ -f "$MONITOR_FILE" ]; then
        echo "Analysis complete → $MONITOR_FILE"
    else
        echo "Analysis had issues (exit $MONITOR_EXIT)"
    fi
    echo ""

    # Check if analysis says re-run
    if analysis_needs_rerun; then
        echo "Analysis indicates remaining issues — re-launching sync."
        rm -f "$LAUNCHED_FILE" "$PID_FILE" "$COMPLETE_MARKER"
        bash "$PROJECT_ROOT/scripts/sync_launcher.sh"
    else
        echo "Analysis clean — marking as analyzed."
        date -Iseconds > "$ANALYSIS_MARKER"
        echo "Done for today. Final report:"
        echo ""
        cat "$SUMMARY_FILE"
        echo ""
        echo "=== Summary ==="
        echo "  Meetings: $(grep '^post_total_meetings:' "$SUMMARY_FILE" | awk '{print $2}')"
        echo "  Completed: $(grep '^post_completed:' "$SUMMARY_FILE" | awk '{print $2}')"
        echo "  New items: $(grep '^new_agenda_items:' "$SUMMARY_FILE" | awk '{print $2}')"
        echo "  New meets: $(grep '^new_meetings_discovered:' "$SUMMARY_FILE" | awk '{print $2}')"
    fi
    exit 0
fi

# ── Case 5: Already analyzed — nothing more to do ─────────────────────────
if [ -f "$ANALYSIS_MARKER" ]; then
    echo "Sync already analyzed today. All done."
    exit 0
fi

# ── Fallback ──────────────────────────────────────────────────────────────
echo "Unknown state. Files present:"
ls -la "$PID_FILE" "$LAUNCHED_FILE" "$SUMMARY_FILE" "$MONITOR_FILE" "$ANALYSIS_MARKER" 2>&1 || true
exit 0
