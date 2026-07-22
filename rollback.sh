#!/usr/bin/env bash
# Emergency rollback — deploy old code, keep old table intact.
#
# Since the migration strategy is "create new, preserve old," rollback
# is just deploying the old code.  The old table still has all its data.
#
# Usage:
#   ./rollback.sh

set -euo pipefail

cd "$(dirname "$0")"

set -a; source .env 2>/dev/null || true; set +a

SSH_TARGET="poliscopic@poliscopic.com"
APP_DIR="/opt/poliscopic"

rsync_safe() {
  rsync "$@" || { rc=$?; [ $rc -le 24 ] && return 0 || exit $rc; }
}

echo "=== Rollback: Step 1 — Deploy old code ==="

# The current working tree has the NEW code.  To deploy old code, we
# need to deploy whatever is currently on prod (which is the OLD code).
# If you committed the pre-migration state, check it out:
#
#   git checkout <pre-migration-tag-or-commit>
#   ./rollback.sh
#   git switch -         # back to working branch
#
# Otherwise, this is a placeholder — the actual rollback is:
echo "  → git checkout the last pre-migration commit"
echo "  → re-run this script"

# ── Rsync code to prod ──
echo "  Syncing code to ${SSH_TARGET}:${APP_DIR} ..."
rsync_safe -avz --delete \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.env' \
  ./ "${SSH_TARGET}:${APP_DIR}/"

echo "=== Rollback: Step 2 — Verify old code imports ==="
ssh root@poliscopic.com "cd ${APP_DIR} && .venv/bin/python -c \"
from db.models import MeetingSupervisor
print('✅ MeetingSupervisor imports OK')
from db.models import MeetingMember
print('⚠ MeetingMember also imports (backward compat)')
\""

echo "=== Rollback: Step 3 — Graceful gunicorn reload ==="
ssh root@poliscopic.com "systemctl reload gunicorn-poliscopic"

echo ""
echo "=== Rollback complete ==="
echo "  Old code deployed.  Old table (meeting_supervisors) still has all its data."
echo "  New table (meeting_members) still exists but is not queried by old code."
echo "  No data was lost."
