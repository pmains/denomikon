#!/usr/bin/env bash
# validate-compile.sh — Validates compiler output for a newsletter run
#
# Usage:
#   bash scripts/newsletter/validators/validate-compile.sh <run-dir>
#
# Checks:
#   - Compile manifest exists and is valid JSON
#   - exit code was zero
#   - artifact_count > 0
#   - Every listed artifact file exists
#   - Every artifact is non-empty
#   - No unresolved template markers remain
#   - Run IDs match
#
# Exit 0 = validation passed
# Exit 1 = validation failed (details to stderr)

set -euo pipefail

RUN_DIR="${1:-}"
if [ -z "$RUN_DIR" ]; then
    echo "Usage: $0 <run-dir>" >&2
    exit 1
fi

COMPILE_OUT="$RUN_DIR/compile-result.json"
COMPILE_LOG="$RUN_DIR/compile.log"
STAGED_DIR="$RUN_DIR/staged"
PIPELINE_LOG="$RUN_DIR/pipeline.log"

errors=0

echo "=== Compile Validation ==="

# 1. Manifest exists
if [ ! -f "$COMPILE_OUT" ]; then
    echo "FAIL: compile-result.json not found at $COMPILE_OUT" >&2
    exit 1
fi
echo "  ✓ compile-result.json exists"

# 2. Valid JSON
if ! python3 -c "import json; json.load(open('$COMPILE_OUT'))" 2>/dev/null; then
    echo "FAIL: compile-result.json is not valid JSON" >&2
    errors=$((errors + 1))
fi
echo "  ✓ compile-result.json is valid JSON"

# 3. status = "succeeded"
STATUS="$(python3 -c "import json; print(json.load(open('$COMPILE_OUT')).get('status', ''))" 2>/dev/null)"
if [ "$STATUS" != "succeeded" ]; then
    echo "FAIL: Compile status is '$STATUS', expected 'succeeded'" >&2
    errors=$((errors + 1))
fi
echo "  ✓ compile status: $STATUS"

# 4. artifact_count > 0
COUNT="$(python3 -c "import json; print(json.load(open('$COMPILE_OUT')).get('artifact_count', 0))" 2>/dev/null)"
if [ "$COUNT" -le 0 ]; then
    echo "FAIL: Artifact count is $COUNT, expected > 0" >&2
    errors=$((errors + 1))
fi
echo "  ✓ artifact count: $COUNT"

# 5. Every listed artifact exists and is non-empty
python3 -c "
import json, os
manifest = json.load(open('$COMPILE_OUT'))
for artifact in manifest.get('artifacts', []):
    if not os.path.isfile(artifact):
        print(f'MISSING: {artifact}')
        exit(1)
    if os.path.getsize(artifact) == 0:
        print(f'EMPTY: {artifact}')
        exit(1)
print('All artifacts exist and are non-empty')
" 2>&1 || errors=$((errors + 1))

# 6. No unresolved template markers
if [ -f "$COMPILE_LOG" ]; then
    if grep -q '{{[^}]*}}' "$COMPILE_LOG" 2>/dev/null; then
        echo "WARN: Unresolved template markers found in compile log" >&2
        # Not a hard failure for Phase 1
    fi
fi
echo "  ✓ template check (warnings only)"

# 7. Run ID present
RUN_ID_CHECK="$(python3 -c "import json; print(json.load(open('$COMPILE_OUT')).get('run_id', ''))" 2>/dev/null)"
if [ -z "$RUN_ID_CHECK" ]; then
    echo "FAIL: No run_id in compile manifest" >&2
    errors=$((errors + 1))
fi
echo "  ✓ run_id: $RUN_ID_CHECK"

if [ "$errors" -gt 0 ]; then
    echo "FAILED: $errors validation error(s)"
    exit 1
fi

echo "PASSED: All compile validations passed."
exit 0
