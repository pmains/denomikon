"""Tests for inspect_db CLI with temporary database."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import integration_test

# Import db BEFORE touching DATABASE_URL so the module-level constant is
# the production default.  We'll switch to a temp database via
# set_database_url() before each class — see _make_fresh_db().
import db as _db_mod
from db import (
    init_db, get_session, Meeting, AgendaItem, SupportingDocument,
    set_database_url,
)

_test_db_path = None  # set by _make_fresh_db()


def _make_fresh_db():
    """Create a new temp database, delete old one, reset engine.

    Uses set_database_url() to safely switch the module-level
    DATABASE_URL constant — never touches os.environ, so the
    production database path is never accidentally overwritten.
    """
    global _test_db_path
    import os as _os
    # Delete old DB file
    if _test_db_path:
        try:
            _os.unlink(_test_db_path)
        except FileNotFoundError:
            pass
    # Create new temp file and switch the database URL
    _test_db_path = tempfile.mktemp(suffix=".sqlite")
    set_database_url(f"sqlite:///{_test_db_path}")
    init_db()


def _capture_output(argv: list[str]) -> str:
    """Run inspect_db.main() with the given args and return stdout as string."""
    from inspect_db import main
    out = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = out
    try:
        main(argv)
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout
    return out.getvalue()


def _populate_db():
    """Insert minimal test data. Safe to call multiple times."""
    from sqlalchemy import select, func as sa_func

    session = get_session()
    existing = session.execute(
        select(Meeting).where(Meeting.meeting_id == "4667")
    ).scalar_one_or_none()
    if existing:
        session.close()
        return

    for mid, date, mtype, title in [
        ("4667", "2026-04-22", "Formal", "Formal Meeting"),
        ("4668", "2026-05-04", "Informal", "Special Session"),
        ("4669", "2026-05-06", "Formal", "Formal Meeting"),
    ]:
        session.add(Meeting(body="bos", meeting_id=mid, meeting_date=date, meeting_type=mtype, meeting_title=title))
    session.commit()

    for mid, num, title, cnum in [
        ("4667", 1, "ROLL CALL", ""),
        ("4667", 2, "INVOCATION", ""),
        ("4667", 3, "PUBLIC COMMENT", "C-86-26-001-X-00"),
        ("4669", 1, "CALL TO ORDER", ""),
    ]:
        session.add(AgendaItem(
            body="bos",
            meeting_id=mid,
            agenda_item_number=num,
            agenda_item_id=f"{mid}-{num}-item",
            agenda_item_title=title,
            agenda_item_text=f"Text for {title}",
            agenda_item_url=f"https://example.com/item?m={mid}&n={num}",
            source_url=f"https://example.com/meeting?id={mid}",
            c_number=cnum,
            c_number_base=cnum.rpartition("-")[0] if cnum else "",
            c_number_revision=cnum.rpartition("-")[2] if cnum else None,
        ))
    session.commit()

    session.add(SupportingDocument(
        body="bos",
        meeting_id="4667", agenda_item_number=3,
        agenda_item_id=3,
        c_number="C-86-26-001-X-00",
        document_title="Public Comment Doc",
        document_url="https://example.com/doc.pdf",
        file_extension="pdf",
    ))
    session.commit()
    session.close()


@integration_test
class TestInspectDbMeetings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _make_fresh_db()
        _populate_db()

    @classmethod
    def tearDownClass(cls):
        """Reset the engine for downstream tests."""
        set_database_url(_db_mod.DATABASE_URL)

    def test_meetings_output(self):
        out = _capture_output(["meetings"])
        self.assertIn("4667", out)
        self.assertIn("4668", out)
        self.assertIn("4669", out)

    def test_counts_output(self):
        out = _capture_output(["counts"])
        self.assertIn("4667", out)
        self.assertIn("4 total items", out)

    def test_agenda_output(self):
        out = _capture_output(["agenda", "4667"])
        self.assertIn("ROLL CALL", out)
        self.assertIn("INVOCATION", out)
        self.assertIn("PUBLIC COMMENT", out)

    def test_item_output(self):
        out = _capture_output(["item", "4667", "3"])
        self.assertIn("C-86-26-001-X-00", out)
        self.assertIn("Public Comment Doc", out)

    def test_search_output(self):
        out = _capture_output(["search", "ROLL"])
        self.assertIn("ROLL CALL", out)

    def test_search_no_results(self):
        out = _capture_output(["search", "XYZZY_NOTHING"])
        self.assertIn("No results", out)

    def test_docs_output(self):
        out = _capture_output(["docs", "4667"])
        self.assertIn("Public Comment Doc", out)

    def test_docs_for_item(self):
        out = _capture_output(["docs", "4667", "3"])
        self.assertIn("Public Comment Doc", out)
        out_no = _capture_output(["docs", "4667", "1"])
        self.assertNotIn("Public Comment Doc", out_no)

    def test_revisions_output(self):
        out = _capture_output(["revisions"])
        self.assertIn("C-86-26-001-X", out)

    def test_status_output(self):
        out = _capture_output(["status"])
        self.assertIn("complete", out.lower())

    def test_unknown_meeting(self):
        out = _capture_output(["agenda", "9999"])
        self.assertIn("not found", out.lower())

    def test_unknown_item(self):
        out = _capture_output(["item", "9999", "1"])
        self.assertIn("not found", out.lower())


if __name__ == "__main__":
    unittest.main()
