#!/usr/bin/env bash
# task_utils.sh — Background task runner (Serenity pattern)
#
# Usage:
#   bash scripts/task_utils.sh run <task-name> "<command>"
#   bash scripts/task_utils.sh check <task-name>
#
# run: Backgrounds <command> via nohup, writes PID/log/status, exits < 1s
# check: Reads status file, reports result, auto-retries on failure
#
# See docs/workflows/TASK_UTILS.md for full design.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="$PROJECT_ROOT/data/tasks"
MAX_RETRIES=3
RETRY_DELAY_SEC=5

mkdir -p "$TASKS_DIR"

_run() {
    local task_name="$1"
    local cmd="$2"
    local date_stamp
    date_stamp=$(date "+%Y-%m-%d")

    local task_dir="$TASKS_DIR/$task_name"
    mkdir -p "$task_dir"

    # Prevent duplicate runs on the same day
    local status_file="$task_dir/$date_stamp.status"
    if [[ -f "$status_file" ]]; then
        local status
        status=$(grep -o '"status": *"[^"]*"' "$status_file" 2>/dev/null | cut -d'"' -f4 || echo "")
        if [[ "$status" == "running" ]]; then
            echo "SKIP: $task_name already running today (status=running)"
            exit 0
        fi
    fi

    # Write command file for retry replay
    echo "$cmd" > "$task_dir/$date_stamp.cmd"

    # Write initial status
    echo '{"status":"running"}' > "$status_file"

    # Launch via nohup
    nohup bash -c "$cmd" > "$task_dir/$date_stamp.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$task_dir/$date_stamp.pid"
    disown

    echo "Launched $task_name (pid=$pid) — log: $task_dir/$date_stamp.log"
}

_check() {
    local task_name="$1"
    local date_stamp
    date_stamp=$(date "+%Y-%m-%d")

    local task_dir="$TASKS_DIR/$task_name"
    local status_file="$task_dir/$date_stamp.status"
    local log_file="$task_dir/$date_stamp.log"
    local pid_file="$task_dir/$date_stamp.pid"

    # Check if task was ever started today
    if [[ ! -f "$status_file" ]]; then
        echo "❌ [$task_name] Never started today."
        exit 1
    fi

    local status
    status=$(grep -o '"status": *"[^"]*"' "$status_file" 2>/dev/null | cut -d'"' -f4 || echo "unknown")

    if [[ "$status" == "running" ]]; then
        # Check if the process is still alive
        if [[ -f "$pid_file" ]]; then
            local pid
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                echo "⏳ [$task_name] Still running (pid=$pid)."
                exit 1
            fi
        fi

        # Process died — wait for log to flush, then read exit code
        sleep 2
        local exit_code=1
        local exit_file="$task_dir/$date_stamp.exit_raw"
        if [[ -f "$exit_file" ]]; then
            exit_code=$(cat "$exit_file")
        fi
        _finish "$task_name" "$exit_code"
    elif [[ "$status" == "success" ]]; then
        echo "✅ [$task_name] Succeeded."
        tail -5 "$log_file" 2>/dev/null || true
        exit 0
    elif [[ "$status" == "failed" ]]; then
        local retry_file="$task_dir/$date_stamp.retry"
        local retries=0
        [[ -f "$retry_file" ]] && retries=$(cat "$retry_file")

        if (( retries < MAX_RETRIES )); then
            echo "🔁 [$task_name] Failed (attempt $((retries+1))/$MAX_RETRIES). Retrying..."
            retries=$((retries + 1))
            echo "$retries" > "$retry_file"

            local cmd
            cmd=$(cat "$task_dir/$date_stamp.cmd" 2>/dev/null || echo "")
            if [[ -n "$cmd" ]]; then
                sleep "$RETRY_DELAY_SEC"
                # Kill old process if still alive
                if [[ -f "$pid_file" ]]; then
                    local old_pid
                    old_pid=$(cat "$pid_file")
                    kill "$old_pid" 2>/dev/null || true
                fi
                echo '{"status":"running"}' > "$status_file"
                nohup bash -c "$cmd" > "$log_file" 2>&1 &
                local new_pid=$!
                echo "$new_pid" > "$pid_file"
                disown
                echo "🔁 Retry launched (pid=$new_pid)"
                exit 1
            fi
        else
            echo "❌ [$task_name] Failed after $MAX_RETRIES retries."
            tail -20 "$log_file" 2>/dev/null || true
            exit 1
        fi
    else
        echo "❌ [$task_name] Unknown status: $status"
        exit 1
    fi
}

_finish() {
    local task_name="$1"
    local exit_code="$2"
    local date_stamp
    date_stamp=$(date "+%Y-%m-%d")
    local task_dir="$TASKS_DIR/$task_name"
    local status_file="$task_dir/$date_stamp.status"

    if (( exit_code == 0 )); then
        echo '{"status":"success"}' > "$status_file"
        echo "✅ [$task_name] Completed successfully."
    else
        local retry_file="$task_dir/$date_stamp.retry"
        local retries=0
        [[ -f "$retry_file" ]] && retries=$(cat "$retry_file")
        if (( retries >= MAX_RETRIES )); then
            echo '{"status":"failed"}' > "$status_file"
            echo "❌ [$task_name] Failed (exit=$exit_code) after $MAX_RETRIES retries."
        else
            echo '{"status":"failed"}' > "$status_file"
        fi
    fi
}

# ── Main dispatch ──
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <run|check> <task-name> [command for run]"
    exit 1
fi

action="$1"
task_name="$2"
shift 2

case "$action" in
    run)
        if [[ $# -lt 1 ]]; then
            echo "Usage: $0 run <task-name> \"<command>\""
            exit 1
        fi
        _run "$task_name" "$*"
        ;;
    check)
        _check "$task_name"
        ;;
    *)
        echo "Unknown action: $action (use run or check)"
        exit 1
        ;;
esac
