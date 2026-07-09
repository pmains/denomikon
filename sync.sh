#!/usr/bin/env bash
# Deploy code + database to production.
#
# Binds code deploy and database sync into one atomic operation so they
# can never drift.  Run after confirming dev data is in good shape.
#
# Usage:
#   ./sync.sh              # deploy code + sync data + restart
#   ./sync.sh --code-only  # deploy code only, skip database sync
set -euo pipefail

cd "$(dirname "$0")"

SSH_TARGET="root@poliscopic.com"
APP_DIR="/opt/poliscopic"

# ── 1. Verify production database is reachable ──
echo "=== Step 1/5: Verify production database ==="
ssh "$SSH_TARGET" ". /opt/poliscopic/.env && python3 -c \"
import psycopg2, os
pg = psycopg2.connect(os.environ['DATABASE_URL'], options='-c client_encoding=UTF8')
cur = pg.cursor()
cur.execute('SELECT COUNT(*) FROM meetings')
cnt = cur.fetchone()[0]
cur.close()
pg.close()
print(f'Production DB OK: {cnt} meetings')
\"" && echo "✅ Production database reachable."

# ── 2. Rsync code + assets ──
#
# WARNING: NEVER use --delete here.  We're syncing specific files, not
# mirroring the entire workspace.  --delete would destroy .venv, .ssh,
# and other server-only files that don't exist locally.
#
# --checksum compares file content, not just timestamp+size, so edits
# that don't change file size still get deployed.
echo "=== Step 2/5: Deploy code ==="
rsync -avz --checksum \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.cache/' \
  --exclude='.env' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='permit-activity/' \
  --exclude='snapshots/' \
  --exclude='agendas/' \
  --exclude='agenda-items/' \
  --exclude='supporting-materials/' \
  --exclude='analytics.sqlite' \
  --exclude='bluesky_tracking.sqlite' \
  --exclude='data/' \
  --exclude='sync_db.sh' \
  --exclude='scripts/db/sync_dev_to_prod.py' \
  app.py \
  requirements.txt \
  scripts/ \
  static/ \
  templates/ \
  root@poliscopic.com:/opt/poliscopic/

rsync -avz --checksum routes/ root@poliscopic.com:/opt/poliscopic/routes/

# ── 3. Sync dev database → prod (unless --code-only) ──
if [ "${1:-}" != "--code-only" ]; then
  echo "=== Step 3/5: Sync database (dev → prod) ==="
  set -a
  source .env
  set +a
  .venv/bin/python scripts/db/sync_dev_to_prod.py
  echo "✅ Database sync complete"
fi

# ── 4. Set ownership (rsync preserves local uid/gid which don't match server) ──
echo "=== Step 4/5: Fix ownership ==="
ssh root@poliscopic.com "chown -R poliscopic:poliscopic /opt/poliscopic/"

# ── 5. Restart the app ──
echo "=== Step 5/5: Restart app ==="
ssh root@poliscopic.com "systemctl restart poliscopic" && echo "✅ App restarted."

echo "Deploy complete — code and database are in sync."
