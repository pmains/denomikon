#!/usr/bin/env bash
# pipeline.sh — Newsletter pipeline state machine
#
# Manages the newsletter compile → verify → send lifecycle through a JSON
# state file at data/newsletter/state.json.
#
# Subcommands:
#   init                    Create fresh state file for today
#   compile                 Background the compiler via task_utils
#   check-compile           Check if compile finished
#   verify [--force]        Trigger the verify-send cron job
#   complete-verify <outcome>  Mark verify as completed|failed
#   reconcile               Full state-machine reconciliation
#   status                  Pretty-print current state
#   reset                   Remove state file (debug/testing only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATE_FILE="$PROJECT_ROOT/data/newsletter/state.json"
TODAY="$(date +%Y-%m-%d)"

# ── Timestamp helper (ISO 8601 UTC) ──
_timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# ── Ensure state directory exists ──
_ensure_dir() {
    mkdir -p "$(dirname "$STATE_FILE")"
}

# ── Read state file; output JSON string ──
_read_state() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "{}"
        return
    fi
    python3 -c "import json; print(json.dumps(json.load(open('$STATE_FILE'))))" 2>/dev/null || echo "{}"
}

# ── Write entire state file ──
_write_state() {
    _ensure_dir
    local tmpfile; tmpfile="$(mktemp)"
    cat > "$tmpfile"
    # Inject updated_at
    python3 -c "
import json, sys
data = json.load(sys.stdin)
data['updated_at'] = '$(_timestamp)'
json.dump(data, open('$tmpfile', 'w'), indent=2)
" < "$tmpfile"
    mv "$tmpfile" "$STATE_FILE"
}

# ── Translate JSON literals to Python literals for code injection ──
_to_py_val() {
    local val="$1"
    case "$val" in
        null)  echo "None"  ;;
        true)  echo "True"  ;;
        false) echo "False" ;;
        *)     echo "$val"  ;;
    esac
}

# ── Update a single key in the state JSON ──
# Usage: _update_state compile.status '"running"'
# The value arg should be a JSON literal (string, number, bool, null)
_update_state() {
    _ensure_dir
    local key="$1"
    local value="$2"
    local py_value; py_value="$(_to_py_val "$value")"

    # Verify the value round-trips by parsing+re-serializing
    python3 -c "
import json, os

path = '$STATE_FILE'
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)

keys = '$key'.split('.')
target = data
for k in keys[:-1]:
    if k not in target or not isinstance(target[k], dict):
        target[k] = {}
    target = target[k]
target[keys[-1]] = $py_value

data['updated_at'] = '$(_timestamp)'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
" 2>&1
}

# ── Get a value from state ──
# Usage: _get_state compile.status → prints value or empty string
_get_state() {
    local key="$1"
    if [ ! -f "$STATE_FILE" ]; then
        echo ""
        return
    fi
    python3 -c "
import json, sys
data = json.load(open('$STATE_FILE'))
keys = '$key'.split('.')
target = data
for k in keys:
    if isinstance(target, dict) and k in target:
        target = target[k]
    else:
        print('')
        sys.exit(0)
if target is None:
    print('null')
elif isinstance(target, bool):
    print(str(target).lower())
elif isinstance(target, (int, float)):
    print(target)
elif isinstance(target, list):
    # Print JSON array
    print(json.dumps(target))
else:
    print(target)
" 2>/dev/null || echo ""
}

# ── Subcommand: init ──
# Create a fresh state file for today. Error if one already exists.
cmd_init() {
    if [ -f "$STATE_FILE" ]; then
        echo "ERROR: State file already exists at $STATE_FILE"
        echo "       Use 'reset' to remove it, or work with the existing state."
        exit 1
    fi

    _ensure_dir
    cat > "$STATE_FILE" <<-ENDJSON
{
  "run_date": "$TODAY",
  "compile": {
    "status": "pending",
    "pid": null,
    "started_at": null,
    "finished_at": null,
    "exit_code": null,
    "log_path": null
  },
  "verify": {
    "status": "idle",
    "attempts": 0,
    "max_attempts": 3,
    "last_attempt_at": null,
    "last_attempt_outcome": null
  },
  "send": {
    "status": "pending",
    "reports_sent": [],
    "reports_failed": [],
    "report_count": 0
  },
  "reconciliation": {
    "last_checked_at": null,
    "escalated": false,
    "escalated_at": null,
    "error": null
  },
  "updated_at": "$(_timestamp)"
}
ENDJSON
    echo "State file created for $TODAY"
}

