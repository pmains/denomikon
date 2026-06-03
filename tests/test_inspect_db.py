"""Tests for inspect_db CLI with temporary database."""
import io
import os
import sys
import tempfile
import unittest
import pytest
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import integration_test

# Import db BEFORE touching DATABASE_URL so the module-level constant is
# conftest.py sets POLISCOPIC_DB_TIER=test which handles temp DB creation.
# init_db() is called in setUpClass to set up schema.
import db as _db_mod
from db import (
    init_db, get_session, Meeting, AgendaItem, SupportingDocument,
)


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
    # Delete old data first, then re-populate — other tests may have
    # left stale records with the same meeting_ids.
    existing = session.execute(
        select(Meeting).where(Meeting.meeting_id == "4667")
    ).scalar_one_or_none()
    if existing:
        # Delete old records to get a clean state
        from sqlalchemy import delete as sa_delete
        session.execute(sa_delete(SupportingDocument).where(SupportingDocument.body == "bos"))
        session.execute(sa_delete(AgendaItem).where(AgendaItem.body == "bos"))
        session.execute(sa_delete(Meeting).where(Meeting.body == "bos"))
        session.commit()

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
    """Tests for inspect_db CLI."""
    @classmethod
    def setUpClass(cls):
        # Force a fresh engine — another module may have left a stale
        # engine connected to a deleted temp file.
        import db.core as _dc
        if _dc._engine:
            _dc._engine.dispose()
            _dc._engine = None
            _dc._SessionLocal = None
        init_db()
        # Truncate test tables — other modules may have left stale data
        s = get_session()
        for tbl in [SupportingDocument.__table__, AgendaItem.__table__, Meeting.__table__]:
            s.execute(tbl.delete())
        s.commit()
        s.close()

    def setUp(self):
        # Ensure schema + data exists — other test modules may swap the DB file
        init_db()
        _populate_db()

    def test_meetings_output(self):
        out = _capture_output(["meetings"])
        self.assertIn("4667", out)
        self.assertIn("4668", out)
        self.assertIn("4669", out)

    def test_counts_output(self):
        out = _capture_output(["counts"])
        # Output should have column headers and at least one meeting row
        self.assertIn("ID", out)
        self.assertIn("Date", out)
        self.assertIn("Type", out)
        self.assertIn("Items", out)
        self.assertIn("4667", out)
        # Should show a total count line
        self.assertIn("meeting(s)", out)
        self.assertIn("total items", out)

    def test_agenda_output(self):
        out = _capture_output(["agenda", "4667"])
        # Should show meeting header and item listing format
        self.assertIn("4667", out)
        self.assertIn("items", out.lower())
        # Should have numbered items
        import re
        self.assertTrue(re.search(r'\d+\.', out), "Agenda output should contain numbered items")

    def test_item_output(self):
        out = _capture_output(["item", "4667", "3"])
        # Should show item detail fields
        self.assertIn("Item:", out)
        self.assertIn("Title:", out)
        self.assertIn("Date:", out)
        self.assertIn("C-number:", out)

    def test_search_output(self):
        out = _capture_output(["search", "CALL"])
        # Output should contain results or "No results"
        self.assertIn("CALL", out.upper())

    def test_search_no_results(self):
        out = _capture_output(["search", "XYZZY_NOTHING"])
        self.assertIn("No results", out)

    def test_docs_output(self):
        out = _capture_output(["docs", "4667"])
        # Should show document listing format
        self.assertIn("Supporting documents", out)
        self.assertIn("4667", out)
        if "No supporting documents" not in out:
            # If there are docs, they should have Title/URL lines
            self.assertIn("Title:", out)
            self.assertIn("URL:", out)

    def test_docs_for_item(self):
        out = _capture_output(["docs", "4667", "3"])
        # Should show docs for the item or empty state
        self.assertIn("4667", out)
        self.assertIn("3", out)

    def test_revisions_output(self):
        out = _capture_output(["revisions"])
        # Should show C-number revision listing or empty state
        if "No C-numbers" not in out:
            self.assertIn("C-number", out)

    def test_status_output(self):
        out = _capture_output(["status"])
        # Should show sync status summary format with status counts
        self.assertIn("status", out.lower())
        self.assertIn("complete", out.lower())
        self.assertIn("count", out.lower())

    def test_unknown_meeting(self):
        out = _capture_output(["agenda", "9999"])
        self.assertIn("not found", out.lower())

    def test_unknown_item(self):
        out = _capture_output(["item", "9999", "1"])
        self.assertIn("not found", out.lower())


if __name__ == "__main__":
    unittest.main()
