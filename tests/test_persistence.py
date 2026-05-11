"""Tests for db.py persistence layer using temporary SQLite."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test

# Import db FIRST so the module-level DATABASE_URL defaults to production.
# We switch to a temp database via set_database_url() below.
import db as _db_mod
from db import (
    init_db,
    get_session,
    Meeting,
    AgendaItem,
    SupportingDocument,
    create_or_get_meeting,
    replace_meeting_data_safe,
    upsert_meeting,
    update_sync_status,
    get_meetings_by_date_range,
    get_meetings_by_status,
    persist_meeting,
    backfill_meeting_normalization,
    set_database_url,
)
from sqlalchemy import select, func, inspect as sa_inspect

# Create a temp file and switch the module-level DATABASE_URL to it.
# This NEVER touches os.environ, so no other process can accidentally
# pick up the test path.
_test_db_path = tempfile.mktemp(suffix=".sqlite")
set_database_url(f"sqlite:///{_test_db_path}")
init_db()


def _reset_db_engine():
    """Dispose and reset the DB engine so the next get_engine() creates
    a fresh connection to the current DATABASE_URL."""
    if _db_mod._engine:
        _db_mod._engine.dispose()
    _db_mod._engine = None
    _db_mod._SessionLocal = None


@integration_test
class TestInitDbIdempotent(unittest.TestCase):
    def test_double_init_is_idempotent(self):
        """init_db() is idempotent — calling it twice does not change schema."""
        init_db()
        from sqlalchemy import inspect as sa_inspect
        session = get_session()
        tables_before = set(sa_inspect(session.get_bind()).get_table_names())
        init_db()  # second call
        tables_after = set(sa_inspect(session.get_bind()).get_table_names())
        self.assertEqual(tables_before, tables_after)
        self.assertIn("meetings", tables_after)
        session.close()

    def test_meeting_table_exists(self):
        """After init_db, the meetings table should exist."""
        init_db()
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("meetings", tables)
        session.close()


@integration_test
class TestPersistMeeting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Clean tables before each test
        self.session.execute(AgendaItem.__table__.delete())
        self.session.execute(SupportingDocument.__table__.delete())
        self.session.execute(Meeting.__table__.delete())
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _make_meeting_dict(self, meeting_id="9999", date="2026-01-01", mtype="Formal", title="Test Meeting"):
        return {
            "meeting_id": meeting_id,
            "meeting_date": date,
            "meeting_type": mtype,
            "meeting_title": title,
            "source_url": f"https://example.com/meeting?id={meeting_id}",
        }

    def _make_items(self, meeting_id="9999", count=3):
        items = []
        for i in range(1, count + 1):
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": i,
                "agenda_item_id": f"{meeting_id}-{i}-item",
                "agenda_item_title": f"Item {i}",
                "agenda_item_text": f"Text for item {i}",
                "agenda_item_url": f"https://example.com/item?m={meeting_id}&n={i}",
                "vote_or_action": "",
                "source_body": "Board of Supervisors",
                "source_url": f"https://example.com/meeting?id={meeting_id}",
                "c_number": f"C-86-26-{100+i:03d}-X-00" if i == 2 else "",
                "c_number_base": f"C-86-26-{100+i:03d}-X" if i == 2 else "",
                "c_number_revision": "00" if i == 2 else None,
            })
        return items

    def _make_docs(self, meeting_id="9999", item_number=2, count=1):
        docs = []
        for i in range(count):
            docs.append({
                "agenda_item_id": item_number,
                "meeting_id": meeting_id,
                "agenda_item_number": item_number,
                "c_number": f"C-86-26-{100+item_number:03d}-X-00",
                "c_number_base": f"C-86-26-{100+item_number:03d}-X",
                "c_number_revision": None,
                "document_title": f"Doc {i+1} for item {item_number}",
                "document_url": f"https://example.com/doc?m={meeting_id}&i={item_number}&d={i}",
                "document_type": "PDF",
                "file_name": f"doc_{i+1}.pdf",
                "file_extension": "pdf",
            })
        return docs

    def test_inserts_meeting(self):
        """replace_meeting_data_safe creates a meeting and persists items."""
        count = replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            self._make_items(),
        )
        self.session.commit()
        self.assertEqual(count, 3)
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "9999")
        ).scalar_one_or_none()
        self.assertIsNotNone(meeting)
        self.assertEqual(meeting.meeting_date, "2026-01-01")
        self.assertEqual(meeting.sync_status, "complete")

    def test_replaces_old_items(self):
        """Re-running persist replaces old items, doesn't duplicate."""
        # First run with 3 items
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            self._make_items(count=3),
        )
        self.session.commit()
        # Second run with 5 items
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            self._make_items(count=5),
        )
        self.session.commit()
        # Should have 5 items, not 8
        count = self.session.execute(
            select(func.count(AgendaItem.id)).where(AgendaItem.meeting_id == "9999")
        ).scalar()
        self.assertEqual(count, 5)

    def test_replaces_supporting_docs(self):
        """Re-running persist replaces old docs, doesn't duplicate."""
        docs1 = self._make_docs(item_number=2, count=2)
        items = self._make_items(count=3)
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            items, supporting_doc_dicts=docs1,
        )
        self.session.commit()

        docs2 = self._make_docs(item_number=2, count=1)
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            items, supporting_doc_dicts=docs2,
        )
        self.session.commit()

        # Should have 1 doc, not 3
        count = self.session.execute(
            select(func.count(SupportingDocument.id))
            .where(SupportingDocument.meeting_id == "9999")
        ).scalar()
        self.assertEqual(count, 1)

    def test_metadata_updates_on_resync(self):
        """Re-syncing updates meeting_date, meeting_type, title, and display_name."""
        init_db()  # Ensure migration columns exist
        d1 = self._make_meeting_dict(meeting_id="9999", date="2026-01-01", mtype="Formal", title="")
        replace_meeting_data_safe(self.session, "bos", "9999", d1, self._make_items(count=1))
        self.session.commit()

        d2 = self._make_meeting_dict(meeting_id="9999", date="2026-01-02", mtype="Special", title="Emergency Meeting")
        replace_meeting_data_safe(self.session, "bos", "9999", d2, self._make_items(count=1))
        self.session.commit()

        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "9999")
        ).scalar_one_or_none()
        self.assertEqual(meeting.meeting_date, "2026-01-02")
        self.assertEqual(meeting.meeting_type, "Special")
        self.assertIsNotNone(meeting.display_name)

    def test_c_number_persists(self):
        """C-number fields persist correctly on agenda items."""
        items = self._make_items(count=3)
        replace_meeting_data_safe(self.session, "bos", "9999", self._make_meeting_dict(), items)
        self.session.commit()

        item2 = self.session.execute(
            select(AgendaItem).where(
                AgendaItem.meeting_id == "9999",
                AgendaItem.agenda_item_number == 2,
            )
        ).scalar_one_or_none()
        self.assertIsNotNone(item2)
        self.assertEqual(item2.c_number, "C-86-26-102-X-00")
        self.assertEqual(item2.c_number_base, "C-86-26-102-X")
        self.assertEqual(item2.c_number_revision, "00")

    def test_failed_preserves_previous(self):
        """When persist raises (e.g. invalid agenda_item_number), old rows remain intact."""
        items = self._make_items(count=3)
        replace_meeting_data_safe(self.session, "bos", "9999", self._make_meeting_dict(), items)
        self.session.commit()

        # Force failure with invalid numeric data
        bad_items = self._make_items(count=2)
        bad_items[0]["agenda_item_number"] = "not_a_number"  # Will fail int()
        with self.assertRaises(Exception):
            replace_meeting_data_safe(self.session, "bos", "9999", self._make_meeting_dict(), bad_items)
        self.session.rollback()

        # Previous items should still exist
        remaining = self.session.execute(
            select(func.count(AgendaItem.id)).where(AgendaItem.meeting_id == "9999")
        ).scalar()
        self.assertEqual(remaining, 3)

    def test_empty_items_and_docs(self):
        """Persisting a meeting with no items and no docs succeeds."""
        count = replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(), [],
            supporting_doc_dicts=[],
        )
        self.session.commit()
        self.assertEqual(count, 0)

    def test_backfill_normalization(self):
        """backfill_meeting_normalization sets display_name on existing rows."""
        init_db()
        d = self._make_meeting_dict(meeting_id="8888", date="2026-03-15", mtype="Formal")
        replace_meeting_data_safe(self.session, "bos", "8888", d, self._make_items(count=1))
        self.session.commit()

        # Manually null out display_name
        meeting_before = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "8888")
        ).scalar_one_or_none()
        meeting_before.display_name = None
        self.session.commit()

        backfill_meeting_normalization(self.session)
        self.session.commit()

        meeting_after = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "8888")
        ).scalar_one_or_none()
        self.assertIsNotNone(meeting_after.display_name)
        self.assertIn("Mar 15", meeting_after.display_name)
        self.assertIn("Formal", meeting_after.display_name)