# ── Subcommand: compile ──
# Background the compiler via task_utils
cmd_compile() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "ERROR: No state file found. Run 'init' first."
        exit 1
    fi

    local current_status; current_status="$(_get_state compile.status)"
    if [ "$current_status" = "running" ]; then
        echo "Compile already running. (Idempotent — doing nothing.)"
        exit 0
    fi

    # Update state
    _update_state compile.status '"running"'
    _update_state compile.started_at "\"$(_timestamp)\""
    _update_state compile.pid null

    # Background via task_utils
    echo "Starting newsletter-compile via task_utils..."
    set +e
    cd "$PROJECT_ROOT"
    bash scripts/task_utils.sh run newsletter-compile '.venv/bin/python scripts/reports/compiler.py --due-today --stage' 2>&1
    local tu_exit=$?
    set -e

    if [ $tu_exit -ne 0 ]; then
        echo "task_utils run failed (exit $tu_exit). Recording error."
        _update_state compile.status '"failed"'
        _update_state compile.exit_code "$tu_exit"
        _update_state compile.finished_at "\"$(_timestamp)\""
        # Don't exit non-zero per spec (idempotent)
        return
    fi

    # Record PID from the task_utils PID file
    local pid_file="$PROJECT_ROOT/data/tasks/newsletter-compile/$TODAY.pid"
    if [ -f "$pid_file" ]; then
        local pid; pid="$(cat "$pid_file")"
        _update_state compile.pid "$pid"
        echo "Compile backgrounded (PID $pid)"
    else
        echo "No PID file found — compile may be already done or started by another process."
    fi
}

# ── Subcommand: check-compile ──
# Check if compile finished via task_utils
cmd_check_compile() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "No state file — nothing to check."
        exit 0
    fi

    local current_status; current_status="$(_get_state compile.status)"

    if [ "$current_status" = "completed" ]; then
        echo "Compile already completed."
        exit 0
    fi

    if [ "$current_status" != "running" ]; then
        echo "Compile not running (status: $current_status). Nothing to check."
        exit 0
    fi

    cd "$PROJECT_ROOT"
    set +e
    bash scripts/task_utils.sh check newsletter-compile 2>&1
    local tu_exit=$?
    set -e

    if [ $tu_exit -eq 0 ]; then
        # Succeeded
        _update_state compile.status '"completed"'
        _update_state compile.finished_at "\"$(_timestamp)\""
        _update_state compile.exit_code 0

        # Also grab log path
        local log_file="$PROJECT_ROOT/data/tasks/newsletter-compile/$TODAY.log"
        if [ -f "$log_file" ]; then
            _update_state compile.log_path "\"$log_file\""
        fi

        echo "Compile completed successfully."
        exit 0
    fi

    # tu_exit == 1 — could be still running or failed
    # Distinguish by reading the task_utils status file
    local status_file="$PROJECT_ROOT/data/tasks/newsletter-compile/$TODAY.status"
    if [ -f "$status_file" ]; then
        local task_status; task_status="$(python3 -c "import json; print(json.load(open('$status_file')).get('status',''))" 2>/dev/null || echo '')"
        local exit_code; exit_code="$(python3 -c "import json; print(json.load(open('$status_file')).get('exit_code',''))" 2>/dev/null || echo '')"

        if [ "$task_status" = "failed" ]; then
            _update_state compile.status '"failed"'
            _update_state compile.finished_at "\"$(_timestamp)\""
            _update_state compile.exit_code "${exit_code:-1}"

            local log_file="$PROJECT_ROOT/data/tasks/newsletter-compile/$TODAY.log"
            if [ -f "$log_file" ]; then
                _update_state compile.log_path "\"$log_file\""
            fi

            echo "Compile failed (exit code ${exit_code:-1})."
            exit 0
        fi

        if [ "$task_status" = "retrying" ]; then
            echo "Compile failed, task_utils will retry (exit code ${exit_code:-1})."
            exit 0
        fi
    fi

    echo "Compile still running."
    exit 0
}

