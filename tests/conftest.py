"""pytest configuration — auto-select test database tier and provide fixtures.

This MUST be set at module level (not in a hook) so it takes effect before
any test module imports the db package.
"""

import os
import sys
import pytest

os.environ["POLISCOPIC_DB_TIER"] = "test"

# Tests that should only run when explicitly requested (not in CI)
# They query the development DB for data quality thresholds.
collect_ignore = ["test_sync_data_integrity.py"]

# Ensure project scripts are importable
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_project_root, "scripts"))
sys.path.insert(0, _project_root)


@pytest.fixture(scope="class", autouse=True)
def _guard_db_url():
    """Save and restore DATABASE_URL and engine state around every test class.

    Some test modules change DATABASE_URL globally via set_database_url().
    This fixture restores the original URL AND disposes the engine, so
    downstream tests get a fresh connection to the correct database.
    """
    import db.core
    saved = db.core.DATABASE_URL
    yield
    if db.core.DATABASE_URL != saved:
        # Restore the original URL
        db.core.set_database_url(saved)
        # Dispose the stale engine so get_session() creates a fresh one
        # pointing at the restored URL, not the leaked temp file.
        if db.core._engine:
            db.core._engine.dispose()
            db.core._engine = None
            db.core._SessionLocal = None
        # Re-initialize schema on the restored DB — another module
        # may have called init_db() on their leaked temp file, not ours.
        from db import init_db
        init_db()


@pytest.fixture(scope="session")
def test_db_url():
    """Return the active test database URL."""
    import db.core
    return db.core.DATABASE_URL


@pytest.fixture(scope="function")
def fresh_session():
    """Provide a clean SQLAlchemy session on a fresh test DB.

    Uses a function-scoped temp DB to isolate tests from each other.
    """
    import tempfile
    import db.core
    from db.core import get_engine, get_session

    # Save the current URL
    saved_url = db.core.DATABASE_URL

    # Create a new temp DB for this test
    tmp = tempfile.mktemp(suffix=".sqlite")
    new_url = f"sqlite:///{tmp}"
    db.core.set_database_url(new_url)

    from db import init_db
    init_db()

    session = get_session()
    try:
        yield session
    finally:
        session.close()
        # Restore the original test DB URL
        db.core.set_database_url(saved_url)
        # Clean up the temp file
        try:
            os.unlink(tmp)
        except OSError:
            pass
