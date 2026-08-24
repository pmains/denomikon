#!/bin/bash
# sync_prod.sh — Deploy to production as a background job
# Usage: ./scripts/sync_prod.sh [--code-only]
#   1. Pre-check: verify today's daily scrape completed successfully
#   2. If pre-check passes, launch sync.sh in the background via nohup
#   3. Exit immediately (caller gets back in <1 second)
# Logs: data/sync/prod-sync-YYYY-MM-DD.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

LOG_DIR="data/sync"
DATE_STAMP=$(date "+%Y-%m-%d")
SUMMARY_FILE="$LOG_DIR/$DATE_STAMP-summary.txt"
LOG_FILE="$LOG_DIR/prod-sync-$DATE_STAMP.log"
mkdir -p "$LOG_DIR"

# Pre-check: daily scrape must have completed successfully
if [ ! -f "$SUMMARY_FILE" ]; then
    echo "ERROR: No scrape summary for $DATE_STAMP (expected: $SUMMARY_FILE)"
    echo "  Run sync_log.sh (daily scrape) first."
    exit 2
fi
if ! grep -q '^completion_status: success$' "$SUMMARY_FILE"; then
    echo "ERROR: Today's scrape did not complete successfully."
    echo "  See: $SUMMARY_FILE"
    exit 2
fi

# Launch sync.sh in background and exit immediately
echo "Pre-check passed. Launching sync.sh in background..."
echo "  Log: $LOG_FILE"

set +euo pipefail
nohup bash "$PROJECT_ROOT/sync.sh" "${@:-}" > "$LOG_FILE" 2>&1 &
PID=$!
disown
set -euo pipefail

echo "  PID: $PID"
echo "sync_prod.sh launched (pid $PID). Check $LOG_FILE for progress."
exit 0