# ── Subcommand: verify [--force] ──
# Trigger the verify-send cron job
cmd_verify() {
    local force=false
    if [ "${1:-}" = "--force" ]; then
        force=true
    fi

    if [ ! -f "$STATE_FILE" ]; then
        echo "ERROR: No state file found. Nothing to verify."
        exit 1
    fi

    local compile_status; compile_status="$(_get_state compile.status)"
    if [ "$compile_status" != "completed" ]; then
        echo "ERROR: Compile must be completed before verify can run."
        echo "       Current compile.status = $compile_status"
        exit 1
    fi

    # Check attempts
    local attempts; attempts="$(_get_state verify.attempts)"
    local max_attempts; max_attempts="$(_get_state verify.max_attempts)"
    attempts="${attempts:-0}"
    max_attempts="${max_attempts:-3}"

    if [ "$attempts" -ge "$max_attempts" ] 2>/dev/null; then
        echo "ERROR: Verify retries exhausted ($attempts/$max_attempts attempts used)."
        echo "       Pipeline has escalated."
        exit 1
    fi

    local verify_status; verify_status="$(_get_state verify.status)"
    if [ "$verify_status" = "running" ] && [ "$force" = false ]; then
        echo "Verify already running. Use --force to re-trigger."
        exit 0
    fi

    # Increment attempts
    local new_attempts=$((attempts + 1))
    _update_state verify.attempts "$new_attempts"
    _update_state verify.status '"running"'
    _update_state verify.last_attempt_at "\"$(_timestamp)\""

    echo "Triggering verify cron job (attempt $new_attempts/$max_attempts)..."

    # Find openclaw CLI
    set +e
    local oc_path
    oc_path="$(command -v openclaw 2>/dev/null)"
    set -e

    if [ -z "$oc_path" ]; then
        # Try common locations
        for candidate in \
            "$HOME/.openclaw/bin/openclaw" \
            "/usr/local/bin/openclaw" \
            "/opt/homebrew/bin/openclaw"; do
            if [ -x "$candidate" ]; then
                oc_path="$candidate"
                break
            fi
        done
    fi

    # If still not found, try npx fallback
    if [ -z "$oc_path" ]; then
        echo "openclaw CLI not found as binary, trying npx..."
        set +e
        cd "$PROJECT_ROOT"
        npx openclaw cron run f03297b7-905d-4ed4-a827-8b8739b57d84 2>&1
        local oc_exit=$?
        set -e
    else
        set +e
        cd "$PROJECT_ROOT"
        "$oc_path" cron run f03297b7-905d-4ed4-a827-8b8739b57d84 2>&1
        local oc_exit=$?
        set -e
    fi

    if [ $oc_exit -ne 0 ]; then
        echo "Verify cron job failed (exit $oc_exit). Reverting attempt."
        _update_state verify.attempts "$attempts"
        _update_state verify.status '"idle"'
        _update_state verify.last_attempt_at 'null'
        _update_state verify.last_attempt_outcome '"failed_to_trigger"'
        exit 0
    fi

    _update_state verify.last_attempt_outcome '"triggered"'
    echo "Verify cron job triggered (attempt $new_attempts/$max_attempts)."
}

# ── Subcommand: complete-verify ──
# Called BY the verify agent session after it finishes
cmd_complete_verify() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "ERROR: No state file found."
        exit 1
    fi

    local outcome="${1:-}"
    if [ -z "$outcome" ]; then
        echo "Usage: $0 complete-verify <outcome>"
        echo "  <outcome> = completed | failed"
        exit 1
    fi

    if [ "$outcome" != "completed" ] && [ "$outcome" != "failed" ]; then
        echo "ERROR: Outcome must be 'completed' or 'failed', got '$outcome'"
        exit 1
    fi

    _update_state verify.status "\"$outcome\""
    _update_state verify.last_attempt_outcome "\"$outcome\""

    # Also update last_attempt_at if not already set
    local last_at; last_at="$(_get_state verify.last_attempt_at)"
    if [ -z "$last_at" ] || [ "$last_at" = "null" ]; then
        _update_state verify.last_attempt_at "\"$(_timestamp)\""
    fi

    echo "Verify marked as $outcome."
}

