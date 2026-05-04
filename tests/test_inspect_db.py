"""Tests for the inspect_db CLI script."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from inspect_db import main, get_session, Meeting, AgendaItem
from sqlalchemy import select


def setUpModule():
    """Verify test database has data before running tests."""
    session = get_session()
    meeting = session.execute(
        select(Meeting).where(Meeting.meeting_id == "4667")
    ).scalar_one_or_none()
    session.close()
    if not meeting:
        raise unittest.SkipTest(
            "No data for meeting 4667. Run --sync --date=2026-04-22 first."
        )


class TestInspectDbMeetings(unittest.TestCase):
    def test_meetings_exits_cleanly(self):
        """meetings command should produce output without errors."""
        rc = main(["meetings"])
        self.assertEqual(rc, 0)

    def test_meetings_shows_4667(self):
        """meetings list should include meeting 4667."""
        out = _capture_output(["meetings"])
        self.assertIn("4667", out)
        self.assertIn("Formal", out)


class TestInspectDbCounts(unittest.TestCase):
    def test_counts_exits_cleanly(self):
        rc = main(["counts"])
        self.assertEqual(rc, 0)

    def test_counts_shows_86(self):
        """Item counts should show 86 for meeting 4667."""
        out = _capture_output(["counts"])
        self.assertIn("86", out)


class TestInspectDbAgenda(unittest.TestCase):
    def test_agenda_exits_cleanly(self):
        rc = main(["agenda", "4667"])
        self.assertEqual(rc, 0)

    def test_agenda_shows_item_1(self):
        out = _capture_output(["agenda", "4667"])
        self.assertIn("ROLL CALL", out)

    def test_agenda_shows_item_86(self):
        out = _capture_output(["agenda", "4667"])
        self.assertIn("summary of current events", out.lower())

    def test_agenda_unknown_meeting(self):
        out = _capture_output(["agenda", "9999"])
        self.assertIn("not found", out.lower())


class TestInspectDbSearch(unittest.TestCase):
    def test_search_exits_cleanly(self):
        rc = main(["search", "Test"])
        self.assertEqual(rc, 0)

    def test_search_finds_items(self):
        out = _capture_output(["search", "ROLL CALL"])
        self.assertIn("ROLL CALL", out)

    def test_search_no_results(self):
        out = _capture_output(["search", "XYZZY_NOTHING"])
        self.assertIn("No results", out)

    def test_search_limit(self):
        out = _capture_output(["search", "the", "--limit", "3"])
        self.assertIn("limit of 3", out)


class TestInspectDbItem(unittest.TestCase):
    def test_item_exits_cleanly(self):
        rc = main(["item", "4667", "1"])
        self.assertEqual(rc, 0)

    def test_item_shows_detail(self):
        out = _capture_output(["item", "4667", "85"])
        self.assertIn("CALL TO THE PUBLIC", out)
        self.assertIn("4667-85-item", out)
        self.assertIn("Public comment", out)

    def test_item_unknown_meeting(self):
        out = _capture_output(["item", "9999", "1"])
        self.assertIn("not found", out.lower())


def _capture_output(argv: list[str]) -> str:
    """Run main() with the given args and return stdout as a string."""
    import io

    out = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = out
    try:
        main(argv)
    finally:
        sys.stdout = old_stdout
    return out.getvalue()


if __name__ == "__main__":
    unittest.main()
