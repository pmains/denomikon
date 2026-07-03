"""Database engine, session, and connection management."""

import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Database URL resolution (see db/config.py for the three-tier system):
#   1. DATABASE_URL env var (explicit override)
#   2. POLISCOPIC_DB_TIER=test → tempfile (pytest)
#   3. Default → data/maricopa.sqlite (development, shared with Flask)
from db.config import DATABASE_URL as _RESOLVED_DB
DATABASE_URL = _RESOLVED_DB

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        connect_args = {}
        url = DATABASE_URL
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            _set_sqlite_pragmas(_engine)
    return _engine


def _set_sqlite_pragmas(engine):
    """Apply performance-oriented PRAGMAs to a SQLite connection."""
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
        cursor.execute("PRAGMA cache_size=-20000;")  # 20 MB cache
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


def set_database_url(url: str):
    """Switch the database URL at runtime.

    Used ONLY by test fixtures to point the engine at a temporary database.
    Disposes any existing engine and resets the session factory so that
    the next get_engine() / get_session() call creates fresh connections
    to the new URL.

    .. warning::
       Do NOT call this in production.  Set DATABASE_URL via the
       environment variable before the first import of this module.
    """
    global DATABASE_URL, _engine, _SessionLocal
    if _engine:
        _engine.dispose()
    DATABASE_URL = url
    _engine = None
    _SessionLocal = None


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), future=True)
    return _SessionLocal()


def ensure_public_body(session, body_code: str, name_hint: str = "",
                       jurisdiction_id: int | None = None) -> int | None:
    """Ensure a PublicBody row exists for the given body_code.

    Returns the public_body.id, or None if no jurisdiction can be resolved.
    Used by scrapers to auto-register body codes found in meeting data
    that don't yet exist in the public_bodies table.
    """
    from db.models import PublicBody, Jurisdiction
    from sqlalchemy import select

    existing = session.execute(
        select(PublicBody).where(PublicBody.body_code == body_code)
    ).scalar_one_or_none()
    if existing:
        return existing.id

    # Resolve jurisdiction from jurisdiction_id or slug prefixes
    if not jurisdiction_id:
        # Extract jurisdiction slug from body_code prefix (e.g. "chandler-cc" → "chandler")
        prefix = body_code.split("-")[0] if "-" in body_code else ""
        jur = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == prefix)
        ).scalar_one_or_none()
        if not jur and prefix:
            # Maybe it's a state-county style (e.g. "az-maricopa")
            jur = session.execute(select(Jurisdiction)).first()
        if jur:
            jurisdiction_id = jur.id

    if not jurisdiction_id:
        return None

    # Build a display name from the body_code or name_hint
    name = name_hint.strip() if name_hint.strip() else body_code.replace("-", " ").title()

    def _make_slug(name: str) -> str:
        import re
        s = name.lower().replace("&", "and").replace("/", "-")
        s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
        return s[:64]

    pb = PublicBody(
        jurisdiction_id=jurisdiction_id,
        name=name,
        slug=slugify(name)[:64],
        body_code=body_code,
        body_type="advisory_general",
    )
    session.add(pb)
    session.flush()
    return pb.id
