"""Database engine, session, and connection management."""

import os
import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Default to a TEMP file that is NEVER the production database.
# The production app explicitly sets DATABASE_URL in os.environ before
# importing this module (see routes/__init__.py).  If you're reading
# this because tests deleted production data, you found the bug.
_DEFAULT_DB = os.environ.get(
    "POLISCOPIC_DEFAULT_DB",
    f"sqlite:///{tempfile.gettempdir()}/poliscopic_dev.sqlite",
)
DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_DB)

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
