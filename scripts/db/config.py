"""
Database configuration — three-tier separation.

TIERS
-----

  TIER          DATABASE FILE                  PURPOSE
  ────────────  ─────────────────────────────  ─────────────────────────────────
  development   data/maricopa.sqlite           Daily work: scraping + Flask app
  test          tempfile (auto-created)        Unit/integration tests
  production    poliscopic.com:/opt/.../db     Public-facing site (via sync.sh)


HOW TO USE
----------

Development (default — no env var needed):
    python scripts/agenda_scraper.py peoria --sync --year=2026

    Uses data/maricopa.sqlite.  The Flask dev server at 127.0.0.1:5000
    reads from this same file.  Scrape, then reload — the data is there.

Test (pytest sets this automatically via conftest.py):
    pytest tests/

    Creates a temp SQLite file, runs tests, destroys it.  Never touches
    the development or production databases.

Production (sync.sh handles this — not for direct use):
    ./sync.sh

    Pushes data/maricopa.sqlite → poliscopic.com.  The production gunicorn
    process reads from /opt/poliscopic/data/maricopa.sqlite.

To override the database for a one-off command:
    DATABASE_URL="sqlite:///data/maricopa.sqlite" python scripts/agenda_scraper.py ...
"""

import os
from pathlib import Path

# ── Default: the local development database ─────────────────────────────
# This is the single file shared by:
#   - The Flask dev server (routes/__init__.py overrides to this path)
#   - CLI scraping commands (agenda_scraper.py, etc.)
#   - One-off analysis scripts
_DEV_DB = str((Path(__file__).resolve().parent.parent.parent / "data" / "maricopa.sqlite").resolve())

# ── Resolution order ──────────────────────────────────────────────────
# 1. DATABASE_URL env var (explicit override for one-off commands)
# 2. POLISCOPIC_DB_TIER=test  →  tempfile (used by pytest)
# 3. POLISCOPIC_DB_TIER=production  →  error out (sync.sh handles this)
# 4. Default  →  development database
#
# NEVER set DATABASE_URL to the production database path.  Production
# sync is handled exclusively by sync.sh, which creates a backup before
# overwriting.
#
# NEVER remove or rename data/maricopa.sqlite without confirming that
# all synced data has been backed up.

_DB_TIER = os.environ.get("POLISCOPIC_DB_TIER", "").lower().strip()

if "DATABASE_URL" in os.environ:
    # Explicit URL overrides tier selection
    DATABASE_URL = os.environ["DATABASE_URL"]
elif _DB_TIER == "development":
    DATABASE_URL = f"sqlite:///{_DEV_DB}"
elif _DB_TIER == "test":
    import tempfile
    DATABASE_URL = f"sqlite:///{tempfile.mktemp(suffix='.sqlite')}"
elif _DB_TIER == "production":
    raise RuntimeError(
        "POLISCOPIC_DB_TIER=production is not for direct use. "
        "Use sync.sh to deploy to poliscopic.com."
    )
else:
    print(
        "No database tier selected. Set POLISCOPIC_DB_TIER or use --dev / --test:\n"
        "  POLISCOPIC_DB_TIER=development  → data/maricopa.sqlite\n"
        "  POLISCOPIC_DB_TIER=test          → temporary file (destroyed after)\n"
        "  DATABASE_URL=sqlite:///path      → explicit database path\n"
        "\n"
        "Example: python scripts/agenda_scraper.py peoria --sync\n"
        "  (requires POLISCOPIC_DB_TIER=development to be set in .env or environment)",
        file=__import__("sys").stderr,
    )
    raise SystemExit(1)

# Final check — validate the URL was resolved
if not (DATABASE_URL.startswith("sqlite:///") or DATABASE_URL.startswith("postgresql://")):
    raise RuntimeError(f"Unexpected DATABASE_URL format: {DATABASE_URL}")

if DATABASE_URL.startswith("postgresql://"):
    from urllib.parse import urlparse
    parsed = urlparse(DATABASE_URL)
    print(f"  [config] Using PostgreSQL: {parsed.hostname}:{parsed.port}/{parsed.path.lstrip('/')}")
