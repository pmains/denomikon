"""Flask routes package — app factory and blueprint registration."""

import logging
import os
import sys
import time
from functools import wraps
from pathlib import Path
from flask import Flask
from typing import Callable

log = logging.getLogger(__name__)

_here = Path(__file__).resolve().parent.parent  # repo root
_scripts_dir = _here / "scripts"

sys.path.insert(0, str(_scripts_dir))

# Load .env so DATABASE_URL is available
from dotenv import load_dotenv
load_dotenv(_here / ".env")

_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    _database_url = "postgresql://poliscopic:CHANGEME@100.91.173.66:5432/poliscopic_dev"
    os.environ["DATABASE_URL"] = _database_url

print(f"Database URL: {_database_url}", file=sys.stderr)


# ── Cache version — bump to invalidate all cached pages ──────────────────
_CACHE_VERSION = "v10"

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


def create_app() -> Flask:
    """Create and configure the Flask application."""
    from flask import Flask, render_template, request

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

    # ── Login manager ────────────────────────────────────────────────────
    _disable_admin = os.environ.get("POLISCOPIC_DISABLE_ADMIN", "").lower() in ("true", "1", "yes")

    from flask_login import LoginManager
    from db.newsroom import AdminUser

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def _load_user(user_id):
        from db.core import get_session
        from sqlalchemy import select
        session = get_session()
        user = session.get(AdminUser, int(user_id))
        session.close()
        return user

    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

    # Explicit session cookie settings for broader browser compatibility
    app.config.update(
        SESSION_COOKIE_NAME="poliscopic_session",  # Avoid conflicts with old cookies
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=False,  # HTTP on localhost
        PERMANENT_SESSION_LIFETIME=3600 * 24,  # 24 hours
        SESSION_REFRESH_EACH_REQUEST=False,
        WTF_CSRF_ENABLED=False,  # Disable CSRF for dev
    )

    # ── Markdown filter ──────────────────────────────────────────────────
    import markdown as _md

    @app.template_filter("markdown")
    def _render_markdown(text):
        if not text:
            return ""
        return _md.markdown(
            text,
            extensions=["fenced_code", "tables", "sane_lists"],
        )

    # ── Arizona timezone filter ──────────────────────────────────────────
    from zoneinfo import ZoneInfo
    _UTC = ZoneInfo("UTC")
    _AZ = ZoneInfo("America/Phoenix")

    @app.template_filter("az_date")
    def _format_az_date(dt, fmt="%B %d, %Y"):
        """Convert a UTC datetime to Arizona time and format it."""
        import datetime as _dt
        if dt is None:
            return ""
        # Handle strings (raw SQL with text() may return date as string)
        if isinstance(dt, str):
            # Just return the date portion of the string
            return dt[:10]
        # Handle plain date objects (no tzinfo attribute)
        if isinstance(dt, _dt.date) and not isinstance(dt, _dt.datetime):
            return dt.strftime(fmt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt.astimezone(_AZ).strftime(fmt)

    # ── Initialize newsroom tables ───────────────────────────────────────
    from db.newsroom import init_newsroom_db, seed_default_tags, seed_default_users, seed_default_topics
    init_newsroom_db()
    seed_default_tags()
    seed_default_users()
    seed_default_topics()

    # ── Register blueprints ──────────────────────────────────────────────
    from routes.meetings import meetings_bp
    from routes.bodies import bodies_bp
    from routes.members import members_bp
    from routes.auth import auth_bp
    from routes.admin import admin_bp
    from routes.articles import articles_bp
    from routes.themes import themes_bp
    from routes.topics import topics_bp
    from routes.entities import entities_bp
    from routes.entity_annotation import annotation_bp
    from routes.podcast import podcast_bp
    app.register_blueprint(meetings_bp)
    app.register_blueprint(bodies_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(articles_bp)
    app.register_blueprint(themes_bp)
    app.register_blueprint(topics_bp)
    app.register_blueprint(entities_bp)
    app.register_blueprint(podcast_bp)
    app.register_blueprint(annotation_bp)

    # Admin and auth are only registered when admin is enabled
    if not _disable_admin:
        app.register_blueprint(auth_bp)
        app.register_blueprint(admin_bp)

    if _disable_admin:
        @app.route("/admin")
        @app.route("/admin/")
        @app.route("/admin/<path:_path>")
        @app.route("/login")
        def _admin_disabled(_path=None):
            from flask import abort
            abort(404)

    @app.route("/about")
    def about():
        return render_template("about.html")

    return app
