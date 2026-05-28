#!/usr/bin/env bash
# Safe sync to production — never overwrite production database with empty data.
set -euo pipefail

cd "$(dirname "$0")"

SNAPSHOT_DIR="snapshots"
mkdir -p "$SNAPSHOT_DIR"

# 1. Verify the local database has BOS data before syncing
LOCAL_DB="data/maricopa.sqlite"
if [ ! -f "$LOCAL_DB" ]; then
    echo "ERROR: Local database not found at $LOCAL_DB" >&2
    exit 1
fi

MEETING_COUNT=$(python3 -c "
import sqlite3
conn = sqlite3.connect('$LOCAL_DB')
c = conn.cursor()
try:
    c.execute('SELECT COUNT(*) FROM meetings')
    print(c.fetchone()[0])
except Exception:
    print(0)
conn.close()
")

if [ "$MEETING_COUNT" -lt 10 ]; then
    echo "ERROR: Local database has only $MEETING_COUNT meetings (< 10). Refusing to sync." >&2
    echo "  Run: python scripts/agenda_scraper.py bos --sync --limit 5" >&2
    exit 1
fi

echo "Local database OK: $MEETING_COUNT meetings found."

# 2. Backup production database before syncing (if it exists)
echo "Backing up production database..."
if ssh root@poliscopic.com "test -f /opt/poliscopic/data/maricopa.sqlite"; then
  ssh root@poliscopic.com "cp /opt/poliscopic/data/maricopa.sqlite /opt/poliscopic/data/maricopa.sqlite.bak.\$(date +%Y%m%d_%H%M%S)"
  echo "  Production database backed up."
else
  echo "  No existing production database to back up (first deploy)."
fi

# 3. Create local snapshot
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
cp "$LOCAL_DB" "$SNAPSHOT_DIR/maricopa.sqlite.$TIMESTAMP"
echo "Local snapshot saved: $SNAPSHOT_DIR/maricopa.sqlite.$TIMESTAMP"

# 4. Prune old snapshots — keep only the 10 most recent
cd "$SNAPSHOT_DIR"
SNAPSHOTS=(maricopa.sqlite.*)
COUNT=${#SNAPSHOTS[@]}
if [ "$COUNT" -gt 10 ]; then
    # Sort by name (timestamps are lexicographically sortable) and remove excess
    REMOVE=$((COUNT - 10))
    for OLD in $(ls -1 maricopa.sqlite.* | head -n "$REMOVE"); do
        rm "$OLD"
        echo "Pruned old snapshot: $SNAPSHOT_DIR/$OLD"
    done
fi
cd ..

# 5. Ensure the SQLite database is in a consistent state before transfer
#    (WAL mode + concurrent writing can produce a corrupted snapshot)
python3 -c "
import sqlite3
conn = sqlite3.connect('$LOCAL_DB')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()
" && echo "Database checkpointed."

# 6. Rsync (excluding snapshots and raw permit files to save time/space)
#
# WARNING: NEVER use --delete here.  We're syncing specific files, not
# mirroring the entire workspace.  --delete would destroy .venv, .ssh,
# and other server-only files that don't exist in the local workspace.
#
# Trailing slashes on directories copy *contents* so subdirectories like
# scripts/scraper/ and static/fonts/ arrive at the right path.
rsync -avz \
  --exclude='permit-activity/' \
  --exclude='snapshots/' \
  --exclude='agendas/' \
  --exclude='agenda-items/' \
  --exclude='supporting-materials/' \
  --exclude='analytics.sqlite' \
  app.py \
  requirements.txt \
  scripts/ \
  static \
  templates \
  data \
  root@poliscopic.com:/opt/poliscopic/

# routes/ — rsync to subdirectory (avoid trailing-slash scattering in root)
rsync -avz routes/ root@poliscopic.com:/opt/poliscopic/routes/

# 7. Set ownership on the server (rsync preserves local uid/gid)
ssh root@poliscopic.com "chown -R poliscopic:poliscopic /opt/poliscopic/"

echo "Sync complete."
echo "Production backup: maricopa.sqlite.bak.$TIMESTAMP"
echo "Local snapshot:    $SNAPSHOT_DIR/maricopa.sqlite.$TIMESTAMP"