# ── Subcommand: reconcile ──
# Full idempotent reconciliation. Reads state, decides next action.
cmd_reconcile() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "No state file for today — nothing to reconcile."
        exit 0
    fi

    _update_state reconciliation.last_checked_at "\"$(_timestamp)\""

    local compile_status; compile_status="$(_get_state compile.status)"
    local verify_status; verify_status="$(_get_state verify.status)"
    local verify_attempts; verify_attempts="$(_get_state verify.attempts)"
    local verify_max_attempts; verify_max_attempts="$(_get_state verify.max_attempts)"
    local send_status; send_status="$(_get_state send.status)"
    local escalated; escalated="$(_get_state reconciliation.escalated)"
    local error; error="$(_get_state reconciliation.error)"

    # Ensure numeric defaults
    verify_attempts="${verify_attempts:-0}"
    verify_max_attempts="${verify_max_attempts:-3}"
    escalated="${escalated:-false}"

    echo "=== Newsletter Pipeline Reconciliation ==="
    echo "  compile: $compile_status"
    echo "  verify:  $verify_status (attempts: $verify_attempts/$verify_max_attempts)"
    echo "  send:    $send_status"
    echo "  escalated: $escalated"
    echo ""

    # If escalated, just report
    if [ "$escalated" = "true" ]; then
        echo "ESCALATED: ${error:-No error details}"
        exit 0
    fi

    # ── Logic tree ──

    # 1. Compile still running
    if [ "$compile_status" = "running" ]; then
        echo "compile still running"
        exit 0
    fi

    # 2. Compile pending (shouldn't happen at 5:30 but be safe)
    if [ "$compile_status" = "pending" ]; then
        echo "Compile is pending. Triggering now..."
        cmd_compile
        echo "COMPILE_TRIGGERED"
        exit 0
    fi

    # 3. Compile failed
    if [ "$compile_status" = "failed" ]; then
        _update_state reconciliation.error '"Compile failed — pipeline cannot proceed"'
        _update_state reconciliation.escalated true
        _update_state reconciliation.escalated_at "\"$(_timestamp)\""
        echo "COMPILE_FAILED_ESCALATED"
        exit 0
    fi

    # 4. Compile completed
    if [ "$compile_status" = "completed" ]; then
        # 4a. Verify retries exhausted
        if [ "$verify_attempts" -ge "$verify_max_attempts" ] 2>/dev/null; then
            _update_state reconciliation.escalated true
            _update_state reconciliation.escalated_at "\"$(_timestamp)\""
            _update_state reconciliation.error "\"Verify retries exhausted after $verify_attempts attempts\""
            echo "VERIFY_RETRIES_EXHAUSTED_ESCALATED"
            exit 0
        fi

        # 4b. Verify running — check for staleness
        if [ "$verify_status" = "running" ]; then
            local last_attempt_at; last_attempt_at="$(_get_state verify.last_attempt_at)"
            if [ -n "$last_attempt_at" ] && [ "$last_attempt_at" != "null" ]; then
                local now_epoch; now_epoch="$(date +%s)"
                local attempt_epoch=0
                if [[ "$last_attempt_at" == *Z ]]; then
                    attempt_epoch="$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_attempt_at" +%s 2>/dev/null || echo 0)"
                else
                    attempt_epoch="$(date -j -f "%Y-%m-%dT%H:%M:%S" "${last_attempt_at%Z}" +%s 2>/dev/null || echo 0)"
                fi
                local age=$((now_epoch - attempt_epoch))

                if [ "$age" -gt 600 ]; then
                    echo "Verify has been running for ${age}s (> 10 min). Re-triggering..."
                    cmd_verify --force
                    echo "VERIFY_RETRIGGERED"
                    exit 0
                fi
            fi

            echo "verify in progress"
            exit 0
        fi

        # 4c. Verify pending or idle
        if [ "$verify_status" = "pending" ] || [ "$verify_status" = "idle" ]; then
            cmd_verify
            echo "VERIFY_TRIGGERED"
            exit 0
        fi
    fi

    # 5. Verify completed — check send state
    if [ "$verify_status" = "completed" ]; then
        if [ "$send_status" = "completed" ]; then
            echo "COMPLETE"
            exit 0
        elif [ "$send_status" = "pending" ] || [ "$send_status" = "partial" ]; then
            echo "send pending - check manually"
            exit 0
        elif [ "$send_status" = "failed" ]; then
            _update_state reconciliation.escalated true
            _update_state reconciliation.escalated_at "\"$(_timestamp)\""
            _update_state reconciliation.error '"Send failed — pipeline cannot proceed"'
            echo "SEND_FAILED_ESCALATED"
            exit 0
        fi
    fi

    # Fallback — unexpected state
    echo "Unexpected pipeline state: compile=$compile_status verify=$verify_status send=$send_status"
    exit 0
}