@integration_test
class TestGetMeetingsByDateRange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Safety: skip if not connected to a test database
        url = str(self.session.get_bind().url)
        if 'test' not in url.lower() and 'memory' not in url.lower():
            self.skipTest(f"Not a test database: {url}")
        self.session.execute(Meeting.__table__.delete())
        for mid, date in [("A", "2026-01-01"), ("B", "2026-03-15"), ("C", "2026-06-01")]:
            m = Meeting(body="bos", meeting_id=mid, meeting_date=date, meeting_type="Formal", meeting_title=f"Meeting {mid}")
            self.session.add(m)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_date_range_inclusive(self):
        result = get_meetings_by_date_range(self.session, "bos", "2026-01-01", "2026-03-15")
        ids = [m.meeting_id for m in result]
        self.assertIn("A", ids)
        self.assertIn("B", ids)
        self.assertNotIn("C", ids)

    def test_no_matches(self):
        result = get_meetings_by_date_range(self.session, "bos", "2027-01-01", "2027-12-31")
        self.assertEqual(result, [])


@integration_test
class TestGetMeetingsByStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Safety: skip if not connected to a test database
        url = str(self.session.get_bind().url)
        if 'test' not in url.lower() and 'memory' not in url.lower():
            self.skipTest(f"Not a test database: {url}")
        self.session.execute(Meeting.__table__.delete())
        for mid, date, status in [
            ("1001", "2026-01-05", "complete"),
            ("1002", "2026-01-12", "failed"),
            ("1003", "2026-01-20", "partial"),
            ("1004", "2026-02-02", "pending"),
            ("1005", "2026-02-10", "complete"),
        ]:
            m = Meeting(
                body="bos", meeting_id=mid, meeting_date=date,
                meeting_type="Formal", meeting_title=f"Meeting {mid}",
                sync_status=status,
                source_url=f"https://example.com/meeting?id={mid}",
            )
            self.session.add(m)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_selects_failed_partial_pending(self):
        """--retry-failed: only failed, partial, pending."""
        result = get_meetings_by_status(self.session, "bos", ["failed", "partial", "pending"])
        ids = sorted(m.meeting_id for m in result)
        self.assertEqual(ids, ["1002", "1003", "1004"])

    def test_excludes_complete(self):
        """--retry-failed excludes complete."""
        result = get_meetings_by_status(self.session, "bos", ["failed", "partial", "pending"])
        ids = sorted(m.meeting_id for m in result)
        self.assertNotIn("1001", ids)
        self.assertNotIn("1005", ids)

    def test_force_includes_all(self):
        """--force includes all meetings regardless of status."""
        result = get_meetings_by_status(
            self.session, "bos", [], force=True,
            meeting_ids=["1001", "1002", "1005"],
        )
        ids = sorted(m.meeting_id for m in result)
        self.assertEqual(ids, ["1001", "1002", "1005"])

    def test_merge_search_with_db_search_takes_priority(self):
        """When merging search with DB results, search list should be returned first."""
        search_ids = ["1001", "1002"]
        db_ids = ["1001", "1002", "1003", "1004", "1005"]
        seen = set()
        merged = []
        for mid in search_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)
        for mid in db_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)
        self.assertEqual(sorted(merged), ["1001", "1002", "1003", "1004", "1005"])
        self.assertEqual(merged[:2], ["1001", "1002"])


