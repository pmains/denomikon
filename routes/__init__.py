"""Flask routes package — app factory and blueprint registration."""

import logging
import os
import sys
import time
from functools import wraps
from pathlib import Path

log = logging.getLogger(__name__)

_here = Path(__file__).resolve().parent.parent  # repo root
_scripts_dir = _here / "scripts"
_expected_db = _here / "data" / "maricopa.sqlite"

sys.path.insert(0, str(_scripts_dir))

_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    _database_url = f"sqlite:///{_expected_db}"
    os.environ["DATABASE_URL"] = _database_url

if not _expected_db.exists():
    print(
        f"WARNING: Database file not found at {_expected_db}",
        file=sys.stderr,
    )
    print(
        "  Sync some meetings first:\n"
        f"    .venv/bin/python scripts/agenda_scraper.py\n"
        "    --sync --start-date=2025-01-01 --end-date=2025-12-31",
        file=sys.stderr,
    )

print(f"Database URL: {_database_url}", file=sys.stderr)
print(f"Data path:    {_expected_db}", file=sys.stderr)
print(f"DB exists:    {_expected_db.exists()}", file=sys.stderr)


# ── Cache version — bump to invalidate all cached pages ──────────────────
_CACHE_VERSION = "v9"

# ── Shared template constants ────────────────────────────────────────────
SYNC_STATUS_BADGES = {
    "complete": "success",
    "failed": "danger",
    "partial": "warning",
    "manual_review": "secondary",
    "pending": "info",
}

_cache_instance = None


def get_cache():
    """Return the shared cache instance (set during create_app)."""
    return _cache_instance


def _cache(timeout=60, query_string=False):
    """Apply Flask-Caching if available, otherwise no-op.

    Versions the cache key via _CACHE_VERSION so reclassification or
    data migrations naturally invalidate stale cached pages.
    """
    if _cache_instance:
        original_cached = _cache_instance.cached(timeout=timeout, query_string=query_string)

        def _wrapper(fn):
            @wraps(fn)
            def _versioned(*args, **kwargs):
                from flask import request
                old = dict(request.args) if hasattr(request, 'args') else {}
                try:
                    if hasattr(request, 'args'):
                        request.args = request.args.copy()
                        request.args['_cv'] = _CACHE_VERSION
                    return original_cached(fn)(*args, **kwargs)
                finally:
                    if old and hasattr(request, 'args'):
                        request.args = type(request.args)(old)
            return _versioned
        return _wrapper
    return lambda f: f


def create_app():
    """Create and configure the Flask application."""
    from flask import Flask, request

    app = Flask(__name__,
                 template_folder=str(_here / "templates"),
                 static_folder=str(_here / "static"))

    # ── Cache setup ──────────────────────────────────────────────────────
    global _cache_instance
    try:
        from flask_caching import Cache
        _cache_instance = Cache(app, config={
            "CACHE_TYPE": "FileSystemCache",
            "CACHE_DIR": str(_here / ".cache" / "flask-cache"),
            "CACHE_DEFAULT_TIMEOUT": 60,
            "CACHE_THRESHOLD": 200,
        })
        log.info("Flask-Caching enabled (FileSystemCache, 60s default)")
    except ImportError:
        _cache_instance = None
        log.warning("Flask-Caching not installed — install with: pip install Flask-Caching")

    # ── Seed default data on startup ─────────────────────────────────────
    from db import seed_default_jurisdictions
    seed_default_jurisdictions()

    # ── Request timing ───────────────────────────────────────────────────
    @app.before_request
    def _start_timer():
        request._start_time = time.monotonic()

    @app.after_request
    def _log_timing(response):
        elapsed = time.monotonic() - getattr(request, "_start_time", time.monotonic())
        if elapsed > 1.0:
            log.warning("%s %.1fs", request.path, elapsed)
        return response

    # ── Register blueprints ──────────────────────────────────────────────
    from routes.meetings import meetings_bp
    from routes.bodies import bodies_bp
    from routes.permits import permits_bp
    from routes.members import members_bp
    from routes.codes import codes_bp

    app.register_blueprint(meetings_bp)
    app.register_blueprint(bodies_bp)
    app.register_blueprint(permits_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(codes_bp)

    return app
