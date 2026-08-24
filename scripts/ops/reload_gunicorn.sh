#!/usr/bin/env bash
# Gracefully reload gunicorn — sends SIGHUP, old workers finish current
# requests while new workers start with the deployed code.
#
# No dropped connections.  If the reload fails, old workers keep serving.
#
# Usage:
#   ./scripts/reload_gunicorn.sh

set -euo pipefail

SSH_ROOT="root@poliscopic.com"

echo "=== Graceful gunicorn reload ==="
ssh ${SSH_ROOT} "systemctl reload poliscopic" && echo "✅ gunicorn reloaded"