@integration_test
class TestSyncStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Safety: skip if not connected to a test database
        url = str(self.session.get_bind().url)
        if 'test' not in url.lower() and 'memory' not in url.lower():
            self.skipTest(f"Not a test database: {url}")
        self.session.execute(Meeting.__table__.delete())
        m = Meeting(body="bos", meeting_id="test1", meeting_date="2026-01-01", meeting_type="Formal")
        self.session.add(m)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_status_complete_sets_fields(self):
        update_sync_status(self.session, "bos", "test1", "complete", item_count_actual=10)
        self.session.commit()
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "test1")
        ).scalar_one_or_none()
        self.assertEqual(meeting.sync_status, "complete")
        self.assertEqual(meeting.item_count_actual, 10)
        self.assertIsNone(meeting.last_error)
        self.assertEqual(meeting.retry_count, 0)

    def test_status_failed_increments_retry(self):
        update_sync_status(self.session, "bos", "test1", "failed", error="Something broke")
        self.session.commit()
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "test1")
        ).scalar_one_or_none()
        self.assertEqual(meeting.sync_status, "failed")
        self.assertEqual(meeting.retry_count, 1)
        self.assertEqual(meeting.last_error, "Something broke")


@integration_test
class TestVoteTablesCreated(unittest.TestCase):
    """Verify vote-related tables are created by init_db."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def test_agenda_item_votes_table_exists(self):
        from db import AgendaItemVote
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("agenda_item_votes", tables)
        session.close()

    def test_supervisor_votes_table_exists(self):
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("supervisor_votes", tables)
        session.close()

    def test_supervisors_table_exists(self):
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("supervisors", tables)
        session.close()

    def test_meeting_supervisors_table_exists(self):
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("meeting_supervisors", tables)
        session.close()


@integration_test
class TestRetryBackoff(unittest.TestCase):
    """Test the retry_with_backoff async helper."""

    def test_retry_succeeds_after_transient(self):
        """Retry succeeds on second attempt."""
        import asyncio
        from agenda_scraper import retry_with_backoff

        attempts = []

        async def sometimes_fails():
            attempts.append(1)
            if len(attempts) < 2:
                raise ConnectionError("transient")
            return "success"

        result = asyncio.run(
            retry_with_backoff(sometimes_fails, max_attempts=3, backoff_seconds=[0.05, 0.05])
        )
        self.assertEqual(result, "success")
        self.assertEqual(len(attempts), 2)

    def test_retry_exhausts_and_raises(self):
        """Retry exhausts all attempts and raises the last exception."""
        import asyncio
        from agenda_scraper import retry_with_backoff

        attempts = []

        async def always_fails():
            attempts.append(1)
            raise ValueError("permanent")

        with self.assertRaises(ValueError):
            asyncio.run(
                retry_with_backoff(always_fails, max_attempts=3, backoff_seconds=[0.05, 0.05])
            )
        self.assertEqual(len(attempts), 3)

    def test_retry_succeeds_first_time(self):
        """No retry needed when first attempt succeeds."""
        import asyncio
        from agenda_scraper import retry_with_backoff

        attempts = []

        async def always_succeeds():
            attempts.append(1)
            return "ok"

        result = asyncio.run(
            retry_with_backoff(always_succeeds, max_attempts=3, backoff_seconds=[0.05, 0.05])
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 1)


@integration_test
class TestPersistVotes(unittest.TestCase):
    """Test that persist_votes handles retry and stale-session scenarios."""

    @classmethod
    def setUpClass(cls):
        _reset_db_engine()
        init_db()

    def setUp(self):
        from db import persist_votes
        self.persist_votes = persist_votes
        self.s = get_session()
        # Ensure supervisor records exist
        from db import Supervisor
        existing = self.s.execute(
            select(Supervisor).where(Supervisor.normalized_name == "test one")
        ).scalar_one_or_none()
        if not existing:
            self.s.add(Supervisor(name="Test One", normalized_name="test one"))
            self.s.add(Supervisor(name="Test Two", normalized_name="test two"))
            self.s.commit()
        else:
            self.s.commit()

    def tearDown(self):
        self.s.close()

    def _make_vote(self, item_num=1):
        return [{
            "agenda_item_number": item_num,
            "c_number": f"C-01-25-{item_num:03d}-X-00",
            "motion_result": "approved",
            "vote_text": "Test vote",
            "supervisor_votes": [
                {"name": "Test One", "vote": "yes"},
                {"name": "Test Two", "vote": "yes"},
            ]
        }]

    def _make_supervisors(self):
        return [
            {"name": "Test One", "normalized_name": "test one", "district": "1"},
            {"name": "Test Two", "normalized_name": "test two", "district": "2"},
        ]

    def test_persist_votes_twice_same_meeting(self):
        """Calling persist_votes twice for the same meeting must succeed.

        First call succeeds internally but the outer commit fails.
        Second call must delete and re-insert without UNIQUE violations.
        """
        sups = self._make_supervisors()
        v = self._make_vote()

        self.persist_votes(self.s, "bos", "TEST001", sups, v)
        self.s.rollback()

        self.persist_votes(self.s, "bos", "TEST001", sups, v)
        self.s.commit()

        from db import MeetingSupervisor, AgendaItemVote
        ms = self.s.execute(
            select(MeetingSupervisor).where(
                MeetingSupervisor.body == "bos",
                MeetingSupervisor.meeting_id == "TEST001",
            )
        ).scalars().all()
        aiv = self.s.execute(
            select(AgendaItemVote).where(
                AgendaItemVote.body == "bos",
                AgendaItemVote.meeting_id == "TEST001",
            )
        ).scalars().all()
        self.assertEqual(len(ms), 2)
        self.assertEqual(len(aiv), 1)

    def test_persist_votes_consecutive_meetings(self):
        """Consecutive meetings must not leak session state."""
        sups = self._make_supervisors()
        for i in range(3):
            mid = f"TEST00{i+2}"
            self.persist_votes(self.s, "bos", mid, sups, self._make_vote(i + 1))
            self.s.commit()

        from db import MeetingSupervisor
        total = self.s.execute(select(MeetingSupervisor)).scalars().all()
        self.assertEqual(len(total), 6)

    def test_persist_votes_stale_session_after_failure(self):
        """When a commit fails after persist_votes, the next meeting's
        persist_votes must succeed despite stale objects in the session."""
        sups = self._make_supervisors()

        # First call: succeed, no commit (simulates outer exception)
        self.persist_votes(self.s, "bos", "TEST010", sups, self._make_vote())

        # Second meeting: must not get UNIQUE constraint failure
        self.persist_votes(self.s, "bos", "TEST011", sups, self._make_vote(1))
        self.s.commit()

        from db import MeetingSupervisor
        ms10 = self.s.execute(
            select(MeetingSupervisor).where(MeetingSupervisor.meeting_id == "TEST010")
        ).scalars().all()
        ms11 = self.s.execute(
            select(MeetingSupervisor).where(MeetingSupervisor.meeting_id == "TEST011")
        ).scalars().all()
        self.assertEqual(len(ms10), 2)
        self.assertEqual(len(ms11), 2)


def _reset_engine_cache():
    """Reset the db engine cache for test isolation."""
    try:
        import db as _db_mod
        _db_mod._engine = None
        _db_mod._SessionLocal = None
    except ImportError:
        pass

# Register cleanup for after all tests in this module run
import atexit
atexit.register(_reset_engine_cache)
if __name__ == "__main__":
    unittest.main()
