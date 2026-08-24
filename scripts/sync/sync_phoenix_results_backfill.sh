#!/bin/bash
# sync_phoenix_results_backfill.sh — One-time backfill of Phoenix AEM meeting results
#
# Downloads metadata for all ~4,196 past meeting result PDFs from the
# AEM public_meeting_table results endpoint, then syncs them to the DB.
#
# Usage:
#   ./scripts/sync/sync_phoenix_results_backfill.sh
#
# This is a background job per the Serenity Philosophy:
#   $ nohup bash scripts/sync/sync_phoenix_results_backfill.sh \
#       > data/sync/phoenix-results-backfill-$(date +%Y%m%d-%H%M).log 2>&1 &

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

TIMESTAMP=$(date +%Y%m%d-%H%M)
LOG_FILE="data/sync/phoenix-results-backfill-${TIMESTAMP}.log"

echo "[$(date)] Starting Phoenix AEM results backfill..." | tee -a "$LOG_FILE"

# Activate virtualenv
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

# Set PYTHONPATH so scraper module is importable
export PYTHONPATH="${ROOT}/scripts${PYTHONPATH:+:$PYTHONPATH}"

# Run the results sync
python3 -u scripts/scraper/main.py phoenix-aem --sync-results 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=$?

echo "[$(date)] Backfill finished (exit code $EXIT_CODE)" | tee -a "$LOG_FILE"
exit $EXIT_CODE