# ── Subcommand: status ──
# Pretty-print the current state
cmd_status() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "No newsletter state file yet."
        echo "Run 'init' to create one for today ($TODAY)."
        exit 0
    fi

    local run_date; run_date="$(_get_state run_date)"
    local updated_at; updated_at="$(_get_state updated_at)"
    local compile_status; compile_status="$(_get_state compile.status)"
    local compile_pid; compile_pid="$(_get_state compile.pid)"
    local compile_started; compile_started="$(_get_state compile.started_at)"
    local compile_finished; compile_finished="$(_get_state compile.finished_at)"
    local compile_exit; compile_exit="$(_get_state compile.exit_code)"
    local compile_log; compile_log="$(_get_state compile.log_path)"
    local verify_status; verify_status="$(_get_state verify.status)"
    local verify_attempts; verify_attempts="$(_get_state verify.attempts)"
    local verify_max; verify_max="$(_get_state verify.max_attempts)"
    local verify_last_at; verify_last_at="$(_get_state verify.last_attempt_at)"
    local verify_last_outcome; verify_last_outcome="$(_get_state verify.last_attempt_outcome)"
    local send_status; send_status="$(_get_state send.status)"
    local send_sent; send_sent="$(_get_state send.reports_sent)"
    local send_failed; send_failed="$(_get_state send.reports_failed)"
    local send_count; send_count="$(_get_state send.report_count)"
    local rec_last; rec_last="$(_get_state reconciliation.last_checked_at)"
    local rec_esc; rec_esc="$(_get_state reconciliation.escalated)"
    local rec_err; rec_err="$(_get_state reconciliation.error)"

    echo "═══════════════════════════════════════════"
    echo "  Newsletter Pipeline Status"
    echo "═══════════════════════════════════════════"
    echo "  Run Date:    ${run_date:---}"
    echo "  Last Update: ${updated_at:---}"
    echo ""
    echo "  -- Compile --"
    echo "  Status:      $compile_status"
    echo "  PID:         ${compile_pid:---}"
    echo "  Started:     ${compile_started:---}"
    echo "  Finished:    ${compile_finished:---}"
    echo "  Exit Code:   ${compile_exit:---}"
    echo "  Log:         ${compile_log:---}"
    echo ""
    echo "  -- Verify --"
    echo "  Status:      $verify_status"
    echo "  Attempts:    ${verify_attempts:-0}/${verify_max:-3}"
    echo "  Last At:     ${verify_last_at:---}"
    echo "  Outcome:     ${verify_last_outcome:---}"
    echo ""
    echo "  -- Send --"
    echo "  Status:      $send_status"
    echo "  Reports:     ${send_count:-0} total"
    echo "  Sent:        ${send_sent:---}"
    echo "  Failed:      ${send_failed:---}"
    echo ""
    echo "  -- Reconciliation --"
    echo "  Last Check:  ${rec_last:---}"
    echo "  Escalated:   $rec_esc"
    echo "  Error:       ${rec_err:---}"
    echo "═══════════════════════════════════════════"
}

# ── Subcommand: reset ──
# Remove state file (debug/testing only)
cmd_reset() {
    if [ -f "$STATE_FILE" ]; then
        rm -f "$STATE_FILE"
        echo "State file removed."
    else
        echo "No state file to remove."
    fi
}

# ── Dispatch ──
CMD="${1:-help}"
shift || true

case "$CMD" in
    init)
        cmd_init
        ;;
    compile)
        cmd_compile
        ;;
    check-compile)
        cmd_check_compile
        ;;
    verify)
        cmd_verify "${1:-}"
        ;;
    complete-verify)
        cmd_complete_verify "${1:-}"
        ;;
    reconcile)
        cmd_reconcile
        ;;
    status)
        cmd_status
        ;;
    reset)
        cmd_reset
        ;;
    help|--help|-h)
        echo "Usage: $0 <command> [options]"
        echo ""
        echo "Commands:"
        echo "  init                        Create fresh state file for today"
        echo "  compile                     Background the compiler via task_utils"
        echo "  check-compile               Check if compile finished"
        echo "  verify [--force]            Trigger the verify-send cron job"
        echo "  complete-verify <outcome>   Mark verify as completed|failed"
        echo "  reconcile                   Full state-machine reconciliation"
        echo "  status                      Pretty-print current state"
        echo "  reset                       Remove state file (debug/testing)"
        ;;
    *)
        echo "Unknown command: $CMD"
        echo "Usage: $0 <command> [options]"
        echo "Run '$0 help' for available commands."
        exit 1
        ;;
esac
