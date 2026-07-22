#!/usr/bin/env bash
# pipeline.sh — Sync pipeline state machine
#
# Manages the database sync lifecycle through a JSON state file at
# data/sync/state.json.
#
# Subcommands:
#   init                    Create fresh state file for today
#   run [--tier daily|weekly]  Background the sync runner via nohup
#   check                   Check if runner finished
#   status                  Pretty-print current state
#   reconcile               Retry failed jurisdictions, escalate if exhausted
#   reset                   Remove state file (debug/testing only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATE_FILE="$PROJECT_ROOT/data/sync/state.json"
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
# Usage: _update_state tier '"daily"'
# The value arg should be a JSON literal (string, number, bool, null)
_update_state() {
    _ensure_dir
    local key="$1"
    local value="$2"
    local py_value; py_value="$(_to_py_val "$value")"

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
# Usage: _get_state jurisdiction.bos.exit_code → prints value or empty string
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
    print(json.dumps(target))
else:
    print(target)
" 2>/dev/null || echo ""
}

# ── Subcommand: init ──
# Create a fresh state file. Error if one already exists.
cmd_init() {
    if [ -f "$STATE_FILE" ]; then
        echo "WARNING: State file already exists at $STATE_FILE"
        echo "         A sync may already be in progress."
        echo "         Use 'reset' to remove it and start fresh."
        exit 1
    fi

    _ensure_dir
    cat > "$STATE_FILE" <<-ENDJSON
{
  "run_date": "$TODAY",
  "tier": null,
  "started_at": null,
  "finished_at": null,
  "total_duration": null,
  "success_count": 0,
  "failure_count": 0,
  "skipped_count": 0,
  "jurisdictions": {},
  "reconciliation": {
    "last_checked_at": null,
    "escalated": false,
    "escalated_at": null,
    "error": null
  },
  "post_sync": {
    "minutes_check": null,
    "votes_backfill": null,
    "fts_rebuild": null
  },
  "updated_at": "$(_timestamp)"
}
ENDJSON
    echo "State file created for $TODAY"
}

# ── Subcommand: run [--tier daily|weekly] ──
# Background the sync runner via nohup (Serenity Philosophy)
cmd_run() {
    local tier="daily"
    if [ "${1:-}" = "--tier" ]; then
        tier="${2:-daily}"
    fi

    if [ ! -f "$STATE_FILE" ]; then
        echo "ERROR: No state file found. Run 'init' first."
        exit 1
    fi

    # Check if already running
    local started_at; started_at="$(_get_state started_at)"
    local finished_at; finished_at="$(_get_state finished_at)"
    if [ -n "$started_at" ] && [ "$started_at" != "null" ] && \
       ( [ -z "$finished_at" ] || [ "$finished_at" = "null" ] ); then
        echo "WARNING: Sync appears to be already running (started at $started_at)."
        echo "         Use 'check' to see progress, or 'reset' to force-clear."
        exit 1
    fi

    # Update state
    _update_state tier "\"$tier\""
    _update_state started_at "\"$(_timestamp)\""
    _update_state finished_at "null"

    # Launch via nohup with timestamped log
    local log_file="$PROJECT_ROOT/data/sync/runner-$(date +%Y%m%d-%H%M).log"
    echo "Starting sync runner (tier=$tier)..."
    echo "  Log: $log_file"

    cd "$PROJECT_ROOT"
    nohup python3 -u scripts/sync/runner.py --tier "$tier" \
        > "$log_file" 2>&1 &
    local pid=$!

    echo "  PID: $pid"
    _update_state pid "$pid"
    echo "Sync runner backgrounded (PID $pid)."
    echo "Check progress: tail -f $log_file"
}

# ── Subcommand: check ──
# Check if the runner has finished by reading state.json
cmd_check() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "No state file — nothing to check."
        exit 0
    fi

    local started_at; started_at="$(_get_state started_at)"
    local finished_at; finished_at="$(_get_state finished_at)"

    if [ -z "$started_at" ] || [ "$started_at" = "null" ]; then
        echo "Sync not started yet."
        exit 0
    fi

    if [ -n "$finished_at" ] && [ "$finished_at" != "null" ]; then
        echo "Sync completed at $finished_at"
        local success; success="$(_get_state success_count)"
        local failure; failure="$(_get_state failure_count)"
        local duration; duration="$(_get_state total_duration)"
        echo "  Duration: ${duration:-?}s"
        echo "  Success: ${success:-0} | Failure: ${failure:-0}"
        exit 0
    fi

    # Check if a PID is still running
    local pid; pid="$(_get_state pid)"
    if [ -n "$pid" ] && [ "$pid" != "null" ]; then
        if kill -0 "$pid" 2>/dev/null; then
            echo "Sync still running (PID $pid)."
            exit 0
        fi
    fi

    echo "Sync started but no PID running — may have crashed."
    echo "Check logs in data/sync/runner-*.log for details."
    exit 0
}

