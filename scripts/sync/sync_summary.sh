#!/bin/bash
# =============================================================================
# sync_summary.sh — Display last N days of sync summaries
#
# Usage:
#   ./scripts/sync_summary.sh              # Last 7 days
#   ./scripts/sync_summary.sh 14           # Last 14 days
#   ./scripts/sync_summary.sh --all        # All available summaries
#   ./scripts/sync_summary.sh --json       # JSON output (all available)
#   ./scripts/sync_summary.sh 7 --json     # Last 7 days as JSON
#
# Reads summary files at data/sync/YYYY-MM-DD-summary.txt (created by
# sync_log.sh) and presents a clean table.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$PROJECT_ROOT/data/sync"

# ── Parse arguments ──────────────────────────────────────────────────────
DAYS=7
JSON_MODE=false

for arg in "$@"; do
    case "$arg" in
        --all)
            DAYS=9999
            ;;
        --json)
            JSON_MODE=true
            ;;
        --help)
            echo "Usage: $(basename "$0") [DAYS|--all] [--json]"
            echo ""
            echo "  DAYS       Number of days of history to show (default: 7)"
            echo "  --all      Show all available summaries"
            echo "  --json     Output as JSON array"
            echo "  --help     Show this help"
            exit 0
            ;;
        [0-9]*)
            DAYS="$arg"
            ;;
        *)
            echo "Unknown option: $arg" >&2
            echo "Usage: $(basename "$0") [DAYS|--all] [--json]" >&2
            exit 1
            ;;
    esac
done

# ── Collect summary files within range ───────────────────────────────────
CUTOFF_DATE=$(date -v-${DAYS}d "+%Y-%m-%d" 2>/dev/null || date -d "-${DAYS} days" "+%Y-%m-%d" 2>/dev/null || echo "1970-01-01")

declare -a SUMMARIES=()
declare -a DATES=()

if [ ! -d "$LOG_DIR" ]; then
    echo "No sync log directory found at $LOG_DIR" >&2
    echo "Run scripts/sync_log.sh first to generate sync logs." >&2
    exit 1
fi

while IFS= read -r -d '' file; do
    basename=$(basename "$file" "-summary.txt")
    # Only include files within the requested window
    if [[ "$basename" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        if [[ "$basename" > "$CUTOFF_DATE" ]] || [[ "$basename" == "$CUTOFF_DATE" ]]; then
            SUMMARIES+=("$file")
            DATES+=("$basename")
        fi
    fi
done < <(find "$LOG_DIR" -name '*-summary.txt' -type f -print0 | sort -z -r)

# ── Read a single value from a summary file ──────────────────────────────
read_value() {
    local file="$1"
    local key="$2"
    grep "^${key}:" "$file" 2>/dev/null | awk -F': ' '{print $2}' | tr -d ' '
}

# ── Format duration ──────────────────────────────────────────────────────
format_duration() {
    local secs="$1"
    if [ -z "$secs" ] || [ "$secs" = "null" ]; then
        echo "—"
        return
    fi
    local mins=$((secs / 60))
    local rem=$((secs % 60))
    if [ $mins -gt 0 ]; then
        printf "%dm%02ds" "$mins" "$rem"
    else
        printf "%ds" "$rem"
    fi
}

# ── Color helpers (auto-disable if not a TTY) ────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'  # No Color
else
    GREEN=''; RED=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi

status_display() {
    local s="$1"
    case "$s" in
        success)  echo "${GREEN}success${NC}" ;;
        failed)   echo "${RED}failed${NC}" ;;
        partial)  echo "${YELLOW}partial${NC}" ;;
        *)        echo "${YELLOW}$s${NC}" ;;
    esac
}

