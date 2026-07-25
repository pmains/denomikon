#!/usr/bin/env bash
# router-send.sh — Router-compatible send step for newsletter
#
# Usage:
#   bash scripts/newsletter/router-send.sh --run-dir <run-dir>
#
# Sends verified staged reports via the compiler's --send-staged mode.
# Respects idempotency so reports are only sent once per day.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
COMPILER="$PROJECT_ROOT/scripts/reports/compiler.py"
IDEMPOTENCY_FILE="$PROJECT_ROOT/data/newsletter/sent.idempotency"

# ── Parse args ──
RUN_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --run-dir) RUN_DIR="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [ -z "$RUN_DIR" ]; then
    echo "ERROR: --run-dir is required"
    exit 1
fi

# Resolve full path
if [[ "$RUN_DIR" != /* ]]; then
    RUN_DIR="$PROJECT_ROOT/$RUN_DIR"
fi

VERIFIED_DIR="$RUN_DIR/verified"
STAGED_DIR="$RUN_DIR/staged"
SEND_RESULT="$RUN_DIR/send-result.json"

TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

echo "[$TIMESTAMP] Send step: sending verified reports..."

# ── Derive idempotency key from run date ──
RUN_ID="$(basename "$RUN_DIR")"
# Expected format: newsletter-YYYY-MM-DD-HHMM
RUN_DATE="$(echo "$RUN_ID" | sed 's/^newsletter-//; s/-[0-9][0-9][0-9][0-9]$//')"
IDEMPOTENCY_KEY="newsletter:${RUN_DATE}"

echo "[$TIMESTAMP] Run ID: $RUN_ID"
echo "[$TIMESTAMP] Date: $RUN_DATE"
echo "[$TIMESTAMP] Idempotency key: $IDEMPOTENCY_KEY"

# ── Check idempotency ──
if [ -f "$IDEMPOTENCY_FILE" ] && grep -Fxq "$IDEMPOTENCY_KEY" "$IDEMPOTENCY_FILE" 2>/dev/null; then
    echo "[$TIMESTAMP] Already sent for $RUN_DATE — skipping."
    echo "{\"status\":\"succeeded\",\"message\":\"already sent\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$(date -u +'%Y-%m-%dT%H:%M:%SZ')\"}" > "$RUN_DIR/steps/send.state"
    exit 0
fi

# ── Collect verified files ──
VERIFIED_FILES=()
for f in "$VERIFIED_DIR"/*.json; do
    if [ -f "$f" ]; then
        VERIFIED_FILES+=("$f")
    fi
done

if [ ${#VERIFIED_FILES[@]} -eq 0 ]; then
    echo "[$TIMESTAMP] No verified files to send."
    echo "{\"status\":\"succeeded\",\"message\":\"no files to send\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$(date -u +'%Y-%m-%dT%H:%M:%SZ')\"}" > "$RUN_DIR/steps/send.state"
    exit 0
fi

echo "[$TIMESTAMP] ${#VERIFIED_FILES[@]} verified reports to send."

# ── Send each report ──
SENT_REPORTS=()
FAILED_REPORTS=()

for f in "${VERIFIED_FILES[@]}"; do
    BASENAME="$(basename "$f" .json)"
    # Extract report key from filename: YYYY-MM-DD-key → key
    REPORT_KEY="$(echo "$BASENAME" | sed 's/^[0-9-]*-//')"
    
    echo "[$TIMESTAMP] Sending report: $REPORT_KEY (file: $(basename "$f"))..."
    
    set +e
    cd "$PROJECT_ROOT"
    "$VENV_PYTHON" "$COMPILER" --send-staged --report "$REPORT_KEY" 2>&1
    SEND_EXIT=$?
    set -e
    
    if [ $SEND_EXIT -eq 0 ]; then
        SENT_REPORTS+=("$REPORT_KEY")
        echo "[$TIMESTAMP]   ✓ $REPORT_KEY sent."
    else
        FAILED_REPORTS+=("$REPORT_KEY")
        echo "[$TIMESTAMP]   ✗ $REPORT_KEY send FAILED (exit $SEND_EXIT)."
    fi
done

# ── Write send result ──
SENT_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

python3 << PYEOF
import json
import os

run_dir = "$RUN_DIR"
sent_reports = $(python3 -c "import json, sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" <<< "$(printf '%s\n' "${SENT_REPORTS[@]:-}")")
failed_reports = $(python3 -c "import json, sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" <<< "$(printf '%s\n' "${FAILED_REPORTS[@]:-}")")

result = {
    'status': 'succeeded' if not failed_reports else 'partial',
    'run_id': '${RUN_ID}',
    'idempotency_key': '${IDEMPOTENCY_KEY}',
    'reports_sent': sent_reports,
    'sent_at': '${SENT_AT}',
    'reports_failed': failed_reports,
    'already_sent': False,
}

with open(os.path.join(run_dir, 'send-result.json'), 'w') as f:
    json.dump(result, f, indent=2)

print('[${SENT_AT}] Send result written to send-result.json')
PYEOF

# ── Record idempotency (only if all succeeded) ──
FINISHED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

if [ ${#FAILED_REPORTS[@]} -eq 0 ]; then
    mkdir -p "$(dirname "$IDEMPOTENCY_FILE")"
    echo "$IDEMPOTENCY_KEY" >> "$IDEMPOTENCY_FILE"
    echo "[$FINISHED_AT] Idempotency recorded: $IDEMPOTENCY_KEY"
    echo "[$FINISHED_AT] Send step complete — all ${#SENT_REPORTS[@]} reports sent."
    echo "{\"status\":\"succeeded\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$FINISHED_AT\"}" > "$RUN_DIR/steps/send.state"
else
    echo "[$FINISHED_AT] Send step FAILED — ${#FAILED_REPORTS[@]} reports had errors."
    echo "{\"status\":\"failed\",\"error\":\"${#FAILED_REPORTS[@]} reports failed\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$FINISHED_AT\"}" > "$RUN_DIR/steps/send.state"
    exit 1
fi
