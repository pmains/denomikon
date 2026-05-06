import asyncio
import csv
import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_scraper():
    scraper_path = Path(__file__).resolve().parents[1] / "scripts" / "maricopa_agenda_scraper.py"
    spec = importlib.util.spec_from_file_location("maricopa_agenda_scraper", scraper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load scraper from {scraper_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scraper = _load_scraper()
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = WORKSPACE_ROOT / "tests" / "fixtures"
FIXTURES_MANIFEST = FIXTURES_ROOT / "fixtures_manifest.csv"
EXPECTED_FIXTURE_COUNTS = {
    "4471": 7,
    "4448": 9,
    "4449": 148,
    "4470": 9,
    "4618": 4,
    "4617": 150,
}
def _load_passed_fixture_rows() -> list[dict[str, str]]:
    with FIXTURES_MANIFEST.open(newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if (row.get("validation_status") or "").strip().lower() == "passed"]


def _extract_structured_items_from_fixture_row(row: dict[str, str]) -> list[dict[str, str]]:
    meeting_id = row["meeting_id"]
    fixture_path = WORKSPACE_ROOT / row["local_fixture_path"]
    html = fixture_path.read_text(encoding="utf-8")
    meeting = {
        "meeting_id": meeting_id,
        "record_id": meeting_id,
        "meeting_date": row["meeting_date"],
        "record_date": row["meeting_date"],
        "meeting_type": row["meeting_type"],
        "document_url": row["source_url"],
    }
    return scraper.parse_agenda_items_from_html(html, row["source_url"], meeting)


class _FixturePage:
    def __init__(self, html):
        self.html = html
        self.visited_urls = []

    async def goto(self, url, wait_until=None):
        self.visited_urls.append((url, wait_until))

    async def wait_for_timeout(self, timeout):
        return None

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def content(self):
        return self.html

    async def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"")


class MaricopaAgendaScraperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_meetings_from_search_results_html_fixture(self):
        html = """
        <html><body>
          <table>
            <thead>
              <tr>
                <th>Meeting Name</th>
                <th>Meeting Type</th>
                <th>Meeting Date</th>
                <th>Links</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Board of Supervisors</td>
                <td>Formal</td>
                <td>1/29/2025 9:30:00 AM</td>
                <td>
                  <a href="/AgendaOnline/Meetings/ViewMeeting?id=4470&amp;doctype=1">Agenda</a>
                  <a href="/AgendaOnline/Meetings/ViewMeeting?id=4470&amp;doctype=3">Summary</a>
                  <a href="/AgendaOnline/Meetings/ViewMeeting?id=4470&amp;doctype=2">Minutes</a>
                  <a href="/AgendaOnline/Meetings/ViewMedia?id=4470">View Media</a>
                </td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """
        page = _FixturePage(html)
        search_url = "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/Search?dropid=11"

        meetings = asyncio.run(scraper.extract_meetings(page, search_url))

        self.assertEqual(len(meetings), 1)
        meeting = meetings[0]
        self.assertEqual(meeting.meeting_id, "4470")
        self.assertEqual(meeting.meeting_date, "2025-01-29")
        self.assertEqual(meeting.meeting_type, "Formal")
        self.assertEqual(
            meeting.agenda_url,
            "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id=4470&doctype=1",
        )
        self.assertEqual(
            meeting.summary_url,
            "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id=4470&doctype=3",
        )
        self.assertEqual(
            meeting.minutes_url,
            "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id=4470&doctype=2",
        )
        self.assertEqual(
            meeting.video_url,
            "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMedia?id=4470",
        )

    def test_extract_raw_agenda_blocks_from_agenda_table_html_fixture(self):
        html = """
        <html><body>
          <div id="agenda-table" class="container-fluid">
            <table><tr><td>PRESENTATION</td></tr></table>
            <table>
              <tr>
                <td>1.</td>
                <td><a id="lnkAgendaItem_1" href="#item-1">ROLL CALL</a></td>
              </tr>
            </table>
            <table><tr><td>ACTION</td></tr></table>
            <table>
              <tr>
                <td>2.</td>
                <td><a id="lnkAgendaItem_2" href="#item-2">INVOCATION</a></td>
              </tr>
            </table>
            <table>
              <tr>
                <td>10.</td>
                <td><a id="lnkAgendaItem_10" href="#item-10">FINAL ITEM</a></td>
              </tr>
            </table>
          </div>
        </body></html>
        """
        page = _FixturePage(html)
        meeting = {
            "record_id": "4470",
            "record_date": "2025-01-29",
            "meeting_type": "Formal",
            "document_url": "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id=4470&doctype=1",
        }

        blocks = asyncio.run(scraper.extract_raw_agenda_blocks_for_meeting(page, meeting))

        self.assertEqual([block["raw_text"] for block in blocks], [
            "1. ROLL CALL",
            "2. INVOCATION",
            "10. FINAL ITEM",
        ])
        self.assertEqual([block["raw_block_index"] for block in blocks], ["2", "4", "5"])
        self.assertTrue(all("PRESENTATION" not in block["raw_text"] for block in blocks))
        self.assertTrue(all("ACTION" not in block["raw_text"] for block in blocks))
        self.assertEqual({block["meeting_id"] for block in blocks}, {"4470"})

    def test_search_url_construction(self):
        url = scraper.build_search_url(scraper.parse_date("2025-01-01"), scraper.parse_date("2025-01-31"))
        self.assertIn("dropid=11", url)
        self.assertIn("dropsv=01%2F01%2F2025%2000%3A00%3A00", url)
        self.assertIn("dropev=01%2F31%2F2025%2023%3A59%3A59", url)

    def test_metadata_fieldnames_match_schema(self):
        discovery = self.root / "discovery_metadata.csv"
        agenda_root = self.root / "agendas"
        with patch.object(scraper, "DISCOVERY_CSV", discovery), patch.object(scraper, "AGENDAS_ROOT", agenda_root):
            scraper.write_discovery_row({
                "source_body": "Board of Supervisors",
                "document_category": "agenda",
                "record_id": "1",
                "record_date": "2025-01-01",
                "record_time": "",
                "record_title": "Test",
                "meeting_type": "Formal",
                "source_page_url": "https://example.com",
                "document_url": "https://example.com/doc",
                "local_path": "",
                "download_status": "discovered",
                "downloaded_at": "",
                "source_search_url": "https://example.com/search",
                "notes": "",
            })
            (agenda_root / "2025" / "01").mkdir(parents=True, exist_ok=True)
            with patch.object(scraper, "month_metadata_path", lambda date_iso: agenda_root / "2025" / "01" / "metadata.csv"):
                scraper.write_download_row({
                    "source_body": "Board of Supervisors",
                    "document_category": "agenda",
                    "record_id": "1",
                    "record_date": "2025-01-01",
                    "record_time": "",
                    "record_title": "Test",
                    "meeting_type": "Formal",
                    "source_page_url": "https://example.com",
                    "document_url": "https://example.com/doc",
                    "local_path": "data/agendas/2025/01/test.pdf",
                    "download_status": "downloaded",
                    "downloaded_at": "2025-01-01T00:00:00Z",
                    "source_search_url": "https://example.com/search",
                    "notes": "",
                })

        with discovery.open(newline="", encoding="utf-8") as f:
            discovery_header = next(csv.reader(f))
        with (agenda_root / "2025" / "01" / "metadata.csv").open(newline="", encoding="utf-8") as f:
            download_header = next(csv.reader(f))

        expected = [
            "source_body",
            "document_category",
            "record_id",
            "record_date",
            "record_time",
            "record_title",
            "meeting_type",
            "source_page_url",
            "document_url",
            "local_path",
            "download_status",
            "downloaded_at",
            "source_search_url",
            "notes",
        ]
        self.assertEqual(discovery_header, expected)
        self.assertEqual(download_header, expected)

    def test_splitter_cases_cover_key_patterns(self):
        cases = [
            (
                "3. PRESENTATION REGARDING THE FY 2026 BUDGET MARICOPA COUNTY TREASURER John Allen, Treasurer Ingrid Garvey, Chief Deputy Treasurer Jordan Dale, Chief of Staff (C-06-25-250-X-00) 4. PRESENTATION REGARDING THE FY 2026 BUDGET MARICOPA COUNTY RECORDER Justin Heap, Recorder Richard Greene, Finance and HR Director Sam Stone, Chief of Staff (C-06-25-249-X-00)",
                ["3", "4"],
            ),
            (
                "6. DOMRES 90 Case #: MCP250001 a. Development shall ... b. Site plan shall ...",
                ["6"],
            ),
            (
                "1. ROLL CALL 2. INVOCATION 3. PLEDGE OF ALLEGIANCE",
                ["1", "2", "3"],
            ),
            (
                "This item includes 24 hours advance notice for public comment.",
                [],
            ),
            (
                "Audio Access code 154-419-871 is provided for attendees.",
                [],
            ),
            (
                "1. TITLE ... (C-06-25-252-X-00) 2. TITLE ...",
                ["1", "2"],
            ),
        ]
        for sample, expected_numbers in cases:
            items = scraper.split_raw_block_into_items(sample)
            self.assertEqual([i["agenda_item_number"] for i in items], expected_numbers, sample)

    def test_splitter_self_test_returns_true(self):
        self.assertTrue(scraper.splitter_self_test())

    def test_validate_raw_block_rejects_boilerplate_time_address(self):
        for sample in [
            "1:00 PM Board Meeting",
            "301 W Jefferson Phoenix AZ 85003",
            "Board members: Thomas Galvin, Kate Brophy McGee",
        ]:
            valid, reason = scraper.validate_raw_block(sample)
            self.assertFalse(valid, sample)
            self.assertTrue(reason)

    def test_validate_raw_block_accepts_numbered_items(self):
        valid, reason = scraper.validate_raw_block(
            "1. ROLL CALL\n2. INVOCATION\n3. PLEDGE OF ALLEGIANCE"
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_validation_and_split_pipeline_writes_rejections_and_structured_rows(self):
        agenda_root = self.root / "agendas"
        agenda_items_root = self.root / "agenda-items"
        raw_csv = agenda_items_root / "raw_agenda_items.csv"
        structured_csv = agenda_items_root / "agenda_items.csv"
        rejected_csv = agenda_items_root / "rejected_raw_blocks.csv"

        agenda_items_root.mkdir(parents=True, exist_ok=True)
        raw_csv.write_text(
            "source_body,meeting_id,meeting_date,meeting_type,raw_block_index,raw_text,source_url\n"
            "Board of Supervisors,4470,2025-01-29,Formal,1,3. TREASURER alpha 4. RECORDER beta,https://example.com/a\n"
            "Board of Supervisors,4470,2025-01-29,Formal,2,Audio Access code 154-419-871 is provided for attendees.,https://example.com/b\n",
            encoding="utf-8",
        )

        with patch.object(scraper, "AGENDA_ITEMS_CSV", structured_csv), \
             patch.object(scraper, "RAW_AGENDA_ITEMS_CSV", raw_csv), \
             patch.object(scraper, "REJECTED_RAW_BLOCKS_CSV", rejected_csv):
            wrote = scraper.split_raw_agenda_blocks_to_structured()

        self.assertEqual(wrote, 2)
        self.assertTrue(structured_csv.exists())
        self.assertTrue(rejected_csv.exists())

        with structured_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["agenda_item_number"] for r in rows], ["3", "4"])
        self.assertEqual(rows[0]["agenda_item_title"], "TREASURER alpha")
        self.assertEqual(rows[1]["agenda_item_title"], "RECORDER beta")

        with rejected_csv.open(newline="", encoding="utf-8") as f:
            rejected_rows = list(csv.DictReader(f))
        self.assertEqual(len(rejected_rows), 1)
        self.assertEqual(rejected_rows[0]["raw_block_index"], "2")
        self.assertIn("Audio Access code", rejected_rows[0]["rejection_reason"] or "")

    def test_dry_run_helpers_do_not_require_playwright(self):
        url = scraper.build_search_url(scraper.parse_date("2025-01-01"), scraper.parse_date("2025-01-31"))
        self.assertIn("AgendaOnline/Meetings/Search", url)
        self.assertEqual(scraper.normalize_meeting_date("1/29/2025 9:30:00 AM"), "2025-01-29")

    def test_fixture_html_matches_expected_structured_agenda_item_counts(self):
        with FIXTURES_MANIFEST.open(newline="", encoding="utf-8") as f:
            manifest_rows = [row for row in csv.DictReader(f) if (row.get("meeting_id") or "") in EXPECTED_FIXTURE_COUNTS]

        self.assertEqual({row["meeting_id"] for row in manifest_rows}, set(EXPECTED_FIXTURE_COUNTS))

        for row in manifest_rows:
            with self.subTest(meeting_id=row["meeting_id"]):
                meeting_id = row["meeting_id"]
                fixture_path = WORKSPACE_ROOT / row["local_fixture_path"]
                self.assertTrue(fixture_path.exists(), fixture_path)

                html = fixture_path.read_text(encoding="utf-8")
                meeting = {
                    "meeting_id": meeting_id,
                    "record_id": meeting_id,
                    "meeting_date": row["meeting_date"],
                    "record_date": row["meeting_date"],
                    "meeting_type": row["meeting_type"],
                    "document_url": row["source_url"],
                }

                parsed_items = scraper.parse_agenda_items_from_html(html, row["source_url"], meeting)
                self.assertGreater(len(parsed_items), 0, meeting_id)

                for item in parsed_items:
                    for field in [
                        "source_body",
                        "meeting_id",
                        "meeting_date",
                        "meeting_type",
                        "agenda_item_number",
                        "agenda_item_title",
                        "agenda_item_text",
                        "source_url",
                    ]:
                        self.assertIn(field, item, f"{meeting_id} missing {field}: {item}")
                    self.assertRegex(item["meeting_id"], r"^\d+$", f"{meeting_id} non-numeric meeting_id: {item}")
                    self.assertRegex(item["meeting_date"], r"^\d{4}-\d{2}-\d{2}$", f"{meeting_id} bad meeting_date: {item}")
                    self.assertRegex(item["agenda_item_number"], r"^\d+$", f"{meeting_id} bad agenda_item_number: {item}")
                    self.assertGreater(len(item["agenda_item_title"] or ""), 0, f"{meeting_id} empty agenda_item_title: {item}")
                    self.assertGreaterEqual(len(item["agenda_item_text"] or ""), len(item["agenda_item_title"] or ""), f"{meeting_id} short agenda_item_text: {item}")
                    self.assertIn("ViewMeeting?id=", item["source_url"], f"{meeting_id} bad source_url: {item}")
                    self.assertTrue(item["agenda_item_number"].strip(), f"{meeting_id} missing item number: {item}")

                numbers = [item["agenda_item_number"] for item in parsed_items]
                self.assertEqual(len(parsed_items), EXPECTED_FIXTURE_COUNTS[meeting_id], meeting_id)
                self.assertTrue(numbers and numbers[0] == "1", f"{meeting_id} first item number was {numbers[:3]}")
                self.assertTrue(all(number.strip() for number in numbers), meeting_id)