# ── JSON output ──────────────────────────────────────────────────────────
if $JSON_MODE; then
    first=true
    echo "["
    for i in "${!SUMMARIES[@]}"; do
        $first || echo ","
        first=false
        file="${SUMMARIES[$i]}"
        date_str="${DATES[$i]}"

        start_time=$(read_value "$file" "start_time")
        duration=$(read_value "$file" "duration_seconds")
        exit_code=$(read_value "$file" "exit_code")
        status=$(read_value "$file" "completion_status")
        errors=$(read_value "$file" "error_count")
        total=$(read_value "$file" "total_meetings" || read_value "$file" "pre_total_meetings" || echo "")
        completed=$(read_value "$file" "completed" || read_value "$file" "pre_completed" || echo "")
        synced_run=$(read_value "$file" "meetings_synced_this_run")
        new_meets=$(read_value "$file" "new_meetings_discovered")
        new_items=$(read_value "$file" "new_agenda_items")

        cat <<JSONDATA
  {
    "date": "$date_str",
    "start_time": "${start_time:-null}",
    "duration_seconds": ${duration:-null},
    "exit_code": ${exit_code:-null},
    "status": "${status:-unknown}",
    "error_count": ${errors:-0},
    "total_meetings": ${total:-null},
    "completed": ${completed:-null},
    "synced_this_run": ${synced_run:-null},
    "new_meetings": ${new_meets:-0},
    "new_items": ${new_items:-0}
  }
JSONDATA
    done
    echo "]"
    exit 0
fi

# ── Table output ─────────────────────────────────────────────────────────
if [ ${#SUMMARIES[@]} -eq 0 ]; then
    echo "No sync summaries found in $LOG_DIR for the last $DAYS days."
    echo "Run scripts/sync_log.sh first to generate sync logs."
    exit 0
fi

# Print header
printf "${BOLD}%-12s  %-10s  %-16s  %-9s  %s${NC}\n" "Date" "Duration" "Meetings" "Status" "Notes"
printf "%-.0s\n" $(seq 80) | tr '\n' '=' | head -c 80
echo ""

for i in "${!SUMMARIES[@]}"; do
    file="${SUMMARIES[$i]}"
    date_str="${DATES[$i]}"

    status=$(read_value "$file" "completion_status")
    exit_code=$(read_value "$file" "exit_code")
    errors=$(read_value "$file" "error_count")
    duration_secs=$(read_value "$file" "duration_seconds")
    synced_run=$(read_value "$file" "meetings_synced_this_run")
    new_meets=$(read_value "$file" "new_meetings_discovered")
    new_items=$(read_value "$file" "new_agenda_items")

    duration_fmt=$(format_duration "$duration_secs")
    status_fmt=$(status_display "${status:-unknown}")

    # Build notes
    notes=""
    [ -n "$synced_run" ] && [ "$synced_run" != "0" ] && [ "$synced_run" != "null" ] && notes+="${synced_run} synced"
    [ -n "$new_meets" ] && [ "$new_meets" != "0" ] && [ "$new_meets" != "null" ] && notes+=", ${new_meets} new meet${new_meets:+s}"
    [ -n "$new_items" ] && [ "$new_items" != "0" ] && [ "$new_items" != "null" ] && notes+=", ${new_items} new item${new_items:+s}"

    # Truncate notes for display
    if [ ${#notes} -gt 30 ]; then
        notes="${notes:0:27}..."
    fi

    printf "%-12s  %-10s  %-16s  %b  %s\n" \
        "$date_str" \
        "$duration_fmt" \
        "${synced_run:--}/${new_meets:--}/${new_items:--}" \
        "$status_fmt" \
        "$notes"
done

echo ""
echo "${BOLD}Key:${NC}  Meetings column = synced_this_run / new_meetings / new_items"
echo ""
echo "Quick status:"

# Count recent outcomes
success_count=0
fail_count=0
partial_count=0
missing_count=0
for file in "${SUMMARIES[@]}"; do
    s=$(read_value "$file" "completion_status")
    case "$s" in
        success)  ((success_count++)) ;;
        failed)   ((fail_count++)) ;;
        partial)  ((partial_count++)) ;;
        *)        ((missing_count++)) ;;
    esac
done
echo "  Last ${DAYS}d: ${GREEN}$success_count success${NC}, ${YELLOW}$partial_count partial${NC}, ${RED}$fail_count failed${NC}${missing_count:+, $missing_count unknown}"
