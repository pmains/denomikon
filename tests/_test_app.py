"""Tests for the Flask web application (app.py).

Run individually:  python -m unittest tests.test_app
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import integration_test

@integration_test
class TestAppRoutes(unittest.TestCase):
    """Test Flask app routes return valid responses."""

    @classmethod
    def setUpClass(cls):
        db_path = Path(__file__).resolve().parents[1] / "data" / "maricopa.sqlite"
        if not db_path.exists():
            raise unittest.SkipTest("maricopa.sqlite not found")
        abs_db = f"sqlite:///{db_path}"

        # Set DB URL before any imports resolve
        import os
        os.environ.pop("DATABASE_URL", None)
        os.environ["DATABASE_URL"] = abs_db

        import scripts.db as _db
        _db._engine = None
        _db._SessionLocal = None

        import importlib
        import app as _app_mod
        importlib.reload(_app_mod)
        cls.app = _app_mod.app

    def setUp(self):
        self.client = self.app.test_client()

    def test_index_redirects_to_meetings(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)

    def test_meetings_list_returns_200(self):
        resp = self.client.get("/meetings")
        self.assertEqual(resp.status_code, 200)

    def test_meeting_detail_returns_200(self):
        resp = self.client.get("/meetings/4669")
        self.assertEqual(resp.status_code, 200)

    def test_meeting_detail_not_found(self):
        resp = self.client.get("/meetings/bos/9999")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    # Set up DB before importing the app
    db_path = Path(__file__).resolve().parents[1] / "data" / "maricopa.sqlite"
    if db_path.exists():
        import os
        abs_db = f"sqlite:///{db_path}"
        os.environ.pop("DATABASE_URL", None)
        os.environ["DATABASE_URL"] = abs_db
        import scripts.db
        scripts.db._engine = None
        scripts.db._SessionLocal = None
    unittest.main()