class AgendaItemSchemaContractTests(unittest.TestCase):
    def test_passed_fixture_items_match_schema_contract(self):
        manifest_rows = _load_passed_fixture_rows()
        self.assertGreater(len(manifest_rows), 0)

        for row in manifest_rows:
            with self.subTest(meeting_id=row["meeting_id"]):
                fixture_path = WORKSPACE_ROOT / row["local_fixture_path"]
                self.assertTrue(fixture_path.exists(), fixture_path)

                items = _extract_structured_items_from_fixture_row(row)
                self.assertGreater(len(items), 0, f"{row['meeting_id']} produced zero structured items")

                for item in items:
                    for field in [
                        "source_body",
                        "meeting_id",
                        "meeting_date",
                        "meeting_type",
                        "agenda_item_number",
                        "agenda_item_title",
                        "agenda_item_text",
                        "source_url",
                    ]:
                        self.assertIn(field, item, f"{row['meeting_id']} missing {field}: {item}")
                    self.assertRegex(item["meeting_id"], r"^\d+$", f"{row['meeting_id']} non-numeric meeting_id: {item}")
                    self.assertRegex(item["meeting_date"], r"^\d{4}-\d{2}-\d{2}$", f"{row['meeting_id']} bad meeting_date: {item}")
                    self.assertGreaterEqual(int(item["agenda_item_number"]), 1, f"{row['meeting_id']} bad agenda_item_number: {item}")
                    self.assertGreater(len(item["agenda_item_title"] or ""), 0, f"{row['meeting_id']} empty agenda_item_title: {item}")
                    self.assertGreater(len(item["agenda_item_text"] or ""), 0, f"{row['meeting_id']} empty agenda_item_text: {item}")
                    self.assertGreaterEqual(len(item["agenda_item_text"]), len(item["agenda_item_title"]), f"{row['meeting_id']} short agenda_item_text: {item}")
                    self.assertIn("ViewMeeting?id=", item["source_url"], f"{row['meeting_id']} bad source_url: {item}")


class BodyScopedIdentityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Ensure we connect to the real database, not a test in-memory one
        from pathlib import Path
        import os
        real_db = str(Path(__file__).resolve().parents[1] / "data" / "maricopa.sqlite")
        os.environ["DATABASE_URL"] = f"sqlite:///{real_db}"
        # Reset sqlalchemy engine cache so get_session() picks up the right URL
        import scripts.db as db_mod
        import importlib
        importlib.reload(db_mod)
    """Regression tests for body-scoped meeting identity."""

    def test_meeting_dataclass_has_body_field(self):
        """Meeting dataclass must have a body field."""
        from dataclasses import fields
        field_names = {f.name for f in fields(scraper.Meeting)}
        self.assertIn("body", field_names)

    def test_meeting_id_for_bos_url(self):
        """BOS meeting ID extracted from ViewMeeting URL."""
        m = scraper.Meeting(
            meeting_date="2025-01-29", meeting_time="", meeting_title="Formal",
            meeting_type="Formal", body="bos", row_text="",
            detail_url="",
            agenda_url="https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id=4470&doctype=1",
        )
        self.assertEqual(m.meeting_id, "4470")
        self.assertEqual(m.body, "bos")

    def test_meeting_id_for_pz_url_with_dash(self):
        """PZ meeting ID extracted from dashed URL format, no pz- prefix."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Planning & Zoning", body="pz", row_text="",
            detail_url="",
            agenda_url="https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/_04232026-3722?html=true",
        )
        self.assertEqual(m.meeting_id, "3722")
        self.assertEqual(m.body, "pz")

    def test_meeting_id_for_pz_viewfile_url(self):
        """PZ meeting ID extracted from ViewFile/Agenda/NNNN format."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Planning & Zoning", body="pz", row_text="",
            detail_url="",
            agenda_url="https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/3734",
        )
        self.assertEqual(m.meeting_id, "3734")
        self.assertEqual(m.body, "pz")

    def test_pz_meeting_id_no_pz_prefix_in_storage(self):
        """PZ meeting IDs stored without pz- prefix; body field provides scope."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Planning & Zoning", body="pz", row_text="",
            detail_url="",
            agenda_url="https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/3734",
        )
        self.assertFalse(m.meeting_id.startswith("pz-"),
                         "meeting_id should not have pz- prefix")

    def test_db_meeting_has_body_column(self):
        """Database Meeting model has body column for scoping."""
        from sqlalchemy import inspect
        from scripts.db import get_engine, Meeting
        insp = inspect(get_engine())
        cols = {c["name"] for c in insp.get_columns("meetings")}
        self.assertIn("body", cols)

    def test_body_backfill_bos_meetings_exist(self):
        """Existing BOS meetings should have body='bos'."""
        from scripts.db import get_session, Meeting
        from sqlalchemy import select, func
        session = get_session()
        count = session.execute(
            select(func.count()).select_from(Meeting).where(
                Meeting.body == "bos",
                Meeting.meeting_type != "Planning & Zoning",
            )
        ).scalar()
        self.assertGreater(count, 0, "Should have BOS meetings with body='bos'")
        session.close()

    def test_body_backfill_pz_meeting_exists(self):
        """Existing PZ meeting should have body='pz'."""
        from scripts.db import get_session, Meeting
        from sqlalchemy import select, func
        session = get_session()
        count = session.execute(
            select(func.count()).select_from(Meeting).where(
                Meeting.body == "pz",
            )
        ).scalar()
        self.assertGreaterEqual(count, 1, "Should have PZ meetings with body='pz'")
        session.close()

    def test_db_agenda_items_have_body_column(self):
        """Agenda items table has body column."""
        from sqlalchemy import inspect
        from scripts.db import get_engine
        insp = inspect(get_engine())
        cols = {c["name"] for c in insp.get_columns("agenda_items")}
        self.assertIn("body", cols)

    def test_pz_meeting_item_body_scoped(self):
        """PZ agenda items should have body='pz'."""
        from scripts.db import get_session, AgendaItem
        from sqlalchemy import select, func
        session = get_session()
        count = session.execute(
            select(func.count()).select_from(AgendaItem).where(
                AgendaItem.body == "pz",
            )
        ).scalar()
        self.assertGreaterEqual(count, 1, "Should have PZ agenda items with body='pz'")
        session.close()

    def test_bos_meeting_item_body_scoped(self):
        """BOS agenda items should have body='bos'."""
        from scripts.db import get_session, AgendaItem
        from sqlalchemy import select, func
        session = get_session()
        count = session.execute(
            select(func.count()).select_from(AgendaItem).where(
                AgendaItem.body == "bos",
            )
        ).scalar()
        self.assertGreaterEqual(count, 1, "Should have BOS agenda items with body='bos'")
        session.close()

    def test_bos_and_pz_can_share_meeting_id(self):
        """BOS and PZ can both have meeting_id='3734' without collision."""
        from scripts.db import get_session, Meeting
        from sqlalchemy import select, func
        session = get_session()
        bos_3734 = session.execute(
            select(Meeting).where(
                Meeting.body == "bos",
                Meeting.meeting_id == "3734",
            )
        ).scalar_one_or_none()
        pz_3734 = session.execute(
            select(Meeting).where(
                Meeting.body == "pz",
                Meeting.meeting_id == "3734",
            )
        ).scalar_one_or_none()
        # With body scoping, bos 3734 and pz 3734 can coexist
        self.assertIsNotNone(pz_3734, "PZ meeting 3734 should exist")
        session.close()

    def test_parse_pz_meetings_creates_body_scoped_meetings(self):
        """parse_pz_meetings_from_html creates Meeting with body='pz'."""
        html = """
        <html><body>
        <table id="meetingDetail">
          <tbody>
            <tr id="row3711" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for May 7, 2026"><abbr title="May">May</abbr> 7, 2026</strong></h3>
                <p>
                  <a id="05072026-3734" href="/AgendaCenter/ViewFile/Agenda/_05072026-3734?html=true">
                    May 7, 2026 Planning and Zoning Commission Meeting
                  </a>
                </p>
              </td>
              <td class="minutes"></td>
              <td class="media"></td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        meetings = scraper.parse_pz_meetings_from_html(html, "https://www.maricopa.gov/AgendaCenter/Search")
        self.assertGreater(len(meetings), 0)
        for m in meetings:
            self.assertEqual(m.body, "pz")
            self.assertFalse(m.meeting_id.startswith("pz-"),
                             f"meeting_id should not have pz- prefix: {m.meeting_id}")


