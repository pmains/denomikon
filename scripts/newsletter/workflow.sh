#!/usr/bin/env bash
# workflow.sh — Newsletter Workflow v2 (deterministic durable pipeline)
#
# Orchestrates the newsletter lifecycle: preflight → compile → send → finalize.
# The verify stage is handled by an agent session (Berry) between compile and send.
#
# Usage:
#   bash scripts/newsletter/workflow.sh --preflight-compile   # stages 1-2
#   bash scripts/newsletter/workflow.sh --send-run <run-dir>  # stages 5-6
#
# Run ID: newsletter-{YYYY}-{MM}-{DD}-{HHMM}
# All artifacts: data/runs/{run_id}/
#
# Exit codes:
#   0  → succeeded (or already sent)
#   1  → failed (log for details)
#   2  → nothing to do today (no reports due)

set -euo pipefail

# ── Config ──
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
COMPILER="$PROJECT_ROOT/scripts/reports/compiler.py"
VALIDATE_COMPILE="$PROJECT_ROOT/scripts/newsletter/validators/validate-compile.sh"
VALIDATE_VERIFY="$PROJECT_ROOT/scripts/newsletter/validators/validate-verify.sh"
IDEMPOTENCY_FILE="$PROJECT_ROOT/data/newsletter/sent.idempotency"
COMPILER_STAGING_DIR="$PROJECT_ROOT/data/reports/staging"
MAX_COMPILE_RETRIES=1
MAX_SEND_RETRIES=2
MAX_FINALIZE_RETRIES=3
RETRY_DELAY_COMPILE=60
RETRY_DELAY_SEND=30
RETRY_DELAY_FINALIZE=5

# ── Run identity (set by _setup* functions) ──
RUN_ID=""
RUN_DATE=""
RUN_DIR=""
LOG_DIR=""
STAGED_DIR=""
STATE_FILE=""
COMPILE_OUT=""
COMPILE_LOG=""
VERIFY_OUT=""
SEND_OUT=""
COMPLETED_SENTINEL=""
PIPELINE_LOG=""

# ── Helpers ──
_timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
_log()        { echo "[$(_timestamp)] $*"; }

_setup_default() {
    RUN_ID="newsletter-$(date +%Y-%m-%d-%H%M)"
    RUN_DATE="$(date +%Y-%m-%d)"
    IDEMPOTENCY_KEY="newsletter:${RUN_DATE}"
    RUN_DIR="$PROJECT_ROOT/data/runs/${RUN_ID}"
    LOG_DIR="$RUN_DIR/logs"
    STAGED_DIR="$RUN_DIR/staged"
    STATE_FILE="$RUN_DIR/state.json"
    COMPILE_OUT="$RUN_DIR/compile-result.json"
    COMPILE_LOG="$RUN_DIR/compile.log"
    VERIFY_OUT="$RUN_DIR/verify-result.json"
    SEND_OUT="$RUN_DIR/send-result.json"
    COMPLETED_SENTINEL="$RUN_DIR/completed"

    mkdir -p "$LOG_DIR" "$STAGED_DIR"

    PIPELINE_LOG="$RUN_DIR/pipeline.log"
    exec > >(tee -a "$PIPELINE_LOG") 2>&1
}

_setup_from_run_dir() {
    local dir="$1"
    RUN_DIR="$dir"
    RUN_ID="$(basename "$dir")"
    # Extract date from run ID: newsletter-YYYY-MM-DD-HHMM
    RUN_DATE="$(echo "$RUN_ID" | sed 's/newsletter-//; s/-[0-9][0-9][0-9][0-9]$//')"
    IDEMPOTENCY_KEY="newsletter:${RUN_DATE}"
    LOG_DIR="$RUN_DIR/logs"
    STAGED_DIR="$RUN_DIR/staged"
    STATE_FILE="$RUN_DIR/state.json"
    COMPILE_OUT="$RUN_DIR/compile-result.json"
    COMPILE_LOG="$RUN_DIR/compile.log"
    VERIFY_OUT="$RUN_DIR/verify-result.json"
    SEND_OUT="$RUN_DIR/send-result.json"
    COMPLETED_SENTINEL="$RUN_DIR/completed"

    PIPELINE_LOG="$RUN_DIR/pipeline.log"
    exec > >(tee -a "$PIPELINE_LOG") 2>&1
}

