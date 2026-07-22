"""
Database configuration — PostgreSQL default.

TIERS
-----

  TIER          DATABASE                       PURPOSE
  ────────────  ──────────────────────────────  ─────────────────────────────────
  development  PostgreSQL (poliscopic_dev)      Daily work: scraping + Flask app
  test          tempfile SQLite                  Unit/integration tests
  production   error (sync.sh handles this)     Public-facing site


HOW TO USE
----------

Development (default — .env supplies DATABASE_URL):
    python scripts/agenda_scraper.py peoria --sync --year=2026

    Reads .env at project root for DATABASE_URL.  Falls back to the
    PostgreSQL dev instance at localhost:5432/poliscopic_dev if .env
    is absent.

Test (pytest sets this automatically via conftest.py):
    pytest tests/

    Creates a temp SQLite file, runs tests, destroys it.  Never touches
    development data.

Production (sync.sh handles this — not for direct use):
    ./sync.sh

    The production gunicorn process reads from /opt/poliscopic/data/maricopa.sqlite.

To override the database for a one-off command:
    DATABASE_URL=postgresql://... python scripts/agenda_scraper.py ...

To use the old SQLite database for historical reference:
    DATABASE_URL="sqlite:///data/maricopa.sqlite" python scripts/agenda_scraper.py ...

SQLite (data/maricopa.sqlite) is retained as a historical archive only.
All ongoing work uses PostgreSQL.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # Load .env — supplies DATABASE_URL

_DEV_PG = os.environ["DATABASE_URL"]
_SQLITE_FALLBACK = str((Path(__file__).resolve().parent.parent.parent / "data" / "maricopa.sqlite").resolve())

# ── Resolution order ──────────────────────────────────────────────────
# 1. DATABASE_URL env var (explicit override, also set by .env)
# 2. POLISCOPIC_DB_TIER=test  →  temp SQLite (used by pytest)
# 3. POLISCOPIC_DB_TIER=production  →  error out (sync.sh handles this)
# 4. Default  →  PostgreSQL dev instance

_DB_TIER = os.environ.get("POLISCOPIC_DB_TIER", "").lower().strip()

if os.environ.get("DATABASE_URL"):
    # Explicit URL or .env value overrides tier selection
    DATABASE_URL = os.environ["DATABASE_URL"]
elif _DB_TIER == "test":
    import tempfile
    DATABASE_URL = f"sqlite:///{tempfile.mktemp(suffix='.sqlite')}"
elif _DB_TIER == "production":
    raise RuntimeError(
        "POLISCOPIC_DB_TIER=production is not for direct use. "
        "Use sync.sh to deploy to poliscopic.com."
    )
else:
    # Default to PostgreSQL (development)
    DATABASE_URL = _DEV_PG

# Final check — validate the URL was resolved
if not (DATABASE_URL.startswith("sqlite:///") or DATABASE_URL.startswith("postgresql://")):
    raise RuntimeError(f"Unexpected DATABASE_URL format: {DATABASE_URL}")

if DATABASE_URL.startswith("postgresql://"):
    from urllib.parse import urlparse
    parsed = urlparse(DATABASE_URL)
    print(f"  [config] Using PostgreSQL: {parsed.hostname}:{parsed.port}/{parsed.path.lstrip('/')}")