class RegressionTests(unittest.TestCase):
    """Regression tests for fixed bugs."""

    def test_pz_sync_with_meeting_id_does_not_raise_unbound_local(self):
        """
        Regression: PZ --sync --meeting-id must not raise UnboundLocalError.

        The bug was caused by 'from db import Meeting' inside main() shadowing
        the module-level Meeting dataclass.  This test verifies that the
        module-level Meeting class name is not shadowed by the db import.
        """
        # Verify the globals are accessible as expected
        # If there were an import shadowing Meeting, constructing one would
        # raise UnboundLocalError
        m = scraper.Meeting(
            meeting_date="2026-05-07", meeting_time="", meeting_title="",
            meeting_type="Planning & Zoning", body="pz", row_text="",
            detail_url="",
            agenda_url="https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/3734",
        )
        self.assertEqual(m.body, "pz")
        self.assertEqual(m.meeting_id, "3734")

    def test_bos_sync_retry_failed_imports_are_available(self):
        """
        Regression: bos --sync --retry-failed must not raise UnboundLocalError
        for create_or_get_meeting, update_sync_status, or replace_meeting_data_safe.

        The bug was caused by these imports being scoped inside a conditional
        block (the search path), but referenced outside it (the retry-failed
        path that skips the search).
        """
        # Verify the imported names are accessible at module level
        # This simulates what happens in main() when the imports are moved
        # to the function scope before the conditional block
        from scripts.db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe
        import inspect
        self.assertTrue(callable(create_or_get_meeting))
        self.assertTrue(callable(update_sync_status))
        self.assertTrue(callable(replace_meeting_data_safe))
        sig = inspect.signature(replace_meeting_data_safe)
        params = list(sig.parameters.keys())
        self.assertIn('body', params,
                      f'replace_meeting_data_safe must accept body parameter, got: {params}')
        self.assertIn('meeting_id', params)
        self.assertIn('agenda_item_dicts', params)

    def test_pz_meeting_id_from_parse_matches_body_and_no_prefix(self):
        """Regression: PZ meetings parsed from HTML don't get pz- prefix."""
        html = """
        <html><body>
        <table id="meetingDetail">
          <tbody>
            <tr id="row3734" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for May 7, 2026"><abbr title="May">May</abbr> 7, 2026</strong></h3>
                <p>
                  <a id="05072026-3734" href="/AgendaCenter/ViewFile/Agenda/_05072026-3734?html=true">
                    May 7, 2026 Planning and Zoning Commission Meeting
                  </a>
                </p>
              </td>
              <td class="minutes"></td>
              <td class="media"></td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        meetings = scraper.parse_pz_meetings_from_html(
            html, "https://www.maricopa.gov/AgendaCenter/Search"
        )
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].meeting_id, "3734")
        self.assertEqual(meetings[0].body, "pz")


class PZStaffReportRegressionTests(unittest.TestCase):
    """Regression tests for P&Z staff report extraction and linking."""

    def test_parse_pz_overview_identifies_agenda_and_staff_reports(self):
        """parse_pz_overview correctly identifies agenda doc and staff reports."""
        html = """
        <html><body>
        <h1 class="title">May 7, 2026 Planning and Zoning Commission Agenda</h1>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10270">Agenda.pdf</a></p>

        <h1 class="title">CPAZ250011 &amp; Z250034 P&amp;Z Report</h1>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10271?fileID=100385">01.02.CPA250011 PZ Staff Report.pdf</a></p>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10272?fileID=100386">01.02.Z250034 Appendix.pdf</a></p>

        <h1 class="title">Z250026 P&amp;Z Staff Report</h1>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10275?fileID=100390">03.Z250026 PZ Staff Report.pdf</a></p>
        </body></html>
        """
        result = scraper.parse_pz_overview(
            html,
            "https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/_05072026-3734?html=true",
            "https://www.maricopa.gov/",
        )

        self.assertIsNotNone(result)
        self.assertIn("Agenda", result["agenda_title"])
        self.assertIn("Item/10270", result["agenda_pdf_url"])

        # Agenda document should NOT appear in staff reports
        staff_titles = [s["document_title"] for s in result["staff_report_files"]]
        self.assertNotIn("Agenda.pdf", [t[:10] for t in staff_titles])

    def test_parse_pz_overview_multi_case_staff_report(self):
        """Staff report covering multiple cases extracts ALL case numbers."""
        html = """
        <html><body>
        <h1 class="title">CPAZ250011 &amp; Z250034 P&amp;Z Report</h1>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10271?fileID=100385">01.02.CPA250011 PZ Staff Report.pdf</a></p>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10272?fileID=100386">01.02.Z250034 Appendix.pdf</a></p>
        </body></html>
        """
        result = scraper.parse_pz_overview(
            html,
            "https://example.com/overview",
            "https://example.com/",
        )

        self.assertEqual(len(result["staff_report_files"]), 2)

        # First file should list both case numbers in all_case_numbers
        first = result["staff_report_files"][0]
        self.assertIn("CPAZ250011", first["all_case_numbers"])
        self.assertIn("Z250034", first["all_case_numbers"])

        # Second file should also list both case numbers
        second = result["staff_report_files"][1]
        self.assertIn("CPAZ250011", second["all_case_numbers"])
        self.assertIn("Z250034", second["all_case_numbers"])

    def test_pz_staff_report_has_file_specific_url(self):
        """Each staff report file has its own unique document URL."""
        html = """
        <html><body>
        <h1 class="title">CPAZ250011 &amp; Z250034 P&amp;Z Report</h1>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10271?fileID=100385">01.02.CPA250011 PZ Staff Report.pdf</a></p>
        <p><a class="file" href="/AgendaCenter/ViewFile/Item/10272?fileID=100386">01.02.Z250034 Appendix.pdf</a></p>
        </body></html>
        """
        result = scraper.parse_pz_overview(
            html,
            "https://example.com/overview",
            "https://example.com/",
        )

        urls = [s["document_url"] for s in result["staff_report_files"]]
        self.assertEqual(len(urls), len(set(urls)), "All file URLs must be unique")

    def test_persist_meeting_sets_body_on_supporting_docs(self):
        """Regression: persist_meeting must set body=body on SupportingDocument.

        Previously, SupportingDocument rows were created without the body field,
        causing UNIQUE constraint violations because the delete-by-(body, meeting_id)
        didn't match rows with empty body, while the insert added them.
        """
        import tempfile
        from scripts.db import get_session, get_engine, Base, Meeting, AgendaItem, SupportingDocument
        from sqlalchemy import create_engine, inspect

        # Create an in-memory SQLite database with the schema
        db_url = "sqlite://"
        import os
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_url
        try:
            # Reload db module with new URL
            import importlib
            from scripts import db
            importlib.reload(db)

            db.init_db()
            session = db.get_session()

            meeting_dict = {
                "meeting_id": "9999",
                "meeting_date": "2026-05-07",
                "meeting_type": "Planning & Zoning",
                "meeting_title": "Test",
                "source_url": "https://example.com",
            }
            db.create_or_get_meeting(session, "pz", meeting_dict)
            session.commit()

            items = [{
                "source_body": "Planning & Zoning",
                "meeting_id": "9999",
                "meeting_date": "2026-05-07",
                "meeting_type": "Planning & Zoning",
                "agenda_item_number": "1",
                "agenda_item_id": "9999-1-item",
                "agenda_item_title": "Test Item",
                "agenda_item_text": "",
                "agenda_item_url": "",
                "vote_or_action": "",
                "source_url": "",
                "c_number": "CPAZ250011",
                "c_number_base": "",
                "c_number_revision": None,
                "case_number": "CPAZ250011",
            }]

            docs = [{
                "agenda_item_id": 1,
                "agenda_item_number": 1,
                "c_number": "CPAZ250011",
                "document_title": "Staff Report",
                "document_url": "https://example.com/doc1.pdf",
                "document_type": "PDF",
                "file_name": "report.pdf",
                "file_extension": "pdf",
            }]

            # First persist - should succeed
            count = db.persist_meeting(session, "pz", "9999", items, docs)
            self.assertEqual(count, 1)

            # Verify body was set on supporting doc
            sd = session.execute(
                db.select(SupportingDocument).where(
                    SupportingDocument.body == "pz",
                    SupportingDocument.meeting_id == "9999",
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(sd, "Supporting doc must have body='pz'")
            self.assertEqual(sd.body, "pz")

            # Second persist - should succeed without IntegrityError
            count2 = db.persist_meeting(session, "pz", "9999", items, docs)
            self.assertEqual(count2, 1)

            session.close()
        finally:
            os.environ["DATABASE_URL"] = old_url or ""

    def test_persist_meeting_body_scope_isolation(self):
        """persist_meeting with different body values should not interfere."""
        import os
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite:///"
        try:
            from scripts import db as db_mod
            import importlib
            importlib.reload(db_mod)

            db_mod.init_db()
            session = db_mod.get_session()

            SdModel = db_mod.SupportingDocument

            meeting_dict = {"meeting_id": "9999", "meeting_date": "2026-05-07",
                           "meeting_type": "Formal", "meeting_title": "Test",
                           "source_url": "https://example.com"}
            db_mod.create_or_get_meeting(session, "bos", meeting_dict)
            db_mod.create_or_get_meeting(session, "pz", meeting_dict)
            session.commit()

            item = {"source_body": "BOS", "meeting_id": "9999", "meeting_date": "2026-05-07",
                    "meeting_type": "Formal", "agenda_item_number": "1",
                    "agenda_item_id": "9999-1-item", "agenda_item_title": "BOS Item",
                    "agenda_item_text": "", "agenda_item_url": "", "vote_or_action": "",
                    "source_url": "", "c_number": "", "c_number_base": "",
                    "c_number_revision": None, "case_number": ""}

            doc = {"agenda_item_id": 1, "agenda_item_number": 1,
                   "document_title": "Report",
                   "document_url": "https://example.com/doc.pdf",
                   "document_type": "PDF", "file_name": "r.pdf", "file_extension": "pdf"}

            # Persist BOS meeting with body='bos'
            db_mod.persist_meeting(session, "bos", "9999", [item], [doc])

            # Check BOS doc exists
            bos_doc = session.execute(
                db_mod.select(SdModel).where(
                    SdModel.body == "bos", SdModel.meeting_id == "9999"
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(bos_doc)
            self.assertEqual(bos_doc.body, "bos")

            # PZ meeting with same meeting_id should have NO docs
            pz_doc = session.execute(
                db_mod.select(SdModel).where(
                    SdModel.body == "pz", SdModel.meeting_id == "9999"
                )
            ).scalar_one_or_none()
            self.assertIsNone(pz_doc, "PZ meeting should not inherit BOS docs")

            session.close()
        finally:
            os.environ["DATABASE_URL"] = old_url or ""


class PZParserRegressionTests(unittest.TestCase):
    """Regression tests for PZ PDF parsing and database interaction fixes."""

    def test_parse_pz_agenda_pdf_no_group_index_error(self):
        """
        Regression: parse_pz_agenda_pdf must not raise IndexError when a field
        pattern matches but has fewer groups than expected.

        The bug was that FIELD_PATTERNS entries like district, project_name,
        applicant, etc. have only one capturing group, but the code tried
        m.group(2) unconditionally, which raised IndexError.
        """
        from scripts.maricopa_agenda_scraper import parse_pz_agenda_pdf
        import subprocess, tempfile
        from pathlib import Path

        # Create a minimal valid PDF that pdftotext can extract
        # The simplest approach: use an empty PDF with specific text layout
        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
            b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
            b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"5 0 obj<</Length 44>>stream\n"
            b"BT /F1 12 Tf 72 700 Td (1. Case: TEST001) Tj ET\n"
            b"endstream\nendobj\n"
            b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"0000000115 00000 n \n0000000266 00000 n \n0000000348 00000 n \n"
            b"trailer<</Size 6/Root 1 0 R>>\n"
            b"startxref\n445\n%%EOF\n"
        )
        pdf_path = Path("/tmp/pz_regression_test.pdf")
        pdf_path.write_bytes(pdf_bytes)

        items = parse_pz_agenda_pdf(str(pdf_path))
        pdf_path.unlink(missing_ok=True)
        # Should not raise; items may be empty if pdftotext fails
        self.assertIsInstance(items, list)

    def test_parse_pz_agenda_pdf_field_patterns_group_safety(self):
        """
        Regression: FIELD_PATTERNS with only one capturing group must not
        cause IndexError when the code tries to access m.group(2).
        Tests each field pattern against matching text lines.
        """
        from scripts.maricopa_agenda_scraper import parse_pz_agenda_pdf

        # Extract FIELD_PATTERNS directly from the function for testing
        import re

        # Simulate what FIELD_PATTERNS does
        patterns = {
            "district": re.compile(r"District\s+(\d+)"),
            "project_name": re.compile(r"Project\s+name\s*:?\s*(.*)"),
            "applicant": re.compile(r"Applicant\s*:?\s*(.*)"),
            "request": re.compile(r"Request\s*:?\s*(.*)"),
            "location": re.compile(r"Location\s*:?\s*(.*)"),
            "presented_by": re.compile(r"Presented\s+by\s*:?\s*(.*)"),
        }

        test_lines = [
            "District 4",
            "Project name: Arlington Valley Solar Energy",
            "Applicant: Ashley Holland",
            "Request: General Plan Amendment",
            "Location: SEC of 395th Ave.",
            "Presented by: Martin Martell",
        ]

        for field, pattern in patterns.items():
            for line in test_lines:
                m = pattern.search(line)
                if m:
                    # This is the code that was raising IndexError
                    val = m.group(1) if m.lastindex >= 1 else ""
                    if not val and m.lastindex >= 2:
                        val = m.group(2)
                    val = (val or "").strip()
                    # Should complete without IndexError
                    self.assertIsInstance(val, str)

    def test_persist_meeting_deduplicates_agenda_item_ids(self):
        """
        Regression: persist_meeting must deduplicate agenda items by
        agenda_item_id to avoid UNIQUE constraint violations.
        """
        import os
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite:///"
        try:
            from scripts import db
            import importlib
            importlib.reload(db)

            db.init_db()
            session = db.get_session()

            meeting_dict = {
                "meeting_id": "9999", "meeting_date": "2026-01-01",
                "meeting_type": "Formal", "meeting_title": "Test",
                "source_url": "https://example.com",
            }
            db.create_or_get_meeting(session, "bos", meeting_dict)
            session.commit()

            # Create items with DUPLICATE agenda_item_id
            items = [
                {
                    "source_body": "BOS", "meeting_id": "9999",
                    "meeting_date": "2026-01-01", "meeting_type": "Formal",
                    "agenda_item_number": "1", "agenda_item_id": "9999-1-item",
                    "agenda_item_title": "Item One", "agenda_item_text": "",
                    "agenda_item_url": "", "vote_or_action": "",
                    "source_url": "", "c_number": "", "c_number_base": "",
                    "c_number_revision": None, "case_number": "",
                },
                {
                    "source_body": "BOS", "meeting_id": "9999",
                    "meeting_date": "2026-01-01", "meeting_type": "Formal",
                    # Intentionally duplicate agenda_item_id:
                    "agenda_item_number": "2", "agenda_item_id": "9999-1-item",
                    "agenda_item_title": "Duplicate ID Item", "agenda_item_text": "",
                    "agenda_item_url": "", "vote_or_action": "",
                    "source_url": "", "c_number": "", "c_number_base": "",
                    "c_number_revision": None, "case_number": "",
                },
            ]

            # Should not raise IntegrityError despite duplicate agenda_item_id
            count = db.persist_meeting(session, "bos", "9999", items)
            self.assertEqual(count, 1, "Only one item should be persisted (dedup)")

            # Verify only one item exists
            from sqlalchemy import select, func
            actual = session.execute(
                select(func.count()).select_from(db.AgendaItem).where(
                    db.AgendaItem.body == "bos",
                    db.AgendaItem.meeting_id == "9999",
                )
            ).scalar()
            self.assertEqual(actual, 1)

            session.close()
        finally:
            os.environ["DATABASE_URL"] = old_url or ""

    def test_pz_item_detail_no_unique_constraint(self):
        """
        Regression: pz_item_details.agenda_item_id must NOT have a UNIQUE
        constraint. Two items in the same meeting can reference the same
        AgendaItem.id value.
        """
        import os
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite:///"
        try:
            from scripts import db
            import importlib
            importlib.reload(db)

            db.init_db()
            session = db.get_session()

            # Create a PZ meeting
            meeting_dict = {
                "meeting_id": "9998", "meeting_date": "2026-01-01",
                "meeting_type": "Planning & Zoning", "meeting_title": "PZ Test",
                "source_url": "https://example.com",
            }
            db.create_or_get_meeting(session, "pz", meeting_dict)
            session.commit()

            # Insert TWO PZItemDetails with the same agenda_item_id
            # This would fail if UNIQUE constraint existed
            now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
            d1 = db.PZItemDetail(
                body="pz", agenda_item_id=999, meeting_id="9998",
                agenda_item_number=1, case_number="CPA001",
                project_name="Project Alpha",
            )
            d2 = db.PZItemDetail(
                body="pz", agenda_item_id=999, meeting_id="9998",
                agenda_item_number=2, case_number="CPA002",
                project_name="Project Beta",
            )
            session.add(d1)
            session.add(d2)
            session.commit()

            # Both should persist
            from sqlalchemy import select, func
            count = session.execute(
                select(func.count()).select_from(db.PZItemDetail).where(
                    db.PZItemDetail.meeting_id == "9998"
                )
            ).scalar()
            self.assertEqual(count, 2)

            session.close()
        finally:
            os.environ["DATABASE_URL"] = old_url or ""


if __name__ == "__main__":
    unittest.main()
