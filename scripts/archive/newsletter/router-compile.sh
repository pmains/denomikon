#!/usr/bin/env bash
# router-compile.sh — Router-compatible compile step for newsletter
#
# Usage:
#   bash scripts/newsletter/router-compile.sh --run-dir <run-dir>
#
# Calls the report compiler, copies staged files into the router's run dir,
# and writes the router-expected state file.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
COMPILER="$PROJECT_ROOT/scripts/reports/compiler.py"
COMPILER_STAGING_DIR="$PROJECT_ROOT/data/reports/staging"

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

STAGED_DIR="$RUN_DIR/staged"
mkdir -p "$STAGED_DIR"

TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

# ── Run the compiler ──
echo "[$TIMESTAMP] Running compiler..."
cd "$PROJECT_ROOT"
if ! "$VENV_PYTHON" -u "$COMPILER" --due-today --stage 2>&1; then
    echo "[$TIMESTAMP] Compiler failed"
    echo "{\"status\":\"failed\",\"error\":\"compiler exit non-zero\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$(date -u +'%Y-%m-%dT%H:%M:%SZ')\"}" > "$RUN_DIR/steps/compile.state"
    exit 1
fi

# ── Copy staged files ──
echo "[$TIMESTAMP] Copying staged reports..."
STAGED_FILES=()
for f in "$COMPILER_STAGING_DIR"/*.json; do
    if [ -f "$f" ]; then
        cp "$f" "$STAGED_DIR/"
        STAGED_FILES+=("$(basename "$f")")
        echo "  -> $(basename "$f")"
    fi
done

echo "[$TIMESTAMP] ${#STAGED_FILES[@]} reports staged."

# ── Write compile manifest for downstream steps ──
python3 -c "
import json
manifest = {
    'status': 'succeeded',
    'run_dir': '${RUN_DIR}',
    'staged_dir': '${STAGED_DIR}',
    'artifact_count': ${#STAGED_FILES[@]},
    'artifacts': $(printf '%s\n' "${STAGED_FILES[@]:-}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))'),
    'finished_at': '$(date -u +'%Y-%m-%dT%H:%M:%SZ')'
}
json.dump(manifest, open('${RUN_DIR}/compile-result.json', 'w'), indent=2)
"

# ── Write router state file ──
echo "{\"status\":\"succeeded\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$(date -u +'%Y-%m-%dT%H:%M:%SZ')\"}" > "$RUN_DIR/steps/compile.state"
echo "[$TIMESTAMP] Compile step complete."
