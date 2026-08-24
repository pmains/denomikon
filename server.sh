#!/bin/bash
# server.sh — Start/stop/restart the Poliscopic Flask app.
# Usage: ./server.sh start|stop|restart|status|pid
# The PID file prevents zombie processes that haunted earlier sessions.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/.server.pid"
LOG_FILE="/tmp/poliscopic-server.log"
PORT=5001
PYTHON="$APP_DIR/.venv/bin/python3"

handle_stop() {
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Stopping server (PID $OLD_PID)..."
      kill "$OLD_PID" 2>/dev/null || true
      # Wait for it to die
      for i in 1 2 3 4 5; do
        if ! kill -0 "$OLD_PID" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      # Force kill if still alive
      if kill -0 "$OLD_PID" 2>/dev/null; then
        kill -9 "$OLD_PID" 2>/dev/null || true
      fi
    else
      echo "PID $OLD_PID not running."
    fi
    rm -f "$PID_FILE"
  fi

  # Also kill anything else on our port (safety net for zombies)
  PORT_PID=$(lsof -ti TCP:"$PORT" 2>/dev/null || true)
  if [ -n "$PORT_PID" ]; then
    echo "Clearing stale process on port $PORT (PID $PORT_PID)..."
    kill -9 "$PORT_PID" 2>/dev/null || true
    sleep 1
  fi
}

handle_start() {
  handle_stop

  # Clear Jinja2 template cache to avoid stale CSS/HTML
  rm -rf "$APP_DIR/.cache"

  echo "Starting server on 0.0.0.0:$PORT..."
  cd "$APP_DIR"
  nohup "$PYTHON" -u -c "
import os
os.environ['FLASK_ENV'] = 'production'
from routes import create_app
app = create_app()
app.run(debug=False, host='0.0.0.0', port=$PORT, use_reloader=False)
" > "$LOG_FILE" 2>&1 &

  PID=$!
  echo "$PID" > "$PID_FILE"
  echo "Server PID: $PID"

  # Wait for it to be ready
  for i in $(seq 1 30); do
    if lsof -ti TCP:"$PORT" 2>/dev/null | grep -q "$PID"; then
      echo "Ready after ${i}s at http://127.0.0.1:$PORT"
      exit 0
    fi
    sleep 1
  done

  echo "Server started but not yet listening — check $LOG_FILE"
}

case "${1:-start}" in
  start)
    handle_start
    ;;
  stop)
    handle_stop
    ;;
  restart)
    handle_start
    ;;
  status)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "Running (PID $PID, port $PORT, uptime $(ps -o etime= -p "$PID" | tr -d ' '))"
      else
        echo "PID file exists but process $PID not running. Stale."
        rm -f "$PID_FILE"
      fi
    else
      PORT_PID=$(lsof -ti TCP:"$PORT" 2>/dev/null || true)
      if [ -n "$PORT_PID" ]; then
        echo "Running (PID $PORT_PID, port $PORT) — no PID file."
      else
        echo "Not running."
      fi
    fi
    ;;
  pid)
    cat "$PID_FILE" 2>/dev/null || echo "No PID file"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|pid}"
    exit 1
    ;;
esac
