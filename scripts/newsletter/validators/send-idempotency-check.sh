#!/usr/bin/env bash
# send-idempotency-check.sh — Check if a newsletter has already been sent
#
# Usage:
#   bash scripts/newsletter/validators/send-idempotency-check.sh <date>
#   Returns 0 if already sent, 1 if not yet sent
#
# Idempotency file: data/newsletter/sent.idempotency (one key per line)
# Key format: newsletter:YYYY-MM-DD

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IDEMPOTENCY_FILE="$PROJECT_ROOT/data/newsletter/sent.idempotency"
DATE_KEY="${1:-$(date +%Y-%m-%d)}"
IDEMPOTENCY_KEY="newsletter:${DATE_KEY}"

if [ ! -f "$IDEMPOTENCY_FILE" ]; then
    echo "NOT_SENT: No idempotency file exists."
    exit 1
fi

if grep -Fxq "$IDEMPOTENCY_KEY" "$IDEMPOTENCY_FILE" 2>/dev/null; then
    echo "ALREADY_SENT: Key '$IDEMPOTENCY_KEY' found in idempotency file."
    exit 0
fi

echo "NOT_SENT: Key '$IDEMPOTENCY_KEY' not found."
exit 1
