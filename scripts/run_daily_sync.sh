#!/bin/zsh
export POLISCOPIC_DB_TIER=development
export PYTHONPATH="$HOME/Code/openclaw/maricopa-agendas/scripts"
cd "$HOME/Code/openclaw/maricopa-agendas" || exit 1
exec "$HOME/Code/openclaw/maricopa-agendas/.venv/bin/python" \
  "$HOME/Code/openclaw/maricopa-agendas/scripts/daily_sync.py" \
  >> "$HOME/Code/openclaw/maricopa-agendas/logs/cron_sync.log" 2>&1
