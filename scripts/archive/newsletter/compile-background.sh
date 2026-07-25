#!/usr/bin/env bash
# compile-background.sh — Backgrounds the newsletter compile and records run info
#
# Cron entry point. Backgrounds workflow.sh --preflight-compile via nohup,
# writes the run directory to a known status file so the verify-send cron
# can find it later. Exits immediately (< 1 second).
#
# Usage:
#   bash scripts/newsletter/compile-background.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATUS_DIR="$PROJECT_ROOT/data/newsletter"
STATUS_FILE="$STATUS_DIR/compile-status.json"
WORKFLOW_SCRIPT="$SCRIPT_DIR/workflow.sh"
TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
RUN_DATE="$(date +%Y-%m-%d)"

mkdir -p "$STATUS_DIR"

# Check idempotency — skip if already sent today
IDEMPOTENCY_FILE="$PROJECT_ROOT/data/newsletter/sent.idempotency"
IDEMPOTENCY_KEY="newsle…TE}"
if [ -f "$IDEMPOTENCY_FILE" ] && grep -Fxq "$IDEMPOTENCY_KEY" "$IDEMPOTENCY_FILE" 2>/dev/null; then
    echo '{"status":"already_sent","run_date":"'"$RUN_DATE"'","message":"Newsletter already sent for today"}'
    echo '{"status":"already_sent","run_date":"'"$RUN_DATE"'","message":"Newsletter already sent for today"}' > "$STATUS_FILE"
    exit 0
fi

# Run identity (matches workflow.sh --preflight-compile)
RUN_ID="newsletter-$(date +%Y-%m-%d-%H%M)"
RUN_DIR="$PROJECT_ROOT/data/runs/${RUN_ID}"
mkdir -p "$RUN_DIR"

# Write pending status
python3 -c "
import json
status = {
    'status': 'compiling',
    'run_id': '${RUN_ID}',
    'run_date': '${RUN_DATE}',
    'run_dir': '${RUN_DIR}',
    'started_at': '${TIMESTAMP}',
    'compile_log': '${RUN_DIR}/pipeline.log',
    'compile_result': '${RUN_DIR}/compile-result.json'
}
json.dump(status, open('${STATUS_FILE}', 'w'), indent=2)
print(json.dumps(status))
" | tee "$RUN_DIR/status.json"

# Background the compile with nohup (Serenity philosophy)
cd "$PROJECT_ROOT"
nohup bash "$WORKFLOW_SCRIPT" --preflight-compile > "$RUN_DIR/pipeline.log" 2>&1 &
echo $! > "$RUN_DIR/compile.pid"

echo "Compile backgrounded (PID: $(cat "$RUN_DIR/compile.pid"))"
echo "Run dir: $RUN_DIR"
