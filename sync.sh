#!/usr/bin/env bash
# Deploy code/assets to production.  Database is shared via Postgres (no sync).
set -euo pipefail

cd "$(dirname "$0")"

SSH_TARGET="root@poliscopic.com"
APP_DIR="/opt/poliscopic"

# 1. Verify the database is accessible (Postgres, shared — no local copy needed)
echo "Checking Postgres connection on production…"
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

# 2. Rsync code + assets (exclude raw data dirs, snapshots, venv, git)
#
# WARNING: NEVER use --delete here.  We're syncing specific files, not
# mirroring the entire workspace.  --delete would destroy .venv, .ssh,
# and other server-only files that don't exist locally.
rsync -avz \
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
  app.py \
  requirements.txt \
  scripts/ \
  static/ \
  templates/ \
  root@poliscopic.com:/opt/poliscopic/

rsync -avz routes/ root@poliscopic.com:/opt/poliscopic/routes/

# 3. Set ownership (rsync preserves local uid/gid which don't match server)
ssh root@poliscopic.com "chown -R poliscopic:poliscopic /opt/poliscopic/"

# 4. Restart the app so new templates/code take effect
ssh root@poliscopic.com "systemctl restart poliscopic" && echo "✅ App restarted."

echo "Deploy complete."