# ── Subcommand: reconcile ──
# Retry failed jurisdictions, escalate if retries exhausted.
cmd_reconcile() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "No state file — nothing to reconcile."
        exit 0
    fi

    _update_state reconciliation.last_checked_at "\"$(_timestamp)\""

    local escalated; escalated="$(_get_state reconciliation.escalated)"
    local error; error="$(_get_state reconciliation.error)"
    escalated="${escalated:-false}"

    if [ "$escalated" = "true" ]; then
        echo "ESCALATED: ${error:-No error details}"
        echo "Manual intervention required."
        exit 0
    fi

    # Collect failed jurisdictions
    local failed_juris=()
    local max_retries=2

    while IFS= read -r juris; do
        [ -z "$juris" ] && continue
        local ec; ec="$(_get_state jurisdictions.$juris.exit_code)"
        local retry_count; retry_count="$(_get_state jurisdictions.$juris.retry_count)"
        retry_count="${retry_count:-0}"

        # Only retry non-zero exit codes (not 0 = success, not null = unrun)
        if [ -n "$ec" ] && [ "$ec" != "null" ] && [ "$ec" != "0" ]; then
            if [ "$retry_count" -lt "$max_retries" ]; then
                failed_juris+=("$juris")
            fi
        fi
    done < <(python3 -c "
import json
data = json.load(open('$STATE_FILE'))
for j in data.get('jurisdictions', {}):
    print(j)
" 2>/dev/null)

    if [ ${#failed_juris[@]} -eq 0 ]; then
        echo "No failed jurisdictions to retry. Sync is clean."
        exit 0
    fi

    echo "=== Sync Reconciliation ==="
    echo "  Failed jurisdictions to retry: ${#failed_juris[@]}"
    for j in "${failed_juris[@]}"; do
        local ec; ec="$(_get_state jurisdictions.$j.exit_code)"
        local err; err="$(_get_state jurisdictions.$j.error_message)"
        local retry_count; retry_count="$(_get_state jurisdictions.$j.retry_count)"
        retry_count="${retry_count:-0}"
        local new_retry=$((retry_count + 1))
        echo "  Retrying $j (attempt $new_retry, previous exit=$ec)..."

        # Increment retry count
        _update_state jurisdictions.$j.retry_count "$new_retry"

        # Re-run single jurisdiction
        local tier; tier="$(_get_state tier)"
        tier="${tier:-daily}"

        cd "$PROJECT_ROOT"
        local result
        result=$(PYTHONPATH=scripts python3 scripts/sync/runner.py --tier "$tier" --dry-run 2>&1 | head -1 || true)

        # Re-run the jurisdiction directly
        local start_date end_date
        if [ "$tier" = "weekly" ]; then
            start_date=$(date -j -v-30d +%Y-%m-%d 2>/dev/null || date -d -30days +%Y-%m-%d)
        else
            start_date=$(date -j -v-3d +%Y-%m-%d 2>/dev/null || date -d -3days +%Y-%m-%d)
        fi
        end_date=$(date -j -v+14d +%Y-%m-%d 2>/dev/null || date -d +14days +%Y-%m-%d)

        set +e
        PYTHONPATH=scripts python3 -c "
import subprocess, sys, json
cmd = [sys.executable, 'scripts/scraper/main.py', '$j', '--sync',
       '--start-date=$start_date', '--end-date=$end_date']
result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                        env={**__import__('os').environ, 'PYTHONPATH': 'scripts'},
                        cwd='$PROJECT_ROOT')
print(result.stdout[-200:] if result.stdout else '')
print(result.stderr[-200:] if result.stderr else '', file=sys.stderr)
sys.exit(result.returncode)
" 2>&1
        local retry_exit=$?
        set -e

        if [ $retry_exit -eq 0 ]; then
            _update_state jurisdictions.$j.exit_code 0
            _update_state jurisdictions.$j.error_message "null"
            echo "  ✅ $j retry succeeded"
        else
            echo "  ❌ $j retry failed (exit=$retry_exit)"
            if [ "$new_retry" -ge "$max_retries" ]; then
                _update_state reconciliation.escalated true
                _update_state reconciliation.escalated_at "\"$(_timestamp)\""
                _update_state reconciliation.error "\"Retries exhausted for $j (exit=$retry_exit)\""
                echo "  ESCALATED: $j retries exhausted"
            fi
        fi
    done

    echo "Reconciliation complete."
}

# ── Subcommand: status ──
# Pretty-print the current state
cmd_status() {
    if [ ! -f "$STATE_FILE" ]; then
        echo "No sync state file yet."
        echo "Run 'init' to create one for today ($TODAY)."
        exit 0
    fi

    local run_date; run_date="$(_get_state run_date)"
    local tier; tier="$(_get_state tier)"
    local updated_at; updated_at="$(_get_state updated_at)"
    local started_at; started_at="$(_get_state started_at)"
    local finished_at; finished_at="$(_get_state finished_at)"
    local total_duration; total_duration="$(_get_state total_duration)"
    local success_count; success_count="$(_get_state success_count)"
    local failure_count; failure_count="$(_get_state failure_count)"
    local skipped_count; skipped_count="$(_get_state skipped_count)"
    local rec_last; rec_last="$(_get_state reconciliation.last_checked_at)"
    local rec_esc; rec_esc="$(_get_state reconciliation.escalated)"
    local rec_err; rec_err="$(_get_state reconciliation.error)"
    local mc; mc="$(_get_state post_sync.minutes_check)"
    local vb; vb="$(_get_state post_sync.votes_backfill)"
    local fts; fts="$(_get_state post_sync.fts_rebuild)"

    echo "═══════════════════════════════════════════"
    echo "  Sync Pipeline Status"
    echo "═══════════════════════════════════════════"
    echo "  Run Date:    ${run_date:---}"
    echo "  Tier:        ${tier:---}"
    echo "  Started:     ${started_at:---}"
    echo "  Finished:    ${finished_at:---}"
    local duration_disp="${total_duration:-}"
    if [ -z "$duration_disp" ] || [ "$duration_disp" = "null" ]; then
      duration_disp="?"
    fi
    echo "  Duration:    ${duration_disp}s"
    echo "  Last Update: ${updated_at:---}"
    echo ""
    echo "  -- Results --"
    echo "  Success:     ${success_count:-0}"
    echo "  Failure:     ${failure_count:-0}"
    echo "  Skipped:     ${skipped_count:-0}"
    echo ""
    echo "  -- Post-Sync --"
    echo "  Minutes:     ${mc:---}"
    echo "  Votes:       ${vb:---}"
    echo "  FTS:         ${fts:---}"
    echo ""
    echo "  -- Reconciliation --"
    echo "  Last Check:  ${rec_last:---}"
    echo "  Escalated:   $rec_esc"
    echo "  Error:       ${rec_err:---}"
    echo ""

    # List jurisdiction results if any
    local juris_list
    juris_list="$(python3 -c "
import json
data = json.load(open('$STATE_FILE'))
for j, js in sorted(data.get('jurisdictions', {}).items()):
    ec = js.get('exit_code')
    dur = js.get('duration_s', 0) or 0
    items = js.get('items_synced') or '?'
    err = js.get('error_message', '') or ''
    if ec == 0 and not err:
        print(f'  ✅ {j}: {items} items ({dur:.0f}s)')
    elif ec is not None and ec != 'null':
        print(f'  ❌ {j}: exit={ec} ({dur:.0f}s) {err[:80]}')
    else:
        print(f'  ⬜ {j}: pending')
" 2>/dev/null || true)"

    if [ -n "$juris_list" ]; then
        echo "  -- Jurisdictions --"
        echo "$juris_list"
    fi
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
    run)
        cmd_run "${1:-}" "${2:-}"
        ;;
    check)
        cmd_check
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
        echo "  init                        Create fresh state file"
        echo "  run [--tier daily|weekly]   Background the sync runner via nohup"
        echo "  check                       Check if runner finished"
        echo "  reconcile                   Retry failed jurisdictions, escalate if exhausted"
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
