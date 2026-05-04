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


if __name__ == "__main__":
    unittest.main()