_write_state() {
    local key="$1"
    local value="$2"
    if [ ! -f "$STATE_FILE" ]; then
        echo '{}' > "$STATE_FILE"
    fi
    python3 -c "
import json, sys
data = json.load(open('$STATE_FILE'))
keys = '$key'.split('.')
target = data
for k in keys[:-1]:
    if k not in target or not isinstance(target[k], dict):
        target[k] = {}
    target = target[k]
target[keys[-1]] = $value
data['updated_at'] = '$(_timestamp)'
json.dump(data, open('$STATE_FILE', 'w'), indent=2)
"
}

_cleanup() {
    local exit_code=$?
    if [ -f "${STATE_FILE:-}" ]; then
        python3 -c "
import json, sys
try:
    data = json.load(open('$STATE_FILE'))
    data.setdefault('pipeline', {})['exit_code'] = $exit_code
    data['pipeline']['finished_at'] = '$(_timestamp)'
    json.dump(data, open('$STATE_FILE', 'w'), indent=2)
except Exception:
    pass
" 2>/dev/null || true
    fi
    exit $exit_code
}
trap _cleanup EXIT

# ── Stage: Preflight ──
preflight() {
    _log "=== Stage 1/6: Preflight ==="
    _write_state preflight.status '"running"'
    _write_state preflight.started_at "\"$(_timestamp)\""

    local errors=()

    # Check virtualenv
    if [ ! -f "$VENV_PYTHON" ]; then
        errors+=("Virtualenv not found at $VENV_PYTHON")
    fi

    # Check .env
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        errors+=(".env file not found")
    fi

    # Check DB connection
    if ! cd "$PROJECT_ROOT" && "$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
import os, psycopg2
try:
    conn = psycopg2.connect(os.environ.get('DATABASE_URL', os.environ.get('DEV_DATABASE_URL', '')))
    conn.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        errors+=("Database unreachable")
    fi

    # Check staging dir writable
    if ! touch "$STAGED_DIR/.write-test" 2>/dev/null; then
        errors+=("Staging directory not writable: $STAGED_DIR")
    else
        rm -f "$STAGED_DIR/.write-test"
    fi

    if [ ${#errors[@]} -gt 0 ]; then
        _log "  Preflight FAILED:"
        for e in "${errors[@]}"; do
            _log "    - $e"
        done
        _write_state preflight.status '"failed"'
        _write_state preflight.errors "$(printf '%s\n' "${errors[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
        _write_state preflight.finished_at "\"$(_timestamp)\""
        _log "  Stopping — preflight failures are non-retryable."
        exit 1
    fi

    _write_state preflight.status '"succeeded"'
    _write_state preflight.finished_at "\"$(_timestamp)\""
    _log "  Preflight passed."
}

# ── Stage: Compile + Validate ──
compile_and_validate() {
    _log "=== Stage 2/6: Compile ==="
    _write_state compile.status '"running"'
    _write_state compile.started_at "\"$(_timestamp)\""

    local attempt=0
    local max_attempts=$((MAX_COMPILE_RETRIES + 1))

    while [ $attempt -lt $max_attempts ]; do
        attempt=$((attempt + 1))
        _log "  Compile attempt $attempt/$max_attempts..."

        # Run compiler
        set +e
        cd "$PROJECT_ROOT"
        "$VENV_PYTHON" -u "$COMPILER" --due-today --stage 2>&1 | tee "$COMPILE_LOG"
        local compile_exit=${PIPESTATUS[0]}
        set -e

        _write_state compile.exit_code "$compile_exit"

        if [ $compile_exit -eq 0 ]; then
            _log "  Compiler exited 0. Running validation..."
            break
        fi

        # Check if retryable (infrastructure error vs structural)
        if grep -qi "timeout\|connection refused\|5.. server error\|provider.*error" "$COMPILE_LOG" 2>/dev/null; then
            _log "  Retryable infrastructure error detected."
            if [ $attempt -lt $max_attempts ]; then
                _log "  Waiting ${RETRY_DELAY_COMPILE}s before retry..."
                sleep "$RETRY_DELAY_COMPILE"
                continue
            fi
        fi

        _log "  Non-retryable compile failure (exit $compile_exit)."
        _write_state compile.status '"failed"'
        _write_state compile.finished_at "\"$(_timestamp)\""
        exit 1
    done

    # ── Copy staged files from compiler's staging dir to run dir ──
    local staged_files=()

    _log "  Copying staged reports to run directory..."
    for f in "$COMPILER_STAGING_DIR"/*.json; do
        if [ -f "$f" ]; then
            local basename_f; basename_f="$(basename "$f")"
            cp "$f" "$STAGED_DIR/$basename_f"
            # Extract report key (files are named DATE-key.json by compiler)
            local key; key="$(echo "$basename_f" | sed 's/^[0-9-]*-//; s/\.json$//')"
            # Check for duplicates
            local already=false
            for existing in "${staged_files[@]:-}"; do
                if [ "$existing" = "$key" ]; then
                    already=true
                    break
                fi
            done
            if [ "$already" = false ]; then
                staged_files+=("$key")
            else
                _log "    (duplicate key $key skipped)"
            fi
            _log "    Copied: $basename_f (key: $key)"
        fi
    done

    # ── Validate compile output ──
    _log "  Validating compile output..."

    # Build artifact list
    local artifact_list_json="[]"
    if [ ${#staged_files[@]} -gt 0 ]; then
        artifact_list_json="$(python3 -c "
import json, os, glob
artifacts = sorted(glob.glob(os.path.join('${STAGED_DIR}', '*.json')))
print(json.dumps(artifacts))
")"
    fi
    local reports_compiled_json
    reports_compiled_json="$(printf '%s\n' "${staged_files[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"

    # Write compile manifest
    python3 -c "
import json, os
manifest = {
    'status': 'succeeded',
    'run_id': '${RUN_ID}',
    'run_dir': '${RUN_DIR}',
    'staged_dir': '${STAGED_DIR}',
    'compile_log': '${COMPILE_LOG}',
    'reports_compiled': ${reports_compiled_json},
    'artifact_count': ${#staged_files[@]},
    'artifacts': ${artifact_list_json},
    'errors': []
}
json.dump(manifest, open('${COMPILE_OUT}', 'w'), indent=2)
print('  Manifest written: ${#staged_files[@]} artifacts')
"

    # Run validation script
    if [ -x "$VALIDATE_COMPILE" ]; then
        if ! bash "$VALIDATE_COMPILE" "$RUN_DIR" 2>&1; then
            _log "  Compile validation FAILED."
            _write_state compile.status '"validation_failed"'
            _write_state compile.finished_at "\"$(_timestamp)\""
            _write_state compile.validation_error '"Compile validation failed — artifacts are defective"'
            exit 1
        fi
        _log "  Compile validation passed."
    else
        _log "  No validate-compile script found — skipping validation."
    fi

    _write_state compile.status '"succeeded"'
    _write_state compile.artifact_count "${#staged_files[@]}"
    _write_state compile.staged_files "$(printf '%s\n' "${staged_files[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
    _write_state compile.finished_at "\"$(_timestamp)\""
    _log "  Compile complete: ${#staged_files[@]} reports staged."
}

# ── Stage: Send (idempotent) ──
send_stage() {
    _log "=== Stage 5/6: Send ==="
    _write_state send.status '"running"'
    _write_state send.started_at "\"$(_timestamp)\""

    # Check idempotency
    if [ -f "$IDEMPOTENCY_FILE" ] && grep -Fxq "$IDEMPOTENCY_KEY" "$IDEMPOTENCY_FILE" 2>/dev/null; then
        _log "  Idempotency key '$IDEMPOTENCY_KEY' already sent. Skipping."
        python3 -c "
import json
result = {
    'status': 'already_sent',
    'run_id': '${RUN_ID}',
    'idempotency_key': '${IDEMPOTENCY_KEY}',
    'reports_sent': [],
    'sent_at': null,
    'already_sent': True
}
json.dump(result, open('${SEND_OUT}', 'w'), indent=2)
"
        _write_state send.status '"already_sent"'
        _write_state send.finished_at "\"$(_timestamp)\""
        _log "  Send skipped (already sent for this date)."
        return 0
    fi

    # Read reports to send
    local reports_to_send
    reports_to_send="$(python3 -c "
import json
manifest = json.load(open('${COMPILE_OUT}'))
print(json.dumps(manifest.get('reports_compiled', [])))
")"

    local attempt=0
    local max_attempts=$((MAX_SEND_RETRIES + 1))
    local send_ok=false

    while [ $attempt -lt $max_attempts ]; do
        attempt=$((attempt + 1))
        _log "  Send attempt $attempt/$max_attempts..."

        set +e
        cd "$PROJECT_ROOT"
        local failed_reports=()
        local sent_reports=()

        for report_key in $(python3 -c "
import json
manifest = json.load(open('${COMPILE_OUT}'))
for r in manifest.get('reports_compiled', []):
    print(r)
"); do
            _log "    Sending report: $report_key"
            if "$VENV_PYTHON" "$COMPILER" --send-staged --report "$report_key" 2>&1; then
                sent_reports+=("$report_key")
                _log "    ✓ $report_key sent."
            else
                failed_reports+=("$report_key")
                _log "    ✗ $report_key send FAILED."
            fi
        done

        local send_exit=0
        if [ ${#failed_reports[@]} -gt 0 ]; then
            send_exit=1
        fi
        set -e

        if [ $send_exit -eq 0 ]; then
            send_ok=true
            break
        fi

        _log "  Send attempt $attempt had ${#failed_reports[@]} failures."
        if [ $attempt -lt $max_attempts ]; then
            _log "  Waiting ${RETRY_DELAY_SEND}s before retry..."
            sleep "$RETRY_DELAY_SEND"
        fi
    done

    # Write send result
    local sent_at="$(_timestamp)"
    if [ "$send_ok" = true ]; then
        mkdir -p "$(dirname "$IDEMPOTENCY_FILE")"
        echo "$IDEMPOTENCY_KEY" >> "$IDEMPOTENCY_FILE"
        _log "  Idempotency recorded: $IDEMPOTENCY_KEY"

        python3 -c "
import json
result = {
    'status': 'succeeded',
    'run_id': '${RUN_ID}',
    'idempotency_key': '${IDEMPOTENCY_KEY}',
    'reports_sent': $(python3 -c "import json; print(json.dumps($(printf '%s\n' "${sent_reports[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')))"),
    'sent_at': '${sent_at}',
    'already_sent': False,
    'reports_failed': $(python3 -c "import json; print(json.dumps($(printf '%s\n' "${failed_reports[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')))")
}
json.dump(result, open('${SEND_OUT}', 'w'), indent=2)
"
        _write_state send.status '"succeeded"'
        _write_state send.sent_at "\"${sent_at}\""
        _write_state send.reports_sent "$(printf '%s\n' "${sent_reports[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
        _log "  Send complete: ${#sent_reports[@]} reports sent."
    else
        python3 -c "
import json
result = {
    'status': 'failed',
    'run_id': '${RUN_ID}',
    'idempotency_key': '${IDEMPOTENCY_KEY}',
    'reports_sent': $(python3 -c "import json; print(json.dumps($(printf '%s\n' "${sent_reports[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')))"),
    'sent_at': '${sent_at}',
    'already_sent': False,
    'reports_failed': $(python3 -c "import json; print(json.dumps($(printf '%s\n' "${failed_reports[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')))")
}
json.dump(result, open('${SEND_OUT}', 'w'), indent=2)
"
        _write_state send.status '"failed"'
        _write_state send.finished_at "\"$(_timestamp)\""
        _write_state send.error '"Send failed after retries"'
        _log "  Send FAILED after $attempt attempts."
        exit 1
    fi

    _write_state send.finished_at "\"$(_timestamp)\""
}

# ── Stage: Finalize ──
finalize() {
    _log "=== Stage 6/6: Finalize ==="
    _write_state finalize.status '"running"'
    _write_state finalize.started_at "\"$(_timestamp)\""

    local attempt=0
    local max_attempts=$((MAX_FINALIZE_RETRIES + 1))

    while [ $attempt -lt $max_attempts ]; do
        attempt=$((attempt + 1))
        _log "  Finalize attempt $attempt/$max_attempts..."

        if touch "$COMPLETED_SENTINEL" 2>/dev/null; then
            _log "  Completed sentinel written: $COMPLETED_SENTINEL"

            python3 -c "
import json
summary = {
    'run_id': '${RUN_ID}',
    'run_date': '${RUN_DATE}',
    'status': 'succeeded',
    'compile_out': '${COMPILE_OUT}',
    'verify_out': '${VERIFY_OUT}',
    'send_out': '${SEND_OUT}',
    'pipeline_log': '${PIPELINE_LOG}',
    'completed_at': '$(_timestamp)'
}
print(json.dumps(summary, indent=2))
" | tee "$RUN_DIR/summary.json"

            _write_state finalize.status '"succeeded"'
            _write_state finalize.finished_at "\"$(_timestamp)\""
            _log "  Pipeline complete: ${RUN_ID}"
            return 0
        fi

        _log "  Finalize attempt $attempt failed."
        if [ $attempt -lt $max_attempts ]; then
            sleep "$RETRY_DELAY_FINALIZE"
        fi
    done

    _log "  Finalize FAILED after $attempt attempts."
    _write_state finalize.status '"failed"'
    _write_state finalize.finished_at "\"$(_timestamp)\""
    exit 1
}

# ── Mode: preflight-compile (stages 1-2) ──
_preflight_compile() {
    _log "╔═══════════════════════════════════════════╗"
    _log "║  Newsletter Workflow v2 — Compile Only    ║"
    _log "║  Run: ${RUN_ID}"
    _log "║  Dir: ${RUN_DIR}"
    _log "╚═══════════════════════════════════════════╝"
    _log ""

    _write_state pipeline.version '"workflow_v2"'
    _write_state pipeline.run_id "\"${RUN_ID}\""
    _write_state pipeline.run_date "\"${RUN_DATE}\""
    _write_state pipeline.started_at "\"$(_timestamp)\""
    _write_state pipeline.status '"compiling"'

    # Check run-level idempotency
    if [ -f "$IDEMPOTENCY_FILE" ] && grep -Fxq "$IDEMPOTENCY_KEY" "$IDEMPOTENCY_FILE" 2>/dev/null; then
        _log "  Newsletters already sent for $RUN_DATE. Nothing to do."
        _write_state pipeline.status '"already_sent"'
        _write_state pipeline.finished_at "\"$(_timestamp)\""
        # Print run_dir so the agent knows it's done
        echo "ALREADY_SENT:$RUN_DATE"
        exit 0
    fi

    preflight
    compile_and_validate

    # Quick exit if no reports due
    local artifact_count
    artifact_count="$(python3 -c "import json; print(json.load(open('${COMPILE_OUT}')).get('artifact_count', 0))" 2>/dev/null || echo 0)"
    if [ "$artifact_count" -eq 0 ]; then
        _log "  No reports compiled — nothing to send today."
        _write_state pipeline.status '"no_reports"'
        _write_state pipeline.finished_at "\"$(_timestamp)\""
        touch "$COMPLETED_SENTINEL" 2>/dev/null || true
        echo "NO_REPORTS"
        exit 2
    fi

    _write_state pipeline.status '"compile_done"'
    _write_state pipeline.finished_at "\"$(_timestamp)\""

    # Output the run directory as the last line — agent captures this
    echo "RUN_DIR:$RUN_DIR"
}

# ── Mode: send-run (stages 5-6) ──
_send_run() {
    local target_dir="$1"

    if [ ! -d "$target_dir" ]; then
        echo "Run directory not found: $target_dir" >&2
        exit 1
    fi

    # Verify that compile-result.json exists (confirm preflight-compile was done)
    if [ ! -f "$target_dir/compile-result.json" ]; then
        echo "No compile-result.json in $target_dir — compile stage not complete." >&2
        exit 1
    fi

    # Verify that verify-result.json exists (confirm Berry verification was done)
    if [ ! -f "$target_dir/verify-result.json" ]; then
        echo "No verify-result.json in $target_dir — verification stage not complete." >&2
        exit 1
    fi

    _log "╔═══════════════════════════════════════════╗"
    _log "║  Newsletter Workflow v2 — Send & Finalize ║"
    _log "║  Run: $(basename "$target_dir")"
    _log "║  Dir: ${target_dir}"
    _log "╚═══════════════════════════════════════════╝"
    _log ""

    _write_state pipeline.status '"sending"'

    # Validate verify result before sending
    if [ -x "$VALIDATE_VERIFY" ]; then
        if ! bash "$VALIDATE_VERIFY" "$target_dir" "$target_dir/compile-result.json" "$target_dir/verify-result.json" 2>&1; then
            _log "  Verification validation FAILED — aborting send."
            _write_state pipeline.status '"verify_validation_failed"'
            _write_state pipeline.finished_at "\"$(_timestamp)\""
            exit 1
        fi
        _log "  Verification validation passed."
    else
        _log "  No validate-verify script found — skipping validation."
    fi

    send_stage
    finalize

    _log "╔═══════════════════════════════════════════╗"
    _log "║  Newsletter Workflow COMPLETE             ║"
    _log "╚═══════════════════════════════════════════╝"
}

# ── Entry Point ──
case "${1:-}" in
    --preflight-compile)
        _setup_default
        _preflight_compile
        ;;
    --send-run)
        if [ -z "${2:-}" ]; then
            echo "Usage: $0 --send-run <run-dir>" >&2
            exit 1
        fi
        _setup_from_run_dir "$2"
        _send_run "$2"
        ;;
    --help|-h)
        echo "Usage:"
        echo "  $0 --preflight-compile       # Compile reports (stages 1-2)"
        echo "  $0 --send-run <run-dir>      # Send & finalize a verified run (stages 5-6)"
        echo ""
        echo "The verify stage (3-4) is handled by the agent between these two."
        exit 0
        ;;
    *)
        echo "Usage: $0 --preflight-compile | --send-run <run-dir>" >&2
        echo "  Use --help for details." >&2
        exit 1
        ;;
esac
