#!/usr/bin/env bash
# router-verify.sh — Router-compatible verify step for newsletter
#
# Usage:
#   bash scripts/newsletter/router-verify.sh --run-dir <run-dir>
#
# Validates staged report files and writes the router-expected state file.
# This is a basic structural validation. Deeper content verification 
# happens via the pipeline.sh state machine.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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
VERIFIED_DIR="$RUN_DIR/verified"
mkdir -p "$VERIFIED_DIR"

TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

echo "[$TIMESTAMP] Verifying staged reports in $STAGED_DIR..."

# ── Collect staged files ──
STAGED_FILES=()
for f in "$STAGED_DIR"/*.json; do
    if [ -f "$f" ]; then
        STAGED_FILES+=("$f")
    fi
done

if [ ${#STAGED_FILES[@]} -eq 0 ]; then
    echo "[$TIMESTAMP] No staged files to verify."
    echo "{\"status\":\"succeeded\",\"message\":\"no files to verify\",\"artifact_count\":0,\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$(date -u +'%Y-%m-%dT%H:%M:%SZ')\"}" > "$RUN_DIR/steps/verify.state"
    exit 0
fi

# ── Validate each JSON file ──
ERRORS=()
VALID_FILES=()

for f in "${STAGED_FILES[@]}"; do
    BASENAME="$(basename "$f")"
    
    # Check it's valid JSON
    if ! python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
        ERRORS+=("$BASENAME: invalid JSON")
        echo "  ✗ $BASENAME: INVALID JSON"
        continue
    fi

    # Check it has required fields (staged report format)
    if ! python3 -c "
import json
d = json.load(open('$f'))
required = ['report_name', 'html', 'items']
for r in required:
    if r not in d:
        exit(1)
" 2>/dev/null; then
        ERRORS+=("$BASENAME: missing required fields")
        echo "  ✗ $BASENAME: missing required fields"
        continue
    fi

    VALID_FILES+=("$f")
    echo "  ✓ $BASENAME"
done

echo "[$TIMESTAMP] ${#VALID_FILES[@]}/${#STAGED_FILES[@]} files valid."

# ── Copy valid files to verified dir ──
if [ ${#VALID_FILES[@]} -gt 0 ]; then
    for f in "${VALID_FILES[@]}"; do
        cp "$f" "$VERIFIED_DIR/"
    done
fi

# ── Write verify manifest (using Python script file to avoid quoting hell) ──
PY_SCRIPT=$(mktemp /tmp/verify-manifest-XXXXXX.py)
cat > "$PY_SCRIPT" << 'PYEOF'
import json
import os
import sys

run_dir = os.environ.get('VERIFY_RUN_DIR', '')
staged_dir = os.environ.get('VERIFY_STAGED_DIR', '')
verified_dir = os.environ.get('VERIFY_VERIFIED_DIR', '')
timestamp = os.environ.get('VERIFY_TIMESTAMP', '')
errors_raw = os.environ.get('VERIFY_ERRORS', '')
total_files = int(os.environ.get('VERIFY_TOTAL_FILES', '0'))
valid_files = int(os.environ.get('VERIFY_VALID_FILES', '0'))

errors = [e for e in errors_raw.split('\n') if e.strip()] if errors_raw else []

finished_at = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

manifest = {
    'status': 'succeeded' if not errors else 'partial',
    'run_dir': run_dir,
    'staged_dir': staged_dir,
    'verified_dir': verified_dir,
    'total_files': total_files,
    'valid_files': valid_files,
    'errors': errors,
    'started_at': timestamp,
    'finished_at': finished_at,
}

with open(os.path.join(run_dir, 'verify-result.json'), 'w') as f:
    json.dump(manifest, f, indent=2)
PYEOF

export VERIFY_RUN_DIR="$RUN_DIR"
export VERIFY_STAGED_DIR="$STAGED_DIR"
export VERIFY_VERIFIED_DIR="$VERIFIED_DIR"
export VERIFY_TIMESTAMP="$TIMESTAMP"
export VERIFY_ERRORS="$(printf '%s\n' "${ERRORS[@]:-}")"
export VERIFY_TOTAL_FILES="${#STAGED_FILES[@]}"
export VERIFY_VALID_FILES="${#VALID_FILES[@]}"

python3 "$PY_SCRIPT"
rm -f "$PY_SCRIPT"

# ── Write router state file ──
FINISHED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

if [ ${#ERRORS[@]} -eq 0 ]; then
    echo "{\"status\":\"succeeded\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$FINISHED_AT\"}" > "$RUN_DIR/steps/verify.state"
    echo "[$TIMESTAMP] Verify step complete — all valid."
else
    echo "{\"status\":\"failed\",\"error\":\"${#ERRORS[@]} files failed validation\",\"started_at\":\"$TIMESTAMP\",\"finished_at\":\"$FINISHED_AT\"}" > "$RUN_DIR/steps/verify.state"
    echo "[$TIMESTAMP] Verify step FAILED — ${#ERRORS[@]} files invalid."
    exit 1
fi
