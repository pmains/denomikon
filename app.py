#!/usr/bin/env python3
"""Poliscopic Meetings — Flask web app entry point.

Usage:
    cd /path/to/maricopa-agendas
    .venv/bin/python app.py

Opens at http://127.0.0.1:5001/meetings (port 5000 is used by OpenClaw)
    FLASK_PORT=9000 python app.py  # override port
"""

import os
import sys
from pathlib import Path

# Production-only: add local packages when running in .venv that doesn't own site-packages
_local_pkgs = Path(__file__).resolve().parent / ".local-pkgs"
if _local_pkgs.exists():
    sys.path.insert(0, str(_local_pkgs))

# Ensure scripts/ and routes/ are importable
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here / "scripts"))

from routes import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(debug=True, port=port)
