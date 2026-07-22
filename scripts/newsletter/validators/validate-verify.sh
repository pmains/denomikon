#!/usr/bin/env bash
# validate-verify.sh — Validates the verification result for a newsletter run
#
# Usage:
#   bash scripts/newsletter/validators/validate-verify.sh <run-dir> <compile-manifest> <verify-result>
#
# Checks:
#   - Verify result exists and is valid JSON
#   - approved is explicitly true
#   - verified_artifact_count matches compiled artifact_count
#   - No blocking issues present
#   - Run ID matches current run
#
# Exit 0 = validation passed
# Exit 1 = validation failed (details to stderr)

set -euo pipefail

RUN_DIR="${1:-}"
COMPILE_OUT="${2:-$RUN_DIR/compile-result.json}"
VERIFY_OUT="${3:-$RUN_DIR/verify-result.json}"

if [ ! -f "$COMPILE_OUT" ] || [ ! -f "$VERIFY_OUT" ]; then
    echo "Usage: $0 <run-dir> [compile-manifest] [verify-result]" >&2
    echo "  Defaults: compile-result.json, verify-result.json under run-dir" >&2
    exit 1
fi

errors=0

echo "=== Verification Validation ==="

# 1. Verify result exists
if [ ! -f "$VERIFY_OUT" ]; then
    echo "FAIL: verify-result.json not found at $VERIFY_OUT" >&2
    exit 1
fi
echo "  ✓ verify-result.json exists"

# 2. Valid JSON
if ! python3 -c "import json; json.load(open('$VERIFY_OUT'))" 2>/dev/null; then
    echo "FAIL: verify-result.json is not valid JSON" >&2
    exit 1
fi
echo "  ✓ verify-result.json is valid JSON"

# 3. status = "succeeded"
STATUS="$(python3 -c "import json; print(json.load(open('$VERIFY_OUT')).get('status', ''))" 2>/dev/null)"
if [ "$STATUS" != "succeeded" ]; then
    echo "FAIL: Verify status is '$STATUS', expected 'succeeded'" >&2
    errors=$((errors + 1))
fi
echo "  ✓ verify status: $STATUS"

# 4. approved is explicitly true
APPROVED="$(python3 -c "import json; print(str(json.load(open('$VERIFY_OUT')).get('approved', False)).lower())" 2>/dev/null)"
if [ "$APPROVED" != "true" ]; then
    echo "FAIL: approved is '$APPROVED', expected 'true'" >&2
    errors=$((errors + 1))
fi
echo "  ✓ approved: $APPROVED"

# 5. verified_artifact_count matches compiled artifact_count
COMPILED_COUNT="$(python3 -c "import json; print(json.load(open('$COMPILE_OUT')).get('artifact_count', 0))" 2>/dev/null)"
VERIFIED_COUNT="$(python3 -c "import json; print(json.load(open('$VERIFY_OUT')).get('verified_artifact_count', 0))" 2>/dev/null)"
if [ "$VERIFIED_COUNT" -ne "$COMPILED_COUNT" ]; then
    echo "FAIL: verified_artifact_count ($VERIFIED_COUNT) != compiled artifact_count ($COMPILED_COUNT)" >&2
    errors=$((errors + 1))
fi
echo "  ✓ verified_artifact_count ($VERIFIED_COUNT) == compiled ($COMPILED_COUNT)"

# 6. No blocking issues
BLOCKING="$(python3 -c "
import json
data = json.load(open('$VERIFY_OUT'))
blocking = data.get('blocking_issues', [])
print(len(blocking))
" 2>/dev/null)"
if [ "$BLOCKING" -gt 0 ]; then
    echo "FAIL: $BLOCKING blocking issues present" >&2
    python3 -c "
import json
data = json.load(open('$VERIFY_OUT'))
for issue in data.get('blocking_issues', []):
    print(f'  - {issue}')
" >&2
    errors=$((errors + 1))
fi
echo "  ✓ blocking issues: $BLOCKING"

# 7. Run ID matches
VERIFY_RUN_ID="$(python3 -c "import json; print(json.load(open('$VERIFY_OUT')).get('run_id', ''))" 2>/dev/null)"
COMPILE_RUN_ID="$(python3 -c "import json; print(json.load(open('$COMPILE_OUT')).get('run_id', ''))" 2>/dev/null)"
if [ "$VERIFY_RUN_ID" != "$COMPILE_RUN_ID" ]; then
    echo "FAIL: Verify run_id ($VERIFY_RUN_ID) != Compile run_id ($COMPILE_RUN_ID)" >&2
    errors=$((errors + 1))
fi
echo "  ✓ run_id match: $VERIFY_RUN_ID"

if [ "$errors" -gt 0 ]; then
    echo "FAILED: $errors validation error(s)"
    exit 1
fi

echo "PASSED: All verification validations passed."
exit 0
