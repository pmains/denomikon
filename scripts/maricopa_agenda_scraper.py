#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import html
import io
import random
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

SOURCE_PAGE = "https://www.maricopa.gov/324/Board-of-Supervisors-Meeting-Information"
SEARCH_BASE = "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/Search"
REQUIRED_BODY = re.compile(r"Board of Supervisors", re.I)
REQUIRED_TYPES = re.compile(r"(Formal|Informal)", re.I)

ROOT = Path.cwd()
AGENDAS_ROOT = ROOT / "data" / "agendas"
SUPPORT_ROOT = ROOT / "data" / "supporting-materials"
AGENDA_ITEMS_ROOT = ROOT / "data" / "agenda-items"
AGENDA_ITEMS_CSV = AGENDA_ITEMS_ROOT / "agenda_items.csv"
RAW_AGENDA_ITEMS_CSV = AGENDA_ITEMS_ROOT / "raw_agenda_items.csv"
REJECTED_RAW_BLOCKS_CSV = AGENDA_ITEMS_ROOT / "rejected_raw_blocks.csv"
DISCOVERY_CSV = ROOT / "data" / "discovery_metadata.csv"
LOGS_ROOT = ROOT / "logs"


def get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for browser-backed scraping commands. "
            "Install the project scraping dependencies before running those commands."
        ) from exc
    return async_playwright


async def retry_with_backoff(
    coro_factory,
    max_attempts: int = 3,
    backoff_seconds: list[int] | None = None,
    label: str = "",
):
    """Execute an async call with retry and exponential backoff.

    coro_factory: a no-arg callable that returns a coroutine
    max_attempts: max retries (default 3)
    backoff_seconds: delays between attempts (default [1, 3, 10])
    """
    if backoff_seconds is None:
        backoff_seconds = [1, 3, 10]
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts:
                delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                jitter = random.uniform(0, 0.5 * delay)
                total_delay = delay + jitter
                label_text = f" [{label}]" if label else ""
                print(f"  Retry {attempt}/{max_attempts}{label_text}: {e} (waiting {total_delay:.1f}s)")
                await asyncio.sleep(total_delay)
    raise last_exc


@dataclass
class Meeting:
    meeting_date: str
    meeting_time: str
    meeting_title: str
    meeting_type: str
    body: str
    row_text: str
    detail_url: str
    agenda_url: str
    summary_url: str = ""
    minutes_url: str = ""
    video_url: str = ""

    @property
    def meeting_id(self) -> str:
        for url in (self.detail_url, self.agenda_url):
            # BOS format: /ViewMeeting?id=1234&doctype=1
            m = re.search(r"[?&]ID=(\d+)", url or "", re.I)
            if m:
                return m.group(1)
            # PZ format: /Agenda/_04232026-3722?html=true  or  /Agenda/3734
            m = re.search(r"/Agenda/[^/]*-(\d{3,})", url or "")
            if m:
                return m.group(1)
            m = re.search(r"/Agenda/(\d{3,})", url or "")
            if m:
                return m.group(1)
        return "meeting"


class _HtmlNode:
    def __init__(self, tag: str = "", attrs: Optional[dict[str, str]] = None, parent: Optional['_HtmlNode'] = None) -> None:
        self.tag = tag.lower()
        self.attrs = attrs or {}
        self.parent = parent
        self.children: list[_HtmlNode | str] = []


class _TreeBuilder(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        parent_node = self._stack[-1] if self._stack else None
        node = _HtmlNode(tag, {k.lower(): v or "" for k, v in attrs}, parent=parent_node)
        if parent_node:
            parent_node.children.append(node)
        if tag.lower() not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == tag:
                del self._stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _parse_html(html: str) -> _HtmlNode:
    parser = _TreeBuilder()
    parser.feed(html or "")
    parser.close()
    return parser.root


def _node_text(node: _HtmlNode | str) -> str:
    if isinstance(node, str):
        return node
    return " ".join(_node_text(child) for child in node.children)


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _closest_parent(node: _HtmlNode, tag: str) -> Optional[_HtmlNode]:
    """Walk up the parent chain to find the nearest ancestor with the given tag."""
    current = node.parent
    while current:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _find_all(node: _HtmlNode, tag: Optional[str] = None) -> list[_HtmlNode]:
    found: list[_HtmlNode] = []
    wanted = tag.lower() if tag else None
    for child in node.children:
        if not isinstance(child, _HtmlNode):
            continue
        if wanted is None or child.tag == wanted:
            found.append(child)
        found.extend(_find_all(child, wanted))
    return found


def _has_class(node: _HtmlNode, class_name: str) -> bool:
    return class_name in (node.attrs.get("class") or "").split()


def _search_results_table_present(html: str) -> bool:
    root = _parse_html(html)
    for table in _find_all(root, "table"):
        table_text = _clean_html_text(_node_text(table)).lower()
        if all(token in table_text for token in ["meeting name", "meeting type", "meeting date", "links"]):
            return True
    return False


def parse_search_results_html(html: str, base_url: str) -> list[Meeting]:
    root = _parse_html(html)
    tables = _find_all(root, "table")
    table = None
    for candidate in tables:
        table_text = _clean_html_text(_node_text(candidate)).lower()
        if all(token in table_text for token in ["meeting name", "meeting type", "meeting date", "links"]):
            table = candidate
            break

    row_candidates = _find_all(table, "tr") if table else _find_all(root, "tr")
    meetings: list[Meeting] = []

    for row in row_candidates:
        row_text = _clean_html_text(_node_text(row))
        meeting_date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}:\d{2}\s?[AP]M)?)", row_text, re.I)
        if not meeting_date_match:
            continue

        anchors = []
        seen: set[str] = set()
        for anchor in _find_all(row, "a"):
            href = anchor.attrs.get("href", "").strip()
            text = _clean_html_text(_node_text(anchor))
            abs_url = urllib.parse.urljoin(base_url, href) if href else ""
            key = abs_url or text
            if not key or key in seen:
                continue
            seen.add(key)
            anchors.append({"text": text, "href": abs_url})

        def by_text(*wanted: str) -> Optional[dict[str, str]]:
            wanted_lower = {w.lower() for w in wanted}
            return next((a for a in anchors if (a["text"] or "").lower() in wanted_lower), None)

        def by_doctype(value: int) -> Optional[dict[str, str]]:
            for anchor in anchors:
                try:
                    parsed = urllib.parse.urlparse(anchor["href"])
                    params = urllib.parse.parse_qs(parsed.query)
                except Exception:
                    continue
                if (params.get("doctype") or [""])[0] == str(value):
                    return anchor
            return None

        agenda = by_text("agenda") or by_doctype(1)
        summary = by_text("summary") or by_doctype(3)
        minutes = by_text("minutes") or by_doctype(2)
        video = by_text("view media", "media", "video")
        if not agenda:
            continue

        cells = [
            _clean_html_text(_node_text(cell))
            for cell in _find_all(row, None)
            if cell.tag in {"th", "td"} and _clean_html_text(_node_text(cell))
        ]
        meeting_title = cells[0] if cells else row_text.split("Agenda", 1)[0].strip()
        meeting_type = cells[1] if len(cells) > 1 else meeting_title
        meeting_date = normalize_meeting_date(meeting_date_match.group(1))
        if not meeting_date:
            continue

        meetings.append(
            Meeting(
                meeting_date=meeting_date,
                meeting_time="",
                meeting_title=meeting_title,
                meeting_type=meeting_type,
                body="bos",
                row_text=row_text,
                detail_url="",
                agenda_url=agenda["href"],
                summary_url=summary["href"] if summary else "",
                minutes_url=minutes["href"] if minutes else "",
                video_url=video["href"] if video else "",
            )
        )

    return meetings


def parse_raw_agenda_blocks_html(html: str, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []

    root = _parse_html(html)
    container = next(
        (
            node
            for node in _find_all(root, "div")
            if node.attrs.get("id") == "agenda-table" and _has_class(node, "container-fluid")
        ),
        None,
    )
    if container is None:
        return []

    normalized_meeting = {
        "meeting_id": (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting",
        "meeting_date": (meeting.get("record_date") or meeting.get("meeting_date") or "").strip(),
        "meeting_type": (meeting.get("meeting_type") or "").strip(),
    }

    blocks: list[dict[str, str]] = []
    for index, table in enumerate(_find_all(container, "table"), start=1):
        raw_text = _clean_html_text(_node_text(table))
        if not re.search(r"(?<!\d)\d+\.\s+", raw_text):
            continue
        if not any((anchor.attrs.get("id") or "").lower().startswith("lnkagendaitem_") for anchor in _find_all(table, "a")):
            continue
        blocks.append({
            "source_body": "Board of Supervisors",
            "meeting_id": normalized_meeting["meeting_id"],
            "meeting_date": normalized_meeting["meeting_date"],
            "meeting_type": normalized_meeting["meeting_type"],
            "raw_block_index": str(index),
            "raw_text": raw_text,
            "source_url": source_url,
        })

    return blocks


def parse_args(argv=None) -> argparse.Namespace:
    """Two-pass argparse: detect source subcommand first, then parse with the right parser.

    Supports:
        bos --sync --start-date=2026-01-01
        pz --sync --start-date=2026-01-01
        --sync --start-date=2026-01-01           (defaults to bos)
        --sync-pz --pz-start-date=01/01/2026     (deprecated, kept for backward compat)
    """
    source = "bos"
    rest = list(argv if argv is not None else sys.argv[1:])

    if rest and rest[0] in ("bos", "pz"):
        source = rest.pop(0)

    if source == "bos":
        args = _parse_bos_args(rest)
    else:
        args = _parse_pz_args(rest)
    args.source = source
    return args


def _parse_bos_args(rest: list[str]) -> argparse.Namespace:
    """Parse BOS (Board of Supervisors) arguments."""
    p = argparse.ArgumentParser(description="Scrape Maricopa BOS agenda materials", prog="bos")
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--download", action="store_true", help="Download agenda/supporting files")
    p.add_argument("--extract-agenda-items", action="store_true", help="Extract agenda items from stored HTML agenda pages")
    p.add_argument("--extract-raw-agenda-blocks", action="store_true", help="Extract raw agenda-item blocks from stored HTML agenda pages")
    p.add_argument("--split-raw-agenda-blocks", action="store_true", help="Split raw agenda blocks into structured agenda items")
    p.add_argument("--self-test-splitter", action="store_true", help="Run splitter self-tests and exit")
    p.add_argument("--debug-agenda-html", action="store_true", help="Write diagnostics for the first agenda HTML page selected for item extraction")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--count-agenda-items", action="store_true", help="Visit agenda pages, count items, and print a summary table")
    p.add_argument("--list-agenda-items", action="store_true", help="Visit agenda pages and list numbered items with titles")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--persist", action="store_true", help="Persist extracted agenda items from CSV to database")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist directly to database (bypasses CSVs)")
    p.add_argument("--meeting-id", help="Single meeting ID to sync (e.g. 4449). Used with --sync to skip date search.")
    p.add_argument("--offline", action="store_true", help="Sync from a locally saved HTML file instead of the live server. Use with --sync --meeting-id.")
    p.add_argument("--from-file", help="Path to a local agenda HTML file to parse offline. Used with --sync.")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts for network/page operations (default 3)")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--sync-votes", action="store_true", help="Extract vote results from meeting summaries")
    # Deprecated PZ flags (kept for backward compatibility)
    p.add_argument("--sync-pz", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--pz-limit", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--pz-start-date", help=argparse.SUPPRESS)
    p.add_argument("--pz-end-date", help=argparse.SUPPRESS)
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    return args


def _parse_pz_args(rest: list[str]) -> argparse.Namespace:
    """Parse PZ (Planning & Zoning) arguments."""
    p = argparse.ArgumentParser(description="Scrape Maricopa Planning & Zoning agenda materials", prog="pz")
    p.add_argument("--start-date", help="Start date in YYYY-MM-DD")
    p.add_argument("--end-date", help="End date in YYYY-MM-DD")
    p.add_argument("--date", help="Single date in YYYY-MM-DD (shorthand for --start-date=DATE --end-date=DATE)")
    p.add_argument("--sync", action="store_true", help="Search online, extract agenda items, and persist to database")
    p.add_argument("--headed", action="store_true", help="Run Playwright headed")
    p.add_argument("--limit", type=int, default=None, help="Optional meeting limit")
    p.add_argument("--meeting-id", help="Single meeting ID to sync")
    p.add_argument("--offline", action="store_true", help="Sync from a locally saved HTML file instead of the live server")
    p.add_argument("--from-file", help="Path to a local agenda HTML file to parse offline")
    p.add_argument("--force", action="store_true", help="Re-sync meetings even if sync_status = complete")
    p.add_argument("--retry-count", type=int, default=3, help="Max retry attempts for network/page operations (default 3)")
    p.add_argument("--retry-failed", action="store_true", help="Sync only meetings with status failed, partial, or pending")
    p.add_argument("--init-db", action="store_true", help="Create database tables")
    p.add_argument("--status", action="store_true", help="Print summary counts of meetings by sync_status")
    p.add_argument("--failed", action="store_true", help="List failed/partial meetings with errors")
    p.add_argument("--include-manual-review", action="store_true", help="Include manual_review meetings in retry/sync operations")
    p.add_argument("--skip-complete", action="store_true", help="Skip meetings with sync_status=complete when using --meeting-id")
    args = p.parse_args(rest)
    # Normalize --date into --start-date/--end-date
    if args.date:
        if args.start_date or args.end_date:
            p.error("--date cannot be combined with --start-date or --end-date")
        args.start_date = args.date
        args.end_date = args.date
    return args


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def build_search_url(start_date: dt.date, end_date: dt.date) -> str:
    params = {
        "dropid": "11",
        "dropsv": f"{start_date:%m/%d/%Y} 00:00:00",
        "dropev": f"{end_date:%m/%d/%Y} 23:59:59",
    }
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{SEARCH_BASE}?{query}"


def slugify(value: str) -> str:
    value = re.sub(r"[\u0300-\u036f]", "", value or "")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value.lower() or "meeting"


def normalize_meeting_date(raw: str) -> str:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw or "")
    if not m:
        return ""
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def _normalize_text_date(raw: str) -> str:
    """Parse a text date like 'Apr 23, 2026' or 'April 9, 2026' to YYYY-MM-DD."""
    from datetime import datetime as _dt
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            d = _dt.strptime(raw.strip(), fmt)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def month_dir_for_date(date_iso: str, base: Path) -> Path:
    year, month, _ = date_iso.split("-")
    return base / year / month


def month_metadata_path(date_iso: str) -> Path:
    return month_dir_for_date(date_iso, AGENDAS_ROOT) / "metadata.csv"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def csv_row(fieldnames: list[str], row: dict[str, str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writerow(row)
    return buf.getvalue()


def read_existing_rows() -> dict[str, dict[str, str]]:
    existing: dict[str, dict[str, str]] = {}
    for csv_path in AGENDAS_ROOT.rglob("metadata.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    agenda_url = (row.get("agenda_url") or row.get("document_url") or "").strip()
                    if agenda_url:
                        existing[agenda_url] = row
        except FileNotFoundError:
            continue
    return existing


def write_download_row(row: dict[str, str]) -> None:
    fieldnames = [
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
    csv_path = month_metadata_path(row["record_date"])
    ensure_dir(csv_path.parent)
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_discovery_row(row: dict[str, str]) -> None:
    fieldnames = [
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
    ensure_dir(DISCOVERY_CSV.parent)
    new_file = not DISCOVERY_CSV.exists()
    with DISCOVERY_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_agenda_item_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "agenda_item_section",
        "agenda_item_id",
        "agenda_item_number",
        "agenda_item_title",
        "agenda_item_text",
        "agenda_item_url",
        "vote_or_action",
        "source_url",
    ]
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    new_file = not AGENDA_ITEMS_CSV.exists()
    with AGENDA_ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_structured_agenda_item_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "agenda_item_number",
        "agenda_item_title",
        "agenda_item_text",
        "source_url",
    ]
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    new_file = not AGENDA_ITEMS_CSV.exists()
    with AGENDA_ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_raw_agenda_item_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "raw_block_index",
        "raw_text",
        "source_url",
    ]
    ensure_dir(RAW_AGENDA_ITEMS_CSV.parent)
    new_file = not RAW_AGENDA_ITEMS_CSV.exists()
    with RAW_AGENDA_ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_rejected_raw_block_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "raw_block_index",
        "raw_text",
        "source_url",
        "rejection_reason",
    ]
    ensure_dir(REJECTED_RAW_BLOCKS_CSV.parent)
    new_file = not REJECTED_RAW_BLOCKS_CSV.exists()
    with REJECTED_RAW_BLOCKS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def debug_agenda_html_path(meeting_id: str, suffix: str) -> Path:
    return LOGS_ROOT / f"agenda_debug_{meeting_id}{suffix}"


async def write_agenda_debug_files(page, meeting: dict[str, str]) -> None:
    ensure_dir(LOGS_ROOT)
    meeting_id = (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting"
    html_path = debug_agenda_html_path(meeting_id, ".html")
    txt_path = debug_agenda_html_path(meeting_id, ".txt")
    selectors_path = debug_agenda_html_path(meeting_id, "_selectors.txt")

    html_path.write_text(await page.content(), encoding="utf-8")
    body_text = await page.locator("body").inner_text(timeout=60000)
    txt_path.write_text(body_text, encoding="utf-8")

    selector_report = await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\s+/g, ' ').trim();
          const describe = el => {
            if (!el) return 'unknown';
            const tag = el.tagName ? el.tagName.toLowerCase() : 'element';
            const id = el.id ? `#${el.id}` : '';
            const cls = el.className && typeof el.className === 'string'
              ? '.' + el.className.trim().split(/\s+/).filter(Boolean).slice(0, 4).join('.')
              : '';
            return `${tag}${id}${cls}`;
          };
          const text = el => clean(el?.innerText || el?.textContent || '');
          const candidates = Array.from(document.querySelectorAll('main, section, article, table, tbody, thead, tr, div, ul, ol, body'))
            .filter(el => text(el).length > 0)
            .slice(0, 50);
          return candidates.map((el, idx) => {
            const t = text(el);
            const rows = el.querySelectorAll('tr, li, p').length;
            const links = el.querySelectorAll('a[href]').length;
            const numbered = t.includes('1.') || t.includes('2.') || t.includes('3.');
            return {
              index: idx + 1,
              selector: describe(el),
              rows,
              links,
              numbered,
              text: t.slice(0, 500),
            };
          });
        }
        """
    )

    with selectors_path.open("w", encoding="utf-8") as f:
        for row in selector_report:
            f.write(
                f"Selector: {row['selector']}\n"
                f"Child rows/items: {row['rows']}\n"
                f"Link count: {row['links']}\n"
                f"Contains numbered items: {row['numbered']}\n"
                f"Text (first 500 chars): {row['text']}\n"
                f"---\n"
            )


def url_ext(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    ext = Path(path).suffix
    return ext if ext else ""


def infer_extension(url: str, content_type: str, fallback: str = ".bin") -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct:
        return ".html"
    ext = url_ext(url)
    if ext:
        return ext
    return fallback


def download_url(url: str, destination: Path) -> tuple[Path, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req) as resp:  # nosec - public county documents
        data = resp.read()
        content_type = resp.headers.get_content_type() if resp.headers else ""
    actual_destination = destination
    if destination.suffix == ".bin":
        actual_destination = destination.with_suffix(infer_extension(url, content_type, ".bin"))
    ensure_dir(actual_destination.parent)
    actual_destination.write_bytes(data)
    return actual_destination, content_type


def existing_paths_present(paths: str) -> bool:
    parts = [p for p in (paths or "").split(";") if p]
    if not parts:
        return False
    return all((ROOT / p).exists() for p in parts)


def row_paths_present(row: dict[str, str]) -> bool:
    return existing_paths_present(row.get("local_file_paths", "") or row.get("local_path", ""))


def read_existing_agenda_urls(csv_paths: Iterable[Path]) -> set[str]:
    existing: set[str] = set()
    for csv_path in csv_paths:
        if not csv_path.exists():
            continue
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    agenda_url = (row.get("agenda_url") or row.get("document_url") or "").strip()
                    if agenda_url:
                        existing.add(agenda_url)
        except FileNotFoundError:
            continue
    return existing


def read_existing_discovery_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                category = (row.get("document_category") or "").strip()
                url = (row.get("document_url") or "").strip()
                if category and url:
                    existing.add((category, url))
    except FileNotFoundError:
        pass
    return existing


def read_agenda_metadata_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for csv_path in AGENDAS_ROOT.rglob("metadata.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    category = (row.get("document_category") or row.get("documentCategory") or "").strip().lower()
                    agenda_url = (row.get("document_url") or row.get("agenda_url") or "").strip()
                    if category != "agenda" or not agenda_url:
                        continue
                    rows.append(row)
        except FileNotFoundError:
            continue
    return rows


def filter_agenda_metadata_rows(
    rows: list[dict[str, str]],
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
    limit: Optional[int] = None,
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for row in rows:
        record_date = (row.get("record_date") or row.get("meeting_date") or "").strip()
        try:
            parsed = dt.date.fromisoformat(record_date)
        except Exception:
            continue
        if start_date and parsed < start_date:
            continue
        if end_date and parsed > end_date:
            continue
        filtered.append(row)
        if limit is not None and len(filtered) >= limit:
            break
    return filtered


def read_existing_agenda_item_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                agenda_item_id = (row.get("agenda_item_id") or "").strip()
                agenda_item_url = (row.get("agenda_item_url") or "").strip()
                key = (meeting_id, agenda_item_id or agenda_item_url)
                if key[0] and key[1]:
                    existing.add(key)
    except FileNotFoundError:
        pass
    return existing


def read_existing_raw_block_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                raw_block_index = (row.get("raw_block_index") or "").strip()
                if meeting_id and raw_block_index:
                    existing.add((meeting_id, raw_block_index))
    except FileNotFoundError:
        pass
    return existing


def read_existing_rejected_block_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                raw_block_index = (row.get("raw_block_index") or "").strip()
                if meeting_id and raw_block_index:
                    existing.add((meeting_id, raw_block_index))
    except FileNotFoundError:
        pass
    return existing


def read_existing_structured_item_keys(csv_path: Path) -> set[tuple[str, str, str]]:
    existing: set[tuple[str, str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                agenda_item_number = (row.get("agenda_item_number") or "").strip()
                agenda_item_title = (row.get("agenda_item_title") or "").strip()
                if meeting_id and agenda_item_number:
                    existing.add((meeting_id, agenda_item_number, agenda_item_title))
    except FileNotFoundError:
        pass
    return existing


def split_bilingual_title(title: str) -> str:
    title = _clean_line(title)
    if " - " in title:
        return title.split(" - ", 1)[0].strip()
    if " / " in title:
        return title.split(" / ", 1)[0].strip()
    return title


def _raw_block_boilerplate_reason(line: str) -> str:
    if _looks_like_boilerplate(line):
        return "boilerplate first line"
    if re.search(r"\baudio access code\b", line, re.I):
        return "contains Audio Access code boilerplate"
    return ""


def validate_raw_block(raw_text: str) -> tuple[bool, str]:
    text = (raw_text or "").strip()
    if not text:
        return False, "empty raw text"
    first_line = _clean_line(text.splitlines()[0] if text.splitlines() else text)
    if not first_line:
        return False, "missing first line"
    if re.match(r"^\d{1,2}:\d{2}\s?[AP]M\b", first_line, re.I):
        return False, "begins with time"
    if re.match(r"^\d+\s+[A-Za-z]", first_line):
        return False, "begins with address"
    boilerplate_reason = _raw_block_boilerplate_reason(first_line)
    if boilerplate_reason:
        return False, boilerplate_reason
    if not re.match(r"^\d+\.\s+.+", first_line):
        return False, "does not begin with numbered agenda item"

    spam_terms = [
        "meeting location",
        "board members",
        "mission",
        "webinar",
        "public notice",
        "live video feeds",
        "the public is invited",
        "accommodations for individuals",
    ]
    lowered = text.lower()
    if any(term in lowered for term in spam_terms):
        return False, "contains non-agenda notice text"

    return True, ""


def split_raw_block_into_items(raw_text: str) -> list[dict[str, str]]:
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    if not text:
        return []

    matches = list(re.finditer(r"(?<!\d)(\d+)\.\s+", text))
    if not matches:
        return []

    items: list[dict[str, str]] = []

    for idx, match in enumerate(matches):
        number = int(match.group(1))
        if idx == 0:
            if number != 1 and len(matches) > 1:
                # still accept the first visible top-level item if it is the first number we see
                pass
        else:
            prev_number = int(matches[idx - 1].group(1))
            if number != prev_number + 1:
                continue

        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if not re.match(r"^\d+\.\s+", block):
            continue

        header = re.match(r"^(\d+)\.\s*(.*)$", block)
        if not header:
            continue

        agenda_number = header.group(1)
        body = header.group(2).strip()
        title = split_bilingual_title(body)
        if not title:
            title = body[:200]

        items.append({
            "agenda_item_number": agenda_number,
            "agenda_item_title": title,
            "agenda_item_text": block,
        })

    return items


def splitter_self_test(verbose: bool = False) -> bool:
    cases = [
        (
            "3. TREASURER ... 4. RECORDER ...",
            2,
            ["3", "4"],
        ),
        (
            "6. DOMRES 90 Case #: MCP250001 a. Development shall ... b. Site plan shall ...",
            1,
            ["6"],
        ),
        (
            "1. ROLL CALL 2. INVOCATION 3. PLEDGE OF ALLEGIANCE",
            3,
            ["1", "2", "3"],
        ),
        (
            "This item includes 24 hours advance notice for public comment.",
            0,
            [],
        ),
        (
            "Audio Access code 154-419-871 is provided for attendees.",
            0,
            [],
        ),
        (
            "1. TITLE ... (C-06-25-252-X-00) 2. TITLE ...",
            2,
            ["1", "2"],
        ),
    ]

    passed = True
    for idx, (sample, expected_count, expected_numbers) in enumerate(cases, start=1):
        items = split_raw_block_into_items(sample)
        numbers = [item["agenda_item_number"] for item in items]
        ok = len(items) == expected_count and numbers == expected_numbers
        passed = passed and ok
        if verbose:
            print(f"splitter_self_test case {idx}: {'PASS' if ok else 'FAIL'} (got {len(items)} items: {numbers})")

    if verbose:
        print(f"splitter_self_test overall: {'PASS' if passed else 'FAIL'}")
    return passed


def split_raw_agenda_blocks_to_structured() -> int:
    if not RAW_AGENDA_ITEMS_CSV.exists():
        print("No raw_agenda_items.csv found.")
        return 0

    ensure_dir(AGENDA_ITEMS_CSV.parent)
    if not AGENDA_ITEMS_CSV.exists():
        AGENDA_ITEMS_CSV.write_text(
            "source_body,meeting_id,meeting_date,meeting_type,agenda_item_number,agenda_item_title,agenda_item_text,source_url\n",
            encoding="utf-8",
        )
    existing_keys = read_existing_structured_item_keys(AGENDA_ITEMS_CSV)
    rejected_keys = read_existing_rejected_block_keys(REJECTED_RAW_BLOCKS_CSV)
    wrote = 0

    with RAW_AGENDA_ITEMS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            meeting_id = (raw_row.get("meeting_id") or "").strip()
            meeting_date = (raw_row.get("meeting_date") or "").strip()
            meeting_type = (raw_row.get("meeting_type") or "").strip()
            source_url = (raw_row.get("source_url") or "").strip()
            raw_text = raw_row.get("raw_text") or ""
            raw_block_index = (raw_row.get("raw_block_index") or "").strip()
            is_valid, reason = validate_raw_block(raw_text)
            if not is_valid:
                key = (meeting_id, raw_block_index)
                if key not in rejected_keys:
                    write_rejected_raw_block_row({
                        "source_body": "Board of Supervisors",
                        "meeting_id": meeting_id,
                        "meeting_date": meeting_date,
                        "meeting_type": meeting_type,
                        "raw_block_index": raw_block_index,
                        "raw_text": raw_text,
                        "source_url": source_url,
                        "rejection_reason": reason,
                    })
                    rejected_keys.add(key)
                continue

            for item in split_raw_block_into_items(raw_text):
                key = (meeting_id, item["agenda_item_number"], item["agenda_item_title"])
                if key in existing_keys:
                    continue
                write_structured_agenda_item_row({
                    "source_body": "Board of Supervisors",
                    "meeting_id": meeting_id,
                    "meeting_date": meeting_date,
                    "meeting_type": meeting_type,
                    "agenda_item_number": item["agenda_item_number"],
                    "agenda_item_title": item["agenda_item_title"],
                    "agenda_item_text": item["agenda_item_text"],
                    "source_url": source_url,
                })
                existing_keys.add(key)
                wrote += 1

    return wrote


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line or "").strip()


def _looks_like_boilerplate(line: str) -> bool:
    return bool(re.match(r"^(?:page\s+\d+.*|copyright.*|hyland software.*|view meeting.*|agenda online.*)$", line, re.I))


def _looks_like_item_heading(line: str) -> Optional[re.Match[str]]:
    line = _clean_line(line)
    if not line or _looks_like_boilerplate(line):
        return None
    return re.match(r"^(?P<number>\d+(?:\.\d+)*)\.?\s*(?P<title>.*)$", line)


def _looks_like_section_heading(line: str) -> bool:
    line = _clean_line(line)
    if not line or _looks_like_boilerplate(line):
        return False
    if re.match(r"^\d", line):
        return False
    if len(line) > 180:
        return False
    if any(ch.isdigit() for ch in line):
        return False
    if any(token in line for token in [":", "/", "AM", "PM"]):
        return False
    letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", line)
    if not letters:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / max(len(letters), 1)
    return upper_ratio >= 0.65 and bool(re.search(r"[A-Za-zÀ-ÿ]", line))


def _detect_vote_or_action(text: str) -> str:
    t = text.lower()
    action_patterns = [
        (r"\bno action\b", "no action"),
        (r"\breceived and filed\b", "received and filed"),
        (r"\bapproved\b", "approved"),
        (r"\badopted\b", "adopted"),
        (r"\bpassed\b", "passed"),
        (r"\bfailed\b", "failed"),
        (r"\bdenied\b", "denied"),
        (r"\bcontinued\b", "continued"),
        (r"\bheld\b", "held"),
        (r"\bpostponed\b", "postponed"),
    ]
    for pattern, label in action_patterns:
        if re.search(pattern, t, re.I):
            return label
    return ""


def _build_item_url(source_url: str, agenda_item_id: str) -> str:
    return f"{source_url}#{urllib.parse.quote(agenda_item_id, safe='')}"


CASE_PATTERN = re.compile(
    r"\b(CPA[A-Z]?\d+|Z\d{5,}|Z\d+-?\d*|GPA\d+|MCP\d+|SU\d+|PD\d+|TU\d+|[A-Z]{2,5}\d{5,}|RECONSIDERATION\s+OF\s+[A-Z]+\d+|SPLIT\s+CASE\s+[A-Z]+\d+)\b", re.I
)


PZ_SEARCH_BASE = "https://www.maricopa.gov/AgendaCenter/Search/"
PZ_AGENDA_BASE = "https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/"


C_NUMBER_PATTERN = re.compile(
    r"\(?(C-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3}){1,3})\)?"
)


def _extract_c_number(text: str) -> str:
    """Extract the first C-number from item text.

    Pattern: (C-XX-XX-XXX-XXX-XX) or C-XX-XX-XXX-XXX-XX
    Returns the number without parentheses, or empty string.
    """
    if not text:
        return ""
    m = C_NUMBER_PATTERN.search(text)
    if m:
        return m.group(1)
    return ""


def parse_c_number_parts(c_number: str) -> dict[str, str]:
    """Split a C-number into base and revision parts.

    C-86-25-040-X-00 => base=C-86-25-040-X, revision=00
    C-06-25-199-02   => base=C-06-25-199,   revision=02
    """
    if not c_number:
        return {"c_number": "", "c_number_base": "", "c_number_revision": ""}
    # The revision is the last dash-separated segment
    last_dash = c_number.rfind("-")
    if last_dash < 0:
        return {"c_number": c_number, "c_number_base": c_number, "c_number_revision": ""}
    revision = c_number[last_dash + 1:]
    base = c_number[:last_dash]
    # Verify revision looks like a revision code (alphanumeric, 1-3 chars)
    if len(revision) <= 4:
        return {"c_number": c_number, "c_number_base": base, "c_number_revision": revision}
    # If the last segment is too long to be a revision, treat the whole thing as base
    return {"c_number": c_number, "c_number_base": c_number, "c_number_revision": ""}


def parse_agenda_items_from_html(html: str, source_url: str, meeting: dict[str, str]) -> list[dict[str, str]]:
    """Extract agenda items from HTML by identifying true top-level numbered items.

    Real agenda items are marked in the HTML with a bold <span> containing
    the item number followed by a period:
        <span style="font-weight:bold">1.</span>

    This avoids promoting nested numbered paragraphs (a., b., c.), warrant
    numbers, dollar amounts, parcel numbers, and boilerplate into agenda rows.
    Item titles use the first lnkAgendaItem anchor after the bold span.
    Subsequent links in the same table are section headings for upcoming items.
    Items without any anchor fall back to the most recent section heading.
    """
    meeting_id = meeting["meeting_id"]
    bold_item_pattern = re.compile(
        r'<span[^>]*font-weight:bold[^>]*>(\d+)\.</span>'
    )

    item_spans: list[tuple[int, int]] = []
    for m in bold_item_pattern.finditer(html):
        num = int(m.group(1))
        pos = m.start()
        item_spans.append((num, pos))

    if not item_spans:
        return []

    item_spans.sort(key=lambda x: x[1])

    seen_positions: set[int] = set()
    deduped: list[tuple[int, int]] = []
    for num, pos in item_spans:
        if pos in seen_positions:
            continue
        seen_positions.add(pos)
        deduped.append((num, pos))

    items: list[dict[str, str]] = []
    pending_section = ""

    for item_num, pos in deduped:
        number_str = str(item_num)

        before = html[:pos]
        tstart = before.rfind("<table")
        tend = html.find("</table>", pos)
        if tstart < 0 or tend < 0:
            continue
        table_html = html[tstart : tend + 8]

        # Find ALL lnkAgendaItem anchors in this table (after the bold span)
        bold_offset = pos - tstart
        lnk_titles: list[str] = []
        for lm in re.finditer(
            r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>', table_html, re.DOTALL
        ):
            if lm.start() <= bold_offset:
                continue
            raw = re.sub(r"<[^>]+>", " ", lm.group(1)).strip()
            raw = _clean_html_text(raw)
            if raw:
                lnk_titles.append(raw)

        if lnk_titles:
            title = split_bilingual_title(lnk_titles[0])
            for extra_title in lnk_titles[1:]:
                pending_section = split_bilingual_title(extra_title)
        else:
            if pending_section:
                title = pending_section
            else:
                # No title in table and no pending section — scan backward in
                # the full HTML for the nearest preceding lnkAgendaItem.
                # Handles items like "CALL TO THE PUBLIC" whose section heading
                # lives in the gap between item tables.
                title = f"Item {number_str}"
                before_html = html[:pos]
                for prev_m in reversed(
                    list(
                        re.finditer(
                            r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>',
                            before_html,
                            re.DOTALL,
                        )
                    )
                ):
                    raw = re.sub(r"<[^>]+>", " ", prev_m.group(1)).strip()
                    raw = _clean_html_text(raw)
                    if raw:
                        title = split_bilingual_title(raw)
                        break

        item_id = f"{meeting_id}-{number_str}-item"
        full_text = _clean_html_text(
            re.sub(r"<[^>]+>", " ", table_html)
        )

        items.append({
            "source_body": "Board of Supervisors",
            "meeting_id": meeting_id,
            "meeting_date": meeting["meeting_date"],
            "meeting_type": meeting["meeting_type"],
            "agenda_item_section": "",
            "agenda_item_id": item_id,
            "agenda_item_number": number_str,
            "agenda_item_title": title[:500],
            "agenda_item_text": full_text[:10000],
            "agenda_item_url": _build_item_url(source_url, item_id),
            "vote_or_action": _detect_vote_or_action(full_text),
            "c_number": _extract_c_number(full_text),
            "c_number_base": "",
            "c_number_revision": "",
            "case_number": "",
            "source_url": source_url,
        })

        # Populate base/revision after the item dict is in items
        # Extract case number from item text and title
        c_m = CASE_PATTERN.search(full_text + " " + (title or ""))
        if c_m:
            items[-1]["case_number"] = c_m.group(1).upper()
        c_num = items[-1]["c_number"]
        if c_num:
            parts = parse_c_number_parts(c_num)
            items[-1]["c_number_base"] = parts["c_number_base"]
            items[-1]["c_number_revision"] = parts["c_number_revision"]

    return items


def _extract_supporting_docs_from_table(table_html: str, agenda_item_dict: dict, base_url: str) -> list[dict]:
    """Extract supporting document links from an agenda item's table HTML.

    Looks for anchor tags pointing to external documents (PDF, DOC,
    URLs containing /Document/, /File/, etc.).
    """
    docs: list[dict] = []
    seen_urls: set[str] = set()

    doc_pattern = re.compile(
        r'href="(?!\#)([^"]*(?:Document|File|Attachment|download|\\.pdf|\\.doc)"[^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL | re.I,
    )
    for m in doc_pattern.finditer(table_html):
        url = m.group(1).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
        title = _clean_html_text(title)
        abs_url = urllib.parse.urljoin(base_url, url) if not url.startswith("http") else url

        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)

        parsed = urllib.parse.urlparse(abs_url)
        path = Path(parsed.path) if parsed.path else Path(title)
        file_name = path.name or None
        ext = path.suffix.lstrip(".") or None

        docs.append({
            "agenda_item_id": 0,
            "meeting_id": agenda_item_dict.get("meeting_id", ""),
            "agenda_item_number": int(agenda_item_dict.get("agenda_item_number", 0)),
            "c_number": agenda_item_dict.get("c_number", "") or None,
            "c_number_base": agenda_item_dict.get("c_number_base", "") or None,
            "c_number_revision": agenda_item_dict.get("c_number_revision"),
            "document_title": title or file_name or "",
            "document_url": abs_url,
            "document_type": ext.upper() if ext else None,
            "file_name": file_name,
            "file_extension": ext,
        })

    return docs


def extract_supporting_documents_from_items(
    html: str,
    agenda_items: list[dict],
    source_url: str,
) -> list[dict]:
    """Extract supporting documents from the full agenda HTML.

    Finds each item's table, then searches for document links within it.
    Returns a flat list of supporting document dicts.
    """
    all_docs: list[dict] = []
    seen_urls: set[str] = set()

    for item_dict in agenda_items:
        item_num = int(item_dict.get("agenda_item_number", 0))
        if not item_num:
            continue

        # Find the item's bold span in the HTML
        bold_pattern = re.compile(
            r'<span[^>]*font-weight:bold[^>]*>'
            + re.escape(str(item_num))
            + r'\.</span>'
        )
        m = bold_pattern.search(html)
        if not m:
            continue

        pos = m.start()
        tstart = html.rfind("<table", 0, pos)
        tend = html.find("</table>", pos)
        if tstart < 0 or tend < 0:
            continue
        table_html = html[tstart : tend + 8]

        docs = _extract_supporting_docs_from_table(table_html, item_dict, source_url)
        for doc in docs:
            url = doc["document_url"]
            if url not in seen_urls:
                seen_urls.add(url)
                all_docs.append(doc)

    return all_docs


async def extract_supporting_documents_dynamic(
    page,
    agenda_items: list[dict],
    base_url: str,
) -> list[dict]:
    """Extract supporting documents by clicking each agenda item link.

    On Agenda Online, supporting documents are revealed by clicking each
    agenda item link, which populates a #itemView div via AJAX. The
    interactive links are those where the page's JavaScript has bound a
    click handler that calls loadAgendaItem(). These links have href="#"
    and live inside #agendaView.

    For each interactive link:
    1. Click the link
    2. Wait for #itemView to update
    3. Extract the C-number from .item-view-title-text
    4. Extract supporting document links from lnkAttachment_* anchors
    5. Look up the corresponding agenda item by C-number
    6. Build supporting document dicts with meeting_id and agenda_item_number

    Returns a flat list of supporting document dicts ready for persist_meeting().
    """
    all_docs: list[dict] = []
    seen_urls: set[str] = set()

    # Build a lookup: C-number → agenda_item_dict
    # Also build a text-based fallback lookup
    items_by_c_number: dict[str, dict] = {}
    items_by_text: dict[str, dict] = {}
    items_ordered: list[dict] = list(agenda_items)
    for item_dict in agenda_items:
        c_num = (item_dict.get("c_number") or "").strip()
        if c_num:
            items_by_c_number[c_num] = item_dict
        title = (item_dict.get("agenda_item_title") or "").strip().lower()
        if title:
            items_by_text[title] = item_dict

    # Find interactive links (href="#") in #agendaView
    interactive_links = await page.evaluate(
        """() => {
            const container = document.getElementById('agendaView');
            if (!container) return [];
            const links = container.querySelectorAll('a[href="#"]');
            return Array.from(links).map(l => ({
                id: l.id,
                text: (l.textContent || '').trim()
            }));
        }"""
    )

    if not interactive_links:
        return all_docs

    # Local reference to avoid repeated re-import
    join = urllib.parse.urljoin

    for link_info in interactive_links:
        link_id = link_info["id"]
        link_text = link_info["text"]

        try:
            # Click and extract in one evaluate call with timeout
            result = await asyncio.wait_for(
                _click_and_extract_item(page, link_id),
                timeout=12,
            )

            if result is None:
                continue

            c_number = result.get("c_number", "")
            attachments = result.get("attachments", [])

            if not attachments:
                continue

            # Look up the agenda item by C-number or link text
            item_dict = None
            if c_number and c_number in items_by_c_number:
                item_dict = items_by_c_number[c_number]
            elif link_text.lower() in items_by_text:
                item_dict = items_by_text[link_text.lower()]

            meeting_id = (item_dict or {}).get("meeting_id", "")
            base_item_num = int((item_dict or {}).get("agenda_item_number", 0))
            c_number_parts = parse_c_number_parts(c_number) if c_number else {}

            for att in attachments:
                url = att.get("href", "")
                if not url or url in seen_urls:
                    continue
                abs_url = join(base_url, url) if not url.startswith("http") else url
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)

                title = att.get("text", "")
                parsed = urllib.parse.urlparse(abs_url)
                path = Path(parsed.path) if parsed.path else Path(title)
                file_name = path.name or None
                ext = path.suffix.lstrip(".") or None
                ext = ext or url_ext(abs_url).lstrip(".") or None

                doc = {
                    "agenda_item_id": base_item_num,
                    "meeting_id": meeting_id,
                    "agenda_item_number": base_item_num,
                    "c_number": c_number if c_number else None,
                    "c_number_base": c_number_parts.get("c_number_base", "") or None,
                    "c_number_revision": c_number_parts.get("c_number_revision"),
                    "document_title": title or file_name or "",
                    "document_url": abs_url,
                    "document_type": ext.upper() if ext else None,
                    "file_name": file_name,
                    "file_extension": ext,
                }
                all_docs.append(doc)

        except asyncio.TimeoutError:
            continue
        except Exception:
            continue

    return all_docs


async def _click_and_extract_item(page, link_id: str) -> dict | None:
    """Click an interactive agenda item link and extract item view data.

    Waits for #itemView content to change after the click (tracked via a
    page-level `__ocLastItemViewInnerLength` counter to avoid race conditions
    with stale AJAX data from previous clicks).
    Queries attachment anchors scoped to #itemView only.

    Returns a dict with 'c_number' and 'attachments' keys, or None if
    the click failed or timed out.
    """
    # Click the link
    clicked = await page.evaluate(
        f"""(id) => {{
            const el = document.getElementById(id);
            if (!el) return false;
            el.click();
            return true;
        }}""",
        link_id,
    )
    if not clicked:
        return None

    # Wait for #itemView content to CHANGE (not just exist — it's already
    # populated from a previous click, so children.length > 0 would race)
    try:
        await page.wait_for_function(
            """() => {
                const iv = document.getElementById('itemView');
                if (!iv || !iv.children.length) return false;
                const prevLen = window.__ocLastItemViewInnerLength || 0;
                const currLen = iv.innerHTML.length;
                if (currLen !== prevLen) {
                    window.__ocLastItemViewInnerLength = currLen;
                    return true;
                }
                return false;
            }""",
            timeout=10000,
        )
    except Exception:
        pass

    # Small settle time
    await page.wait_for_timeout(300)

    # Extract C-number and attachments scoped to #itemView
    result = await page.evaluate(
        """() => {
            const iv = document.getElementById('itemView');
            if (!iv) return { c_number: '', attachments: [] };
            const cnum = iv.querySelector('.item-view-title-text');
            const c_number = cnum ? cnum.textContent.trim() : '';
            const anchors = iv.querySelectorAll('a[id^="lnkAttachment_"]');
            const attachments = Array.from(anchors).map(a => ({
                href: a.getAttribute('href') || '',
                text: (a.textContent || '').trim()
            }));
            return { c_number, attachments };
        }"""
    )

    return result


async def extract_votes_from_summary(page, source_url: str, agenda_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Extract vote results from a meeting summary page (doctype=3).

    Visits the summary URL, waits for #agendaView to load, extracts the
    text content, and parses it to find:
    - Supervisors present (with district info)
    - For each agenda item: motion text, Ayes, Nays, and withdrawn status

    The summary page uses \xa0 (non-breaking space) as the item separator
    instead of newlines, so parsing works on the single-line text via regex.

    Args:
        page: Playwright page object
        source_url: The summary URL (doctype=3)
        agenda_items: List of parsed agenda item dicts (for matching C-numbers)

    Returns:
        (supervisors, votes) where:
        - supervisors: [{"name": ..., "normalized_name": ..., "district": ..., "role": ...}]
        - votes: [{"agenda_item_number": ..., "c_number": ..., "motion_result": ...,
                   "vote_text": ..., "supervisor_votes": [{"name": ..., "vote": ...}]}]
    """
    await page.goto(source_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Wait for #agendaView to be populated
    try:
        await page.wait_for_function(
            """() => {
                const av = document.getElementById('agendaView');
                return av && av.textContent && av.textContent.length > 100;
            }""",
            timeout=15000,
        )
    except Exception:
        pass

    # Get the text content of the agenda view
    text = await page.evaluate(
        """() => {
            const av = document.getElementById('agendaView');
            return av ? av.textContent || '' : '';
        }"""
    )

    if not text or len(text.strip()) < 50:
        return [], []

    # Normalize: collapse \xa0 to regular spaces so we can split and match
    # The summary uses \xa0 between items
    text_normalized = text.replace("\xa0", " ")
    # Also collapse multiple spaces
    text_normalized = re.sub(r"\s{3,}", "\n\n", text_normalized)

    # --- Parse supervisors present ---
    supervisors: list[dict] = []
    # Extract everything between "with the following members present:" and ". Also present:"
    sup_match = re.search(
        r"with the following members present:\s+(.*?)\.\s*Also present",
        text_normalized,
        re.I | re.DOTALL,
    )
    if not sup_match:
        # Try alternative ending: just before a numbered item
        sup_match = re.search(
            r"with the following members present:\s+(.*?)(?=\d+\.)",
            text_normalized,
            re.I | re.DOTALL,
        )
    if sup_match:
        sup_text = sup_match.group(1)
        # Split by semicolons
        for part in re.split(r";\s*", sup_text):
            part = part.strip().rstrip(";,.")
            if not part:
                continue
            # Remove parenthetical comments like "(entered the meeting late)"
            part = re.sub(r"\s*\([^)]*\)", "", part).strip()
            # Match "Thomas Galvin, Chairman, District 2" or "Thomas Galvin, District 2"
            m = re.match(
                r"([A-Za-z]+(?:\s+[A-Za-z']+)+)"
                r"(?:,\s*(?:Chairman|Vice Chair|Supervisor))?"
                r"(?:,\s*District\s+(\d+))?",
                part,
                re.I,
            )
            if m:
                name = m.group(1).strip()
                district = m.group(2)
                role = ""
                if re.search(r"Chairman\b", part, re.I) and not re.search(r"Vice", part, re.I):
                    role = "Chairman"
                elif re.search(r"Vice Chair", part, re.I):
                    role = "Vice Chair"
                if name:
                    supervisors.append({
                        "name": name,
                        "normalized_name": re.sub(r"[^a-z0-9]+", " ", name.lower()).strip(),
                        "district": district,
                        "role": role if role else None,
                        "present": True,
                    })

    # --- Build a lookup from item number to C-number from agenda_items ---
    item_cnumber_map: dict[str, str] = {}
    for item in agenda_items:
        num = str(item.get("agenda_item_number", ""))
        c = item.get("c_number", "") or ""
        if num and c:
            item_cnumber_map[num] = c

    # --- Parse votes for each agenda item ---
    votes: list[dict] = []

    # Split the text into sections by numbered items
    # The text is run-together like "...5.SUN BASIN..." with non-breaking
    # spaces or double spaces as separators
    lines = text_normalized.split("\n")

    # Find all line positions where a numbered item starts.
    # Use finditer to catch multiple items per line (common when normalization
    # doesn't split them onto their own line).
    item_boundaries: list[tuple[int, int, str, str]] = []
    for i, line in enumerate(lines):
        for m in re.finditer(r"(?:^|\D)(\d{1,3})\.\s*([A-Z])", line):
            num = m.group(1)
            # Skip numbers that look like dates or other non-items
            if len(num) > 3 and num not in item_cnumber_map:
                continue
            # Use character position within the line for section ordering
            pos = m.start()
            rest = line[pos:].lstrip()
            rest = re.sub(r"^\d+\.\s*", "", rest)
            c_m = re.search(r"\(([A-Z]-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3}){1,3})\)", rest)
            c_num = c_m.group(1) if c_m else item_cnumber_map.get(num, "")
            item_boundaries.append((i, pos, num, c_num))

    # Use a counter for unique agenda_item_id within this batch
    agenda_item_counter = 0

    # Parse each item's section for vote information
    for idx, (start_line, _start_pos, item_num, c_num) in enumerate(item_boundaries):
        end_line = item_boundaries[idx + 1][0] if idx + 1 < len(item_boundaries) else len(lines)
        section_lines = lines[start_line:end_line]
        section_text = " ".join(line.strip() for line in section_lines)
        section_text = re.sub(r"\s+", " ", section_text).strip()

        # Check for "withdrawn" 
        if re.search(r"\bwithdrawn\b", section_text, re.I):
            agenda_item_counter += 1
            votes.append({
                "agenda_item_id": agenda_item_counter,
                "agenda_item_number": int(item_num),
                "c_number": c_num if c_num else None,
                "c_number_base": c_num[:-4] if c_num and len(c_num) > 4 else c_num if c_num else None,
                "motion_result": "withdrawn",
                "vote_text": section_text[:2000],
                "supervisor_votes": [],
            })
            continue

        # Find motion line
        motion_match = re.search(
            r"Motion to (\w+)[^.]*?(?:by Supervisor ([^,]+),\s*seconded by Supervisor ([^)]+))",
            section_text,
            re.I,
        )

        # Build a set of known supervisor normalized names for filtering
        known_supervisor_names = {s["normalized_name"] for s in supervisors}
        # Also match partial names (first + last)
        def is_known_supervisor(name: str) -> bool:
            """Check if a name matches a known supervisor."""
            normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            if normalized in known_supervisor_names:
                return True
            # Check partial matches: if the name starts with a known supervisor's name
            for known in known_supervisor_names:
                if normalized.startswith(known) or known.startswith(normalized):
                    return True
            return False

        # Find Ayes - capture names and stop before text that isn't a name
        ayes: list[str] = []
        ayes_match = re.search(r"Ayes:\s*(.*?)(?:\s*Nay:|\s*$)", section_text, re.I)
        if ayes_match:
            raw = ayes_match.group(1).strip()
            # Only keep entries that look like names (shorter than 60 chars, start with capital letter)
            candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
            for c in candidates:
                # Clean up: trim trailing content after a Spanish translation marker "-"
                # Supervisors don't have " - " in their names
                c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
                # Remove trailing Spanish section names (ALL CAPS)
                c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
                # Remove trailing text after role markers
                c = re.sub(r"\s+(County|Human|Public|Parks|Transportation|Elections|Risk|Finance|Real Estate|Library|Planning).*$", "", c, flags=re.I).strip()
                # Remove trailing text after specific section names
                c = re.sub(r"\s+STATUTORY.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+AUDIENCIAS.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+BOARD.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+CALL TO.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+LIBRARY.*$", "", c, flags=re.I).strip()
                c = c.rstrip(",;.:").strip()
                if not c or len(c) < 3 or len(c) > 60:
                    continue
                if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                    continue
                if re.search(r"\b(with|and|the|for|of|that|this|from|please|email|prior|local|fire|written|except|amenos|como|que|del|para|una|los|las|por|notado)", c, re.I):
                    continue
                if c not in ayes:
                    ayes.append(c)

        # Filter ayes against known supervisors when we have them
        if known_supervisor_names and len(ayes) > len(supervisors):
            filtered = [n for n in ayes if is_known_supervisor(n)]
            if filtered:
                ayes = filtered

        # Find Nays - same approach
        nays: list[str] = []
        nays_match = re.search(r"Nay:\s*(.*?)(?:\s+(?=\d+\.)|\s*$)", section_text, re.I)
        if nays_match:
            raw = nays_match.group(1).strip()
            candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
            for c in candidates:
                c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
                c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
                c = c.rstrip(",;.:").strip()
                if not c or len(c) < 3 or len(c) > 60:
                    continue
                if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                    continue
                if c not in nays:
                    nays.append(c)

            # Filter nays against known supervisors
            if known_supervisor_names:
                filtered = [n for n in nays if is_known_supervisor(n)]
                if filtered:
                    nays = filtered

        # Determine motion_result
        motion_result = ""
        if motion_match:
            action = motion_match.group(1).lower()
            if action in ("approve", "adopt", "concur"):
                motion_result = "approved"
            elif action in ("deny", "denied"):
                motion_result = "denied"
            elif action == "continue":
                motion_result = "continued"
            else:
                motion_result = action
        elif ayes and not nays:
            motion_result = "approved"
        elif nays:
            motion_result = "carried"

        # Build a lookup: normalized_name → canonical supervisor name/role
        known_supervisor_lookup: dict[str, dict] = {
            s["normalized_name"]: s for s in supervisors
        }

        supervisor_votes: list[dict] = []

        def find_canonical_name(raw_name: str) -> str:
            """Match a raw extracted name to its canonical supervisor name."""
            cleaned = re.sub(r"^Supervisor\s+|^Vice Chair\s+|^Chairman\s+", "", raw_name, flags=re.I).strip()
            normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
            # Direct match
            if normalized in known_supervisor_lookup:
                return known_supervisor_lookup[normalized]["name"]
            # Partial match
            for kn, kd in known_supervisor_lookup.items():
                if normalized.startswith(kn) or kn.startswith(normalized):
                    return kd["name"]
            return cleaned

        # Build ayes as "yes" votes
        for name in ayes:
            canonical = find_canonical_name(name)
            if canonical and not re.match(r"^(\d+|none|and)$", canonical, re.I):
                nrm = re.sub(r"[^a-z0-9]+", " ", canonical.lower()).strip()
                supervisor_votes.append({
                    "name": canonical,
                    "normalized_name": nrm,
                    "vote": "yes",
                    "raw_vote_text": "Ayes",
                })

        # Build nays as "no" votes
        for name in nays:
            canonical = find_canonical_name(name)
            if canonical and not re.match(r"^(\d+|none|and)$", canonical, re.I):
                nrm = re.sub(r"[^a-z0-9]+", " ", canonical.lower()).strip()
                supervisor_votes.append({
                    "name": canonical,
                    "normalized_name": nrm,
                    "vote": "no",
                    "raw_vote_text": "Nay",
                })

        # Deduplicate supervisor_votes by normalized_name
        seen_sup: set[str] = set()
        deduped_sv: list[dict] = []
        for sv in supervisor_votes:
            key = sv.get("normalized_name", sv.get("name", "").lower())
            if key not in seen_sup:
                seen_sup.add(key)
                deduped_sv.append(sv)
        supervisor_votes = deduped_sv

        if motion_result or supervisor_votes:
            agenda_item_counter += 1
            votes.append({
                "agenda_item_id": agenda_item_counter,
                "agenda_item_number": int(item_num),
                "c_number": c_num if c_num else None,
                "c_number_base": c_num[:-4] if c_num and len(c_num) > 4 else c_num if c_num else None,
                "motion_result": motion_result or "unknown",
                "vote_text": section_text[:2000],
                "supervisor_votes": supervisor_votes,
            })

    return supervisors, votes


def parse_metadata_from_page_data(page_data: dict) -> dict:
    """Parse meeting metadata from page text data.

    Pure function counterpart to extract_meeting_metadata_from_page.
    Takes the dict from page.evaluate and returns parsed metadata.
    """
    body = page_data.get("bodyText", "")
    result = {
        "meeting_date": "",
        "meeting_type": "",
        "meeting_title": "",
    }

    # Title: use header or formTitle
    result["meeting_title"] = (page_data.get("formTitle") or page_data.get("headerText") or "").strip()

    # Date: parse MM/DD/YYYY from body text
    date_m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", body)
    if date_m:
        result["meeting_date"] = f"{date_m.group(3)}-{int(date_m.group(1)):02d}-{int(date_m.group(2)):02d}"

    # Type: look for Formal Meeting / Informal Meeting in body
    type_m = re.search(r"\b(FORMAL|INFORMAL)\s+MEETING\b", body, re.I)
    if type_m:
        result["meeting_type"] = type_m.group(1).capitalize() + " Meeting"

    return result


async def extract_meeting_metadata_from_page(page, source_url: str) -> dict:
    """Extract meeting date, type, and title from the loaded agenda page.

    Grabs page body text via Playwright, then delegates to
    parse_metadata_from_page_data for structured parsing.
    """
    try:
        data = await page.evaluate(
            """() => {
                return {
                    bodyText: document.body.textContent || '',
                    headerText: (
                        document.querySelector('.view-header-area') ||
                        document.querySelector('.page-header') ||
                        document.querySelector('.meeting-header') ||
                        document.querySelector('h1') ||
                        document.querySelector('h2') ||
                        { textContent: '' }
                    ).textContent.trim(),
                    formTitle: (
                        document.getElementById('formTitle') ||
                        { textContent: '' }
                    ).textContent.trim(),
                };
            }"""
        )
    except Exception:
        return {"meeting_date": "", "meeting_type": "", "meeting_title": ""}

    return parse_metadata_from_page_data(data)


async def is_image_based_agenda(page) -> bool:
    """Check if the agenda page is image-based (scanned) and unparseable.

    Looks for:
    - Zero interactive links (href=\"#\" in #agendaView)
    - Zero numbered table items matching the agenda pattern
    - Significant base64 image data as primary content
    - Zero lnkAgendaItem_TOC links with numeric patterns
    """
    try:
        data = await page.evaluate(
            """() => {
                const av = document.getElementById('agendaView');
                if (!av) return { interactive: 0, tables: 0, images: 0, hasNumberedItems: false };
                const interactive = av.querySelectorAll('a[href="#"]').length;
                const tables = av.querySelectorAll('table').length;
                const images = av.querySelectorAll('img').length;
                const text = av.textContent || '';
                // Check for numbered item patterns like "1. " or "1)"
                const hasNumberedItems = /\\b\\d+\\.\\s+|\\b\\d+\\)\\s+/.test(text);
                // Check for image-based content (data URI images, large image content)
                const hasDataUriImages = av.innerHTML.includes('data:image');
                return { interactive, tables, images, hasNumberedItems, hasDataUriImages, textLen: text.length };
            }"""
        )
        # Image-based: no interactive links, no numbered items, has data URI images or many images + little text
        if data['interactive'] == 0 and not data['hasNumberedItems'] and data['images'] > 0:
            return True
        if data['interactive'] == 0 and not data['hasNumberedItems'] and data.get('hasDataUriImages'):
            return True
        return False
    except Exception:
        # If the evaluation fails, assume it's not image-based
        return False


async def extract_agenda_items_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    await page.goto(source_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    html = await page.content()
    normalized_meeting = {
        "meeting_id": (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting",
        "meeting_date": (meeting.get("record_date") or meeting.get("meeting_date") or "").strip(),
        "meeting_type": (meeting.get("meeting_type") or "").strip(),
    }
    return parse_agenda_items_from_html(html, source_url, normalized_meeting)


async def extract_raw_agenda_blocks_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    await page.goto(source_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)
    return parse_raw_agenda_blocks_html(await page.content(), meeting)


async def extract_raw_agenda_blocks_from_metadata(page, meeting_rows: Optional[list[dict[str, str]]] = None) -> int:
    if meeting_rows is None:
        meeting_rows = read_agenda_metadata_rows()
    if not meeting_rows:
        print("No agenda metadata rows found for raw block extraction.")
        return 0

    existing_keys = read_existing_raw_block_keys(RAW_AGENDA_ITEMS_CSV)
    ensure_dir(RAW_AGENDA_ITEMS_CSV.parent)
    wrote = 0

    for meeting in meeting_rows:
        blocks = await extract_raw_agenda_blocks_for_meeting(page, meeting)
        for block in blocks:
            key = (block["meeting_id"], block["raw_block_index"])
            if key in existing_keys:
                continue
            write_raw_agenda_item_row(block)
            existing_keys.add(key)
            wrote += 1
    return wrote


async def extract_agenda_items_from_metadata(
    page,
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
    limit: Optional[int] = None,
) -> int:
    meeting_rows = filter_agenda_metadata_rows(
        read_agenda_metadata_rows(), start_date, end_date, limit
    )
    if not meeting_rows:
        print("No agenda metadata rows matched the selected date range/limit.")
        return 0

    existing_keys = read_existing_agenda_item_keys(AGENDA_ITEMS_CSV)
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    wrote = 0

    for meeting in meeting_rows:
        meeting_id = (meeting.get("record_id") or meeting.get("meeting_id") or "").strip() or "meeting"
        items = await extract_agenda_items_for_meeting(page, meeting)
        for item in items:
            key = (item["meeting_id"], item["agenda_item_id"])
            if key in existing_keys:
                continue
            write_agenda_item_row(item)
            existing_keys.add(key)
            wrote += 1
    return wrote


async def extract_meetings(page, search_url: str) -> list[Meeting]:
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)
    table_found = False
    try:
        await page.wait_for_selector('table:has(th:has-text("Meeting Name"))', timeout=60000)
        table_found = True
    except Exception:
        try:
            await page.wait_for_selector('text=Meeting Search Results', timeout=60000)
        except Exception:
            pass
    html = await page.content()
    table_found = table_found or _search_results_table_present(html)
    meetings = parse_search_results_html(html, search_url)

    if not table_found:
        ensure_dir(LOGS_ROOT)
        await page.screenshot(path=str(LOGS_ROOT / "debug-search-page.png"), full_page=True)
        (LOGS_ROOT / "debug-search-page.html").write_text(html, encoding="utf-8")
    return meetings


def iter_discovery_documents(meeting: Meeting):
    yield "agenda", meeting.agenda_url
    yield "summary", meeting.summary_url
    yield "minutes", meeting.minutes_url
    yield "video", meeting.video_url


def write_discovery_rows(meeting: Meeting, search_url: str, existing_keys: set[tuple[str, str]]) -> None:
    for category, url in iter_discovery_documents(meeting):
        if not url or (category, url) in existing_keys:
            continue
        write_discovery_row({
            "source_body": "Board of Supervisors",
            "document_category": category,
            "record_id": meeting.meeting_id,
            "record_date": meeting.meeting_date,
            "record_time": meeting.meeting_time,
            "record_title": meeting.meeting_title,
            "meeting_type": meeting.meeting_type,
            "source_page_url": SOURCE_PAGE,
            "document_url": url,
            "local_path": "",
            "download_status": "discovered",
            "downloaded_at": "",
            "source_search_url": search_url,
            "notes": "",
        })
        existing_keys.add((category, url))


async def count_agenda_items_for_meeting(page, meeting_url: str) -> int:
    """Visit an agenda HTML page and count the number of numbered agenda items."""
    items = await extract_agenda_item_titles(page, meeting_url)
    return len(items)


def _clean_lnk_title(text: str) -> str:
    """Decode HTML entities and collapse whitespace."""
    return _clean_html_text(text)


def _find_item_tables(html: str) -> list[tuple[int, int, int]]:
    """Find all numbered agenda items and their containing table boundaries.

    Returns: list of (item_number, bold_span_position, table_end_position)
    """
    bold_pattern = re.compile(
        r'<span[^>]*font-weight:bold[^>]*>(\d+)\.</span>'
    )
    items: list[tuple[int, int, int]] = []
    for m in bold_pattern.finditer(html):
        num = int(m.group(1))
        pos = m.start()
        tend = html.find("</table>", pos)
        if tend < 0:
            continue
        items.append((num, pos, tend))
    return items


def _extract_lnk_from_table(table_html: str, bold_offset: int) -> list[str]:
    """Extract all lnkAgendaItem titles from a table that appear after the bold span."""
    titles: list[str] = []
    for m in re.finditer(
        r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>', table_html, re.DOTALL
    ):
        if m.start() <= bold_offset:
            continue  # Before the bold span — not the item's title
        text = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        text = _clean_lnk_title(text)
        if text:
            titles.append(text)
    return titles


async def extract_agenda_item_titles(page, meeting_url: str) -> list[tuple[int, str]]:
    """Visit an agenda HTML page and extract (item_number, title) pairs.

    Finds bold numbered <span> elements and their associated titles:
    - Uses the first lnkAgendaItem anchor after the bold span in the item's
      own table as the title.
    - Subsequent lnkAgendaItems in the same table are section headings for
      the next items.
    - Items without any anchor in their table fall back to the nearest
      preceding lnkAgendaItem by position in the full HTML.
    """
    await page.goto(meeting_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    html = await page.content()

    # Find all numbered items with their table boundaries
    items = _find_item_tables(html)
    if not items:
        return []

    # Sort by display position
    items.sort(key=lambda x: x[1])

    # Build position-sorted list of all lnkAgendaItem entries
    all_lnk_positions: list[tuple[str, int]] = []
    for m in re.finditer(
        r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>', html, re.DOTALL
    ):
        text = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        text = _clean_lnk_title(text)
        if text:
            all_lnk_positions.append((text, m.start()))
    all_lnk_positions.sort(key=lambda x: x[1])

    # Track latest section heading found as extra lnk within an item's table
    pending_section = ""

    results: list[tuple[int, str, int]] = []
    for idx, (num, item_pos, tend) in enumerate(items):
        tstart = html.rfind("<table", 0, item_pos)
        if tstart < 0:
            results.append((num, pending_section, item_pos))
            continue

        table_html = html[tstart : tend + 8]
        lnk_titles = _extract_lnk_from_table(table_html, item_pos - tstart)

        if lnk_titles:
            # First title is the item's own
            title = lnk_titles[0]
            # Subsequent titles in the same table are section headings
            # for upcoming items (e.g. "CALL TO THE PUBLIC" in the
            # same table as the preceding FCD item)
            for extra_title in lnk_titles[1:]:
                pending_section = extra_title
        else:
            # No title anchor in this item's table — fall back to
            # the most recently seen section heading (from a prior
            # item's extra lnkAgendaItem, not from the TOC area).
            title = pending_section

        results.append((num, title, item_pos))

    return [(num, title) for num, title, _ in results]


def build_pz_search_url(start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for P&Z meetings."""
    params = {
        "term": "",
        "CIDs": "9,",
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"{PZ_SEARCH_BASE}?{qs}"


async def extract_pz_meetings(page, search_url: str) -> list[Meeting]:
    """Extract P&Z meetings from AgendaCenter search results."""
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    html = await page.content()
    return parse_pz_meetings_from_html(html, search_url)


def parse_pz_meetings_from_html(html: str, base_url: str) -> list[Meeting]:
    """Parse P&Z meetings from AgendaCenter search HTML.

    Structure of each meeting row:
      <tr id="row3711..." class="catAgendaRow">
        <td>
          <h3><strong>Apr 23, 2026</strong></h3>
          <p>
            <a id="04232026-3722"
               href="/AgendaCenter/ViewFile/Agenda/_04232026-3722?html=true">
              April 23, 2026 Planning and Zoning Commission Meeting ...
            </a>
          </p>
        </td>
        <td class="minutes"></td>
        <td class="media"><a href="https://youtu.be/...">...</a></td>
      </tr>
    """
    root = _parse_html(html)
    meetings: list[Meeting] = []

    # Find all catAgendaRow rows
    rows = _find_all(root, "tr")
    for row in rows:
        classes = (row.attrs.get("class") or "").split()
        if "catAgendaRow" not in classes:
            continue

        cells = _find_all(row, "td")
        if not cells:
            continue

        first_cell = cells[0]
        cell_text = _clean_html_text(_node_text(first_cell))

        # Extract meeting date from the <strong> in the <h3>
        # Format: '<strong aria-label="Agenda for April 23, 2026">
        #           <abbr title="April">Apr</abbr> 23, 2026</strong>'
        meeting_date = ""
        for h3 in _find_all(first_cell, "h3"):
            for strong in _find_all(h3, "strong"):
                # Check aria-label first (has full month name)
                aria = strong.attrs.get("aria-label", "")
                if aria:
                    dm = re.search(r"(\w+ \d{1,2},? \d{4})", aria)
                    if dm:
                        date_str = dm.group(1)
                        meeting_date = _normalize_text_date(date_str)
                        break
                # Fallback: extract from text content directly
                abbr = _find_all(strong, "abbr")
                date_text = _clean_html_text(_node_text(strong))
                dm = re.search(r"(\w{3,9})\s+(\d{1,2}),?\s+(\d{4})", date_text)
                if dm:
                    meeting_date = _normalize_text_date(f"{dm.group(1)} {dm.group(2)}, {dm.group(3)}")
                    break
            if meeting_date:
                break
        if not meeting_date:
            continue

        # Find agenda link
        agenda_url = ""
        meeting_title = ""
        for a in _find_all(first_cell, "a"):
            href = a.attrs.get("href", "")
            if "/Agenda/" in href or "ViewFile/Agenda" in href:
                agenda_url = urllib.parse.urljoin(base_url, href)
                meeting_title = _clean_html_text(_node_text(a))
                break

        if not agenda_url:
            continue

        # Note: meeting_id is extracted from agenda_url by the Meeting property
        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=meeting_title,
            meeting_type="Planning & Zoning",
            body="pz",
            row_text=_clean_html_text(_node_text(row)),
            detail_url=agenda_url,
            agenda_url=agenda_url,
        ))

    return meetings


async def extract_pz_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract real agenda items from a P&Z AgendaCenter meeting.

    The AgendaCenter overview page (ViewFile/Agenda/...) is a document index,
    NOT an agenda. Real agenda items come from the linked agenda PDF.
    Staff reports on the overview page become supporting documents.
    """
    await page.goto(meeting_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    html = await page.content()
    base_for_url = urllib.parse.urljoin(meeting_url, "/")

    # Parse overview: find agenda PDF link + staff reports
    overview = parse_pz_overview(html, meeting_url, base_for_url)

    agenda_pdf_url = ""
    staff_report_files: list[dict] = []
    if overview:
        agenda_pdf_url = overview.get("agenda_pdf_url", "")
        staff_report_files = overview.get("staff_report_files", [])

    # If overview parsing found no agenda PDF, try fallback link search
    if not agenda_pdf_url:
        # Fallback: scan the page for any link with "agenda" in href or text
        found_count = len(staff_report_files) if staff_report_files else 0
        for m in re.finditer(
            r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.I
        ):
            href = m.group(1)
            link_text = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            if re.search(r"agenda", href + " " + link_text, re.I) and "ViewFile/Item/" in href:
                agenda_pdf_url = urllib.parse.urljoin(base_for_url, href)
                break
        if not agenda_pdf_url:
            print(f"    No agenda PDF found in overview (found {found_count} staff reports)")
            return []
        else:
            print(f"    Found agenda PDF (title did not match expected pattern)")

    if not agenda_pdf_url:
        print("    No agenda PDF found on overview page")
        return []

    # Download the agenda PDF directly (not via Playwright click, which is unreliable)
    pdf_path_str = f"/tmp/pz_agenda_{Path(meeting_url).stem}.pdf"
    pdf_path = Path(pdf_path_str)
    try:
        pdf_req = urllib.request.Request(
            agenda_pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_path.write_bytes(pdf_resp.read())
        if not pdf_path.exists() or pdf_path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded file is {pdf_path.stat().st_size} bytes, too small")
    except Exception as e:
        print(f"    Failed to download agenda PDF: {e}")
        Path(pdf_path_str).unlink(missing_ok=True)
        return []

    # Parse the agenda PDF for real items
    pdf_items = parse_pz_agenda_pdf(pdf_path_str)
    pdf_path.unlink(missing_ok=True)

    if not pdf_items:
        print(f"    Agenda PDF parse returned no items — keeping existing data")
        return []

    # Build real agenda item dicts from PDF data
    items: list[dict] = []
    for pi in pdf_items:
        item_num = pi.get("agenda_item_number", 0)
        case_number = (pi.get("case_number") or "").strip()
        title = (pi.get("project_name") or f"Case {case_number}" if case_number else f"Agenda Item #{item_num}")

        item = {
            "source_body": "Planning & Zoning",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "Planning & Zoning",
            "agenda_item_number": str(item_num),
            "agenda_item_id": "",
            "agenda_item_title": title[:200],
            "agenda_item_text": f"Case: {case_number}" if case_number else "",
            "agenda_item_url": meeting_url,
            "vote_or_action": "",
            "source_url": meeting_url,
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": case_number,
            "supporting_doc_dicts": [],
            "pz_data_complete": True,
            "pz_case_number": case_number,
            "pz_district": pi.get("district"),
            "pz_project_name": pi.get("project_name"),
            "pz_applicant": pi.get("applicant"),
            "pz_request": pi.get("request"),
            "pz_location": pi.get("location"),
            "pz_recommendation": pi.get("recommendation"),
            "pz_presented_by": pi.get("presented_by"),
            "staff_report_url": None,
        }
        items.append(item)

    # Build (all_case_numbers) -> staff report file lookups
    # A file like "CPAZ250011 & Z250034 P&Z Report" applies to BOTH cases
    file_case_to_files: dict[str, list[dict]] = {}
    for srf in staff_report_files:
        # Register under ALL case numbers this file applies to
        all_cases = srf.get("all_case_numbers", [])
        primary_case = (srf.get("c_number") or "").upper()
        if primary_case and primary_case not in all_cases:
            all_cases.append(primary_case)
        if not all_cases and primary_case:
            all_cases = [primary_case]
        for cn in all_cases:
            file_case_to_files.setdefault(cn.upper(), []).append(srf)
        # Also register under the direct c_number for backward compat
        if primary_case and primary_case not in all_cases:
            file_case_to_files.setdefault(primary_case, []).append(srf)

    # Match staff report files to items by case number
    for it in items:
        it_case = (it.get("case_number") or "").upper()
        if it_case and it_case in file_case_to_files:
            for srf in file_case_to_files[it_case]:
                sd = {
                    "agenda_item_number": int(it["agenda_item_number"]),
                    "agenda_item_id": int(it["agenda_item_number"]),
                    "c_number": srf.get("c_number"),
                    "document_title": srf.get("document_title", ""),
                    "document_url": srf.get("document_url", ""),
                    "document_type": "PDF",
                    "file_name": f"{srf.get('document_title', '')}.pdf",
                    "file_extension": "pdf",
                }
                # Deduplicate by URL
                existing_urls = {d["document_url"] for d in it["supporting_doc_dicts"]}
                if sd["document_url"] not in existing_urls:
                    it["supporting_doc_dicts"].append(sd)
                if it.get("staff_report_url") is None:
                    it["staff_report_url"] = srf.get("document_url", "")

    return items


def parse_pz_overview(html: str, source_url: str, base_url: str) -> dict | None:
    """Parse a P&Z AgendaCenter overview/document-index page.

    Each h1.title section may have multiple file links (staff reports, exhibits).
    Titles like "CPAZ250011 & Z250034 P&Z Report" contain multiple case numbers.

    Returns:
      {
        "agenda_pdf_url": str | "",
        "agenda_title": str | "",
        "staff_report_files": list[dict],  # One dict per actual file
      }
    Returns None if no h1.title structure found.
    """
    from html import unescape
    base_for_url = base_url

    item_blocks: list[tuple[str, str]] = []
    for m in re.finditer(
        r'<h1[^>]*class="title"[^>]*>(.*?)</h1>\s*(.*?)(?=<h1[^>]*class="title"|\Z)',
        html, re.DOTALL | re.I
    ):
        title_html = m.group(1).strip()
        section = m.group(2).strip()
        item_blocks.append((title_html, section))

    if not item_blocks:
        return None

    agenda_pdf_url = ""
    agenda_title = ""
    staff_report_files: list[dict] = []

    for title_html, section in item_blocks:
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = unescape(re.sub(r"\s+", " ", title)).strip()
        if not title:
            continue

        if re.search(r"GoToWebinar|Webinar User Guide", title, re.I):
            continue

        # Extract ALL case numbers from the title (e.g. "CPAZ250011 & Z250034")
        title_cases: list[str] = []
        for c_m in CASE_PATTERN.finditer(title):
            cn = c_m.group(1).upper()
            if cn not in title_cases:
                title_cases.append(cn)

        # Find ALL document links in this section
        doc_urls: list[tuple[str, str]] = []  # (url, link_text)
        for a_m in re.finditer(
            r'<a[^>]*class="[^"]*file[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            section, re.DOTALL | re.I
        ):
            href = a_m.group(1)
            link_text = re.sub(r"<[^>]+>", " ", a_m.group(2))
            link_text = re.sub(r"\s+", " ", link_text).strip()
            if "/ViewFile/Item/" in href:
                doc_url = urllib.parse.urljoin(base_for_url, href)
                doc_urls.append((doc_url, link_text))

        if not doc_urls:
            continue

        # Identify the agenda document vs staff reports
        # Identify the agenda document: match by "agenda", "Planning and Zoning" (without staff report),
        # or any title that looks like a date-range descriptor (e.g. "April 23, 2026 - Planning and Zoning")
        has_no_case = not title_cases
        is_agenda = (
            (bool(re.search(r"agenda", title, re.I)) and not re.search(r"staff report", title, re.I))
            or (bool(re.search(r"planning\s+and\s+zoning", title, re.I))
                and not re.search(r"staff report", title, re.I)
                and has_no_case)
            or (has_no_case and bool(re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)", title, re.I)))
        )

        if is_agenda:
            # The agenda document is typically the first file link
            agenda_pdf_url = doc_urls[0][0]
            agenda_title = title
        else:
            # Each file link is a separate staff report document
            for doc_url, link_text in doc_urls:
                # Extract case number from this specific file
                file_case = ""
                dc = CASE_PATTERN.search(doc_url + " " + link_text)
                if dc:
                    file_case = dc.group(1).upper()
                
                # Also try to extract from the section title
                doc_case = file_case or (title_cases[0] if title_cases else "")
                
                staff_report_files.append({
                    "document_title": link_text.replace(".pdf", "").replace(".PDF", "").strip() or title,
                    "document_url": doc_url,
                    "c_number": doc_case if doc_case else None,
                    "all_case_numbers": title_cases.copy(),
                })

    return {
        "agenda_pdf_url": agenda_pdf_url,
        "agenda_title": agenda_title,
        "staff_report_files": staff_report_files,
    }

def _format_mm_dd_yyyy(date_iso: str) -> str | None:
    """Convert YYYY-MM-DD to MM/DD/YYYY. Returns None if input is empty."""
    if not date_iso:
        return None
    try:
        d = dt.date.fromisoformat(date_iso)
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    except (ValueError, TypeError):
        # Already in MM/DD/YYYY? Return as-is.
        if re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_iso):
            return date_iso
        return date_iso



def parse_pz_agenda_pdf(filepath: str) -> list[dict]:
    """Parse a P&Z Agenda PDF and extract structured field data per item."""
    import subprocess
    import tempfile

    if not filepath or not Path(filepath).exists():
        return []

    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            txt_path = f.name
        subprocess.run(
            ["pdftotext", "-layout", filepath, txt_path],
            capture_output=True, timeout=30,
        )
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        Path(txt_path).unlink(missing_ok=True)
    except Exception:
        return []

    lines = text.split("\n")
    items: list[dict] = []
    current: dict | None = None

    FIELD_PATTERNS = {
        "case_number": re.compile(r"^\s*\d*\.?\s*Case\s*:?\s*(.+?)\s{2,}|^\s*\d*\.?\s*Case\s{2,}(.+?)\s{2,}"),
        "district": re.compile(r"District\s+(\d+)"),
        "project_name": re.compile(r"Project\s+name\s*:?\s*(.*)"),
        "applicant": re.compile(r"Applicant\s*:?\s*(.*)"),
        "request": re.compile(r"Request\s*:?\s*(.*)"),
        "location": re.compile(r"Location\s*:?\s*(.*)"),
        "recommendation": re.compile(r"Recommendation\s*:?\s*(.*)"),
        "presented_by": re.compile(r"Presented\s+by\s*:?\s*(.*)"),
    }

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if re.match(r"^\s*(Continuance|Consent|Regular)\s+Agenda\s*$", stripped, re.I):
            continue

        # Match items starting with "N. Case: ..." or "N. CASENUMBER ..." (ZIPPOR format)
        item_start = re.match(r"^\s*(\d+)\.?\s*Case\b", stripped, re.I)
        if not item_start:
            item_start = re.match(r"^\s*(\d+)\.\s+([A-Z]+-?\d{3,})", stripped)
        if item_start:
            if current:
                items.append(current)

            item_num = item_start.group(1)
            current = {
                "agenda_item_number": int(item_num), "case_number": "", "district": None,
                "project_name": None, "applicant": None, "request": None,
                "location": None, "recommendation": None, "presented_by": None,
            }
            rest = stripped[item_start.end():].strip()
            # For "N. CASENUMBER" format (ZIPPOR), the case number is in group(2)
            # and was consumed by the regex — prepend it back to rest
            if len(item_start.groups()) >= 2:
                rest = (item_start.group(2) + " " + rest).strip()
            case_m = re.search(r"([A-Z]+-?\d{3,})", rest)
            if case_m:
                current["case_number"] = case_m.group(1)
            dist_m = FIELD_PATTERNS["district"].search(rest)
            if dist_m:
                current["district"] = f"District {dist_m.group(1)}"
            continue

        if current is None:
            continue

        for field, pattern in FIELD_PATTERNS.items():
            if current.get(field):
                continue
            m = pattern.search(stripped)
            if m:
                val = (m.group(1) or m.group(2) or "").strip()
                if val:
                    current[field] = val
                break

    if current:
        items.append(current)

    return items

async def main() -> int:
    args = parse_args()

    # Backward compatibility: --sync-pz with legacy --pz-* flags
    if getattr(args, 'sync_pz', False):
        print("WARNING: --sync-pz is deprecated. Use: pz --sync", file=sys.stderr)
        args.source = "pz"
        # Map legacy PZ flags to the new unified args
        if getattr(args, 'pz_start_date', None):
            args.start_date = args.pz_start_date
        if getattr(args, 'pz_end_date', None):
            args.end_date = args.pz_end_date
        if getattr(args, 'pz_limit', None) is not None:
            args.limit = args.pz_limit
        args.sync = True

    if getattr(args, 'self_test_splitter', False):
        return 0 if splitter_self_test(verbose=True) else 1

    if args.init_db:
        from db import init_db

        init_db()
        print("Database tables created.")
        return 0

    if args.source == "pz" and args.sync:
        from db import get_session, init_db, replace_meeting_data_safe

        init_db()

        # If --meeting-id is provided, bypass search and use direct meeting URL
        if args.meeting_id:
            meeting_id = args.meeting_id
            # Try to get the agenda URL from the database first
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            db_session = get_session()
            existing = db_session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == "pz",
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            agenda_url = ""
            meeting_date = ""
            meeting_title = ""
            if existing and existing.source_url:
                # Use the HTML agenda URL from previous sync
                agenda_url = existing.source_url
                meeting_date = existing.meeting_date or ""
                meeting_title = existing.meeting_title or ""
                print(f"Found existing P&Z meeting {meeting_id} in database")
            else:
                # Construct a best-guess URL. Agenda Center HTML pages use
                # ViewFile/Agenda/<id>?html=true
                agenda_url = f"https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/{meeting_id}?html=true"
            db_session.close()

            pz_meetings = [Meeting(
                meeting_date=meeting_date,
                meeting_time="",
                meeting_title=meeting_title,
                meeting_type="Planning & Zoning",
                body="pz",
                row_text="",
                detail_url="",
                agenda_url=agenda_url,
            )]
            print(f"Syncing P&Z meeting {meeting_id}...")
        else:
            now = dt.date.today()
            if args.start_date:
                pz_start = _format_mm_dd_yyyy(args.start_date) or f"{now.month:02d}/01/{now.year}"
            else:
                three_months_ago = now - dt.timedelta(days=90)
                pz_start = f"{three_months_ago.month:02d}/01/{three_months_ago.year}"

            if args.end_date:
                pz_end = _format_mm_dd_yyyy(args.end_date) or f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"
            else:
                pz_end = f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"

            search_url = build_pz_search_url(pz_start, pz_end)
            print(f"P&Z search URL: {search_url}")

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.meeting_id:
                    # Skip search when --meeting-id is provided; list already built above
                    pass
                else:
                    pz_meetings = await extract_pz_meetings(page, search_url)

                    if not pz_meetings:
                        print(f"No P&Z meetings found.")
                        return 0

                if args.limit:
                    pz_meetings = pz_meetings[:args.limit]

                print(f"Found {len(pz_meetings)} P&Z meeting(s)")

                session = get_session()
                total = 0

                for meeting in pz_meetings:
                    meeting_dict = {
                        "meeting_id": meeting.meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": "Planning & Zoning",
                        "meeting_title": meeting.meeting_title,
                        "source_url": meeting.agenda_url,
                    }

                    items = await extract_pz_agenda_items(page, meeting.agenda_url)

                    # extract_pz_agenda_items now returns real items from the PDF
                    # with supporting_doc_dicts already set. We just need to fix
                    # meeting_id and agenda_item_id, then flatten supporting docs.
                    docs: list[dict] = []
                    for it in items:
                        it["meeting_id"] = meeting.meeting_id
                        it["agenda_item_id"] = f"{meeting.meeting_id}-{it['agenda_item_number']}-item"
                        for sd in it.pop("supporting_doc_dicts", []):
                            sd["meeting_id"] = meeting.meeting_id
                            sd["agenda_item_number"] = int(it["agenda_item_number"])
                            sd["agenda_item_id"] = int(it["agenda_item_number"])
                            docs.append(sd)

                    if items:
                        replace_meeting_data_safe(
                            session, meeting.body, meeting.meeting_id, meeting_dict, items,
                            supporting_doc_dicts=docs,
                        )

                        # Persist PZ item details to database
                        from db import PZItemDetail, AgendaItem
                        from sqlalchemy import select
                        try:
                            # Delete old details for this meeting
                            session.execute(
                                PZItemDetail.__table__.delete().where(
                                    PZItemDetail.body == meeting.body,
                                    PZItemDetail.meeting_id == meeting.meeting_id,
                                )
                            )
                            # Look up agenda item DB IDs after persist
                            db_items = {
                                row.agenda_item_number: row.id
                                for row in session.execute(
                                    select(AgendaItem.id, AgendaItem.agenda_item_number)
                                    .where(
                                        AgendaItem.body == meeting.body,
                                        AgendaItem.meeting_id == meeting.meeting_id,
                                    )
                                ).all()
                            }
                            # Insert new details
                            for it in items:
                                if it.get("pz_project_name"):
                                    item_num = int(it.get("agenda_item_number", 0))
                                    detail = PZItemDetail(
                                        body=meeting.body,
                                        agenda_item_id=db_items.get(item_num),
                                        meeting_id=meeting.meeting_id,
                                        agenda_item_number=item_num,
                                        case_number=it.get("case_number", ""),
                                        district=it.get("pz_district"),
                                        project_name=it.get("pz_project_name"),
                                        applicant=it.get("pz_applicant"),
                                        request=it.get("pz_request"),
                                        location=it.get("pz_location"),
                                        recommendation=it.get("pz_recommendation"),
                                        presented_by=it.get("pz_presented_by"),
                                        staff_report_url=it.get("staff_report_url"),
                                    )
                                    session.add(detail)
                            session.commit()
                        except Exception as pz_err:
                            print(f"    PZ detail persist skipped: {pz_err}")
                            session.rollback()

                        total += len(items)
                        doc_summary = f", {len(docs)} doc(s)" if docs else ""
                        print(f"  {meeting.meeting_id} {meeting.meeting_date}: {len(items)} item(s){doc_summary}")

                session.close()
                print(f"Synced {total} P&Z agenda items across {len(pz_meetings)} meeting(s)")
            finally:
                await browser.close()
        return 0

    if args.status:
        from db import get_session, get_sync_status_summary
        session = get_session()
        summary = get_sync_status_summary(session)
        session.close()
        print(f"{'Status':<14}  {'Count':>6}")
        print(f"{'─' * 14}  {'─' * 6}")
        for status in ["complete", "partial", "manual_review", "failed", "pending"]:
            print(f"{status:<14}  {summary.get(status, 0):>6}")
        print(f"{'─' * 14}  {'─' * 6}")
        print(f"{'Total':<14}  {summary['total']:>6}")
        print(f"\nItems: {summary['total_items']}  Supporting docs: {summary['total_docs']}")
        return 0

    if args.failed:
        from db import get_session, get_failed_meetings, get_meetings_by_status
        session = get_session()
        failed_statuses = ["failed", "partial", "manual_review"] if args.include_manual_review else ["failed", "partial"]
        body_filter = args.source if hasattr(args, 'source') else "bos"
        meetings = get_meetings_by_status(session, body_filter, failed_statuses)
        session.close()
        if not meetings:
            print("No meetings with issues.")
            return 0
        print(f"{'ID':>6}  {'Date':<12}  {'Status':<12}  {'Retries':>7}  {'Error'}")
        print(f"{'─' * 6}  {'─' * 12}  {'─' * 12}  {'─' * 7}  {'─' * 40}")
        for m in meetings:
            err = (m.last_error or "")[:60]
            print(f"{m.meeting_id:>6}  {m.meeting_date:<12}  {m.sync_status:<12}  {m.retry_count:>7}  {err}")
        return 0

    if args.persist:
        from db import get_session, persist_meeting
        import csv

        csv_path = AGENDA_ITEMS_CSV
        if not csv_path.exists():
            print(f"No agenda items CSV found at {csv_path}. Run --extract-agenda-items first.")
            return 1

        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            print("CSV is empty.")
            return 1

        # Group by meeting_id + meeting_date
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            key = (row.get("meeting_id", ""), row.get("meeting_date", ""))
            groups.setdefault(key, []).append(row)

        session = get_session()
        total = 0
        errors = 0
        for (meeting_id, meeting_date), items in sorted(groups.items()):
            first = items[0]
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": first.get("meeting_type", ""),
                "meeting_title": first.get("agenda_item_section", "") or meeting_id,
                "source_url": first.get("source_url", ""),
            }
            try:
                body = first.get("body", "bos") if "body" in first else args.source if hasattr(args, 'source') else "bos"
                count = persist_meeting(session, body, meeting_id, items)
                total += count
                print(f"  {meeting_id} {meeting_date}: {count} items")
            except Exception as e:
                errors += 1
                print(f"  {meeting_id} {meeting_date}: FAILED - {e}")

        session.close()
        print(f"Persisted {total} agenda items across {len(groups)} meeting(s)")
        if errors:
            print(f"{errors} meeting(s) had errors")
            return 1
        return 0

    if args.sync:
        from db import get_session, init_db, persist_meeting

        init_db()

        if not args.from_file and args.meeting_id and not args.offline:
            meeting_id = args.meeting_id
            source_url = (
                "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                f"?id={meeting_id}&doctype=1"
            )
            print(f"Syncing meeting {meeting_id}...")
            print(f"  Agenda URL: {source_url}")

            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": "",
                "meeting_type": "",
                "meeting_title": "",
                "source_url": source_url,
            }
            extract_meeting = {
                "document_url": source_url,
                "agenda_url": source_url,
                "record_id": meeting_id,
                "meeting_id": meeting_id,
                "record_date": "",
                "meeting_date": "",
                "meeting_type": "",
            }

            from db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe

            session = get_session()
            try:
                # Ensure meeting row exists
                meeting = create_or_get_meeting(session, args.source, meeting_dict)
                session.commit()

                # Check if we should skip complete
                if args.skip_complete and meeting.sync_status == "complete":
                    print(f"  {meeting_id}: status=complete, skipping (use --force to re-sync)")
                    session.close()
                    return 0

                async_playwright = get_async_playwright()
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=not args.headed)
                    page = await browser.new_page()
                    page.set_default_timeout(60000)
                    try:
                        # Mark as attempted
                        update_sync_status(session, args.source, meeting_id, meeting.sync_status)
                        session.commit()

                        retry = args.retry_count

                        # Extract agenda items with retry
                        async def do_extract_items():
                            return await extract_agenda_items_for_meeting(page, extract_meeting)

                        items = await retry_with_backoff(
                            lambda: do_extract_items(),
                            max_attempts=retry,
                            label=f"items {meeting_id}",
                        )

                        # After page loads, extract meeting metadata from the page
                        # and update the meeting_dict/extract_meeting with real values
                        page_meta = await extract_meeting_metadata_from_page(page, source_url)
                        if page_meta.get("meeting_date"):
                            meeting_dict["meeting_date"] = page_meta["meeting_date"]
                            extract_meeting["meeting_date"] = page_meta["meeting_date"]
                            extract_meeting["record_date"] = page_meta["meeting_date"]
                        if page_meta.get("meeting_type"):
                            meeting_dict["meeting_type"] = page_meta["meeting_type"]
                            extract_meeting["meeting_type"] = page_meta["meeting_type"]
                        if page_meta.get("meeting_title"):
                            meeting_dict["meeting_title"] = page_meta["meeting_title"]
                        if not items:
                            # Check if page is image-based (unparseable but reachable)
                            if await is_image_based_agenda(page):
                                status = "manual_review"
                                error = "Unsupported agenda format: page loaded but no parseable agenda items found; possible image/scanned agenda"
                                print(f"  {meeting_id}: 0 items (image/scanned agenda - manual review)")
                            else:
                                status = "failed"
                                error = "No agenda items found on page"
                                print(f"  {meeting_id}: 0 items (no agenda items found)")
                            update_sync_status(
                                session, args.source, meeting_id, status,
                                error=error,
                            )
                            session.commit()
                            return 1

                        # Extract supporting documents with retry
                        async def do_extract_docs():
                            return await extract_supporting_documents_dynamic(page, items, source_url)

                        docs = []
                        docs_ok = True
                        try:
                            docs = await retry_with_backoff(
                                lambda: do_extract_docs(),
                                max_attempts=retry,
                                label=f"docs {meeting_id}",
                            )
                        except Exception as e:
                            docs_ok = False
                            print(f"  {meeting_id}: supporting doc extraction failed: {e}")

                        if not docs_ok:
                            # Items succeeded but docs failed - partial
                            replace_meeting_data_safe(
                                session, args.source, meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            update_sync_status(
                                session, args.source, meeting_id, "partial",
                                item_count_expected=len(items),
                                item_count_actual=len(items),
                                supporting_doc_count=len(docs),
                                items_extracted=True,
                                supporting_docs_extracted=False,
                                error="Supporting document extraction failed",
                            )
                            session.commit()
                            print(f"  {meeting_id}: {len(items)} items synced (partial - no supporting docs)")
                        else:
                            replace_meeting_data_safe(
                                session, args.source, meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            session.commit()
                            print(f"  {meeting_id}: {len(items)} items, {len(docs)} supporting docs synced")

                        # Extract and persist votes from the summary page
                        try:
                            summary_url = source_url.replace("doctype=1", "doctype=3")
                            from db import persist_votes
                            vote_items = [
                                {"agenda_item_number": it.get("agenda_item_number", ""),
                                 "c_number": it.get("c_number", "")}
                                for it in items
                            ]
                            supervisors, votes = await extract_votes_from_summary(
                                page, summary_url, vote_items
                            )
                            if votes:
                                vote_count = persist_votes(session, args.source, meeting_id, supervisors, votes)
                                session.commit()
                                print(f"  {meeting_id}: {vote_count} vote(s) synced")
                            elif supervisors:
                                # Supervisors found but no item votes (display-only meeting)
                                vote_count = persist_votes(session, args.source, meeting_id, supervisors, votes)
                                session.commit()
                                print(f"  {meeting_id}: {len(supervisors)} supervisor(s) present, no item votes")
                        except Exception as ve:
                            # Vote extraction is non-critical — don't fail the sync
                            print(f"  {meeting_id}: vote extraction skipped ({ve})")

                    except Exception as e:
                        # Items extraction failed
                        update_sync_status(
                            session, args.source, meeting_id, "failed",
                            error=str(e)[:500],
                        )
                        session.commit()
                        print(f"  {meeting_id}: FAILED - {e}")
                        return 1
                    finally:
                        await browser.close()
            except Exception as e:
                session.rollback()
                raise
            finally:
                session.close()
            return 0

        if args.offline and args.meeting_id:
            # Offline: auto-discover HTML file by meeting ID
            meeting_id = args.meeting_id
            search_dirs = [
                ROOT / "data" / "agenda-html",
                ROOT / "tests" / "fixtures",
                ROOT / "tests" / "fixtures" / "agendas",
                ROOT,
            ]
            found = None
            for d in search_dirs:
                if not d.exists():
                    continue
                for pattern in [f"*{meeting_id}*.html", f"*{meeting_id}*.htm"]:
                    candidates = list(d.glob(pattern))
                    if candidates:
                        found = candidates[0]
                        break
                if found:
                    break

            if not found:
                raise SystemExit(
                    f"No HTML file found for meeting {meeting_id}. "
                    "Save the agenda HTML to one of:"
                    f"\n  - data/agenda-html/{meeting_id}.html"
                    f"\n  - tests/fixtures/"
                )

            print(f"Offline sync: using {found}")
            # Delegate to the --from-file handler by rewriting args
            args.from_file = str(found)
            # Fall through to the from-file block below

        if args.from_file:
            # Sync from a local HTML file — no server needed
            # Check this before --meeting-id so --from-file takes priority
            # when both flags are given
            fixture_path = Path(args.from_file)
            if not fixture_path.exists():
                raise SystemExit(f"File not found: {fixture_path}")

            # Parse meeting metadata from filename
            # Supports:
            #   {date}_{type}_{id}_agenda.html  (e.g. 2025-01-29_formal_4449_agenda.html)
            #   {id}_{type}_{date}.html         (e.g. 4667_formal_2026-04-22.html)
            fn_match = re.match(
                r"(\d{4}-\d{2}-\d{2})_(.+?)_(\d+)_agenda\.html"
                r"|(\d+)_(.+?)_(\d{4}-\d{2}-\d{2})\.html",
                fixture_path.name,
            )
            if fn_match:
                groups = fn_match.groups()
                if groups[0]:  # date_type_id_agenda pattern
                    meeting_date = groups[0]
                    meeting_type = groups[1].replace("_", " ").title()
                    meeting_id = groups[2]
                else:  # id_type_date pattern
                    meeting_id = groups[3]
                    meeting_type = groups[4].replace("_", " ").title()
                    meeting_date = groups[5]
            elif args.meeting_id:
                meeting_id = args.meeting_id
                meeting_date = ""
                meeting_type = ""
            else:
                raise SystemExit(
                    f"Could not parse meeting info from filename '{fixture_path.name}'. "
                    "Use --meeting-id to specify the meeting ID, "
                    "or rename the file to: YYYY-MM-DD_type_ID_agenda.html"
                )

            html = fixture_path.read_text(encoding="utf-8")
            source_url = (
                "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                f"?id={meeting_id}&doctype=1"
            )

            meeting = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
            }
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": f"Meeting {meeting_id}",
                "source_url": source_url,
            }

            items = parse_agenda_items_from_html(html, source_url, meeting)
            if not items:
                print(f"  {meeting_id}: 0 items (no agenda items found in file)")
                return 1

            # Extract supporting documents from the HTML
            docs = extract_supporting_documents_from_items(html, items, source_url)
            if docs:
                print(f"  {meeting_id}: {len(docs)} supporting document(s) found")

            session = get_session()
            count = persist_meeting(session, args.source, meeting_id, items, supporting_doc_dicts=docs)
            session.close()
            print(f"  {meeting_id} {meeting_date}: {count} items synced from '{fixture_path.name}'")
            return 0

        if not args.start_date or not args.end_date:
            raise SystemExit("--start-date and --end-date (or --date) are required for --sync, or use --meeting-id")
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
        if end_date < start_date:
            raise SystemExit("--end-date must be on or after --start-date")

        search_url = build_search_url(start_date, end_date)
        print(f"Agenda Online search URL: {search_url}")

        from db import get_session, init_db, persist_meeting, get_meetings_by_date_range

        init_db()
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
                await page.goto(search_url, wait_until="domcontentloaded")
                # Search for meetings in the date range
                search_meetings = await extract_meetings(page, search_url)

                from db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe

                session = get_session()

                # When --force is used, also pull known meetings from DB to
                # compensate for search pagination (Agenda Online only returns
                # one page of results per request)
                if args.force:
                    db_meetings = get_meetings_by_date_range(
                        session,
                        args.source,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    )
                    # Merge: search results get priority for metadata (dates, types),
                    # DB entries fill in gaps for meetings the search missed
                    seen_ids: set[str] = set()
                    merged: list[Meeting] = []
                    # Add search results first (they have richer metadata)
                    for m in search_meetings:
                        if m.meeting_id not in seen_ids:
                            seen_ids.add(m.meeting_id)
                            merged.append(m)
                    for db_m in db_meetings:
                        mid = db_m.meeting_id
                        if mid not in seen_ids:
                            seen_ids.add(mid)
                            # Create a Meeting-like object from DB row
                            merged.append(Meeting(
                                meeting_date=db_m.meeting_date,
                                meeting_time="",
                                meeting_title=db_m.meeting_title,
                                meeting_type=db_m.meeting_type,
                                body=db_m.body if hasattr(db_m, 'body') and db_m.body else "bos",
                                row_text="",
                                detail_url="",
                                agenda_url=db_m.source_url,
                            ))
                    meetings = merged
                else:
                    meetings = search_meetings

                if not meetings:
                    print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                    return 0

                if args.limit is not None:
                    meetings = meetings[: args.limit]
                total = 0
                errors = 0
                skipped = 0
                for meeting in meetings:
                    meeting_dict = {
                        "meeting_id": meeting.meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                        "meeting_title": meeting.meeting_title,
                        "source_url": meeting.agenda_url,
                    }

                    extract_meeting = {
                        "document_url": meeting.agenda_url,
                        "agenda_url": meeting.agenda_url,
                        "record_id": meeting.meeting_id,
                        "meeting_id": meeting.meeting_id,
                        "record_date": meeting.meeting_date,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                    }

                    # Ensure meeting row exists
                    db_meeting = create_or_get_meeting(session, args.source, meeting_dict)
                    session.commit()

                    # Determine which statuses to retry
                    retry_statuses = ["failed", "partial", "pending"]
                    if args.include_manual_review:
                        retry_statuses.append("manual_review")
                    # When --retry-failed is used, only process retry_statuses
                    # When --force is used, process everything
                    # When neither, skip complete
                    if not args.force and args.retry_failed:
                        if db_meeting.sync_status not in retry_statuses:
                            skipped += 1
                            continue
                    elif not args.force and db_meeting.sync_status == "complete":
                        skipped += 1
                        continue
                    elif args.retry_failed and db_meeting.sync_status not in retry_statuses:
                        skipped += 1
                        continue

                    try:
                        items = await extract_agenda_items_for_meeting(page, extract_meeting)
                        if not items:
                            if await is_image_based_agenda(page):
                                status = "manual_review"
                                error = "Unsupported agenda format: page loaded but no parseable agenda items found; possible image/scanned agenda"
                                print(f"  {meeting.meeting_id} {meeting.meeting_date}: 0 items (image/scanned - manual review)")
                            else:
                                status = "failed"
                                error = "No agenda items found"
                                print(f"  {meeting.meeting_id} {meeting.meeting_date}: 0 items ({status})")
                            update_sync_status(
                                session, args.source, meeting.meeting_id, status,
                                error=error,
                            )
                            session.commit()
                            if status == "failed":
                                errors += 1
                            continue

                        docs = await extract_supporting_documents_dynamic(
                            page, items, meeting.agenda_url
                        )

                        status_line = f"  {meeting.meeting_id} {meeting.meeting_date}: {len(items)} items, {len(docs)} supporting doc(s)"

                        try:
                            replace_meeting_data_safe(
                                session, args.source, meeting.meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            total += len(items)
                            print(f"{status_line}")
                        except Exception as e:
                            update_sync_status(
                                session, args.source, meeting.meeting_id, "failed",
                                error=str(e)[:500],
                            )
                            session.commit()
                            print(f"{status_line}: FAILED - {e}")
                            errors += 1
                    except Exception as e:
                        # Pre-extraction failure
                        update_sync_status(
                            session, args.source, meeting.meeting_id, "failed",
                            error=str(e)[:500],
                        )
                        session.commit()
                        print(f"  {meeting.meeting_id} {meeting.meeting_date}: FAILED - {e}")
                        errors += 1

                session.close()
                print(f"Synced {total} agenda items across {len(meetings)} meeting(s)")
                if skipped:
                    print(f"{skipped} meeting(s) skipped (status=complete), use --force to re-sync")
                if errors:
                    print(f"{errors} meeting(s) had errors")
                    return 1
            finally:
                await browser.close()
        return 0

    if args.sync_votes:
        from db import get_session, init_db, persist_votes

        init_db()

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.meeting_id:
                    meeting_id = args.meeting_id
                    summary_url = (
                        "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                        f"?id={meeting_id}&doctype=3"
                    )
                    print(f"Extracting votes from meeting {meeting_id}...")
                    print(f"  Summary URL: {summary_url}")

                    # Load agenda items for C-number matching
                    from db import Meeting as MeetingModel, AgendaItem
                    from sqlalchemy import select
                    session = get_session()
                    body = args.source if hasattr(args, 'source') else "bos"
                    try:
                        meeting = session.execute(
                            select(MeetingModel).where(
                                MeetingModel.body == body,
                                MeetingModel.meeting_id == meeting_id,
                            )
                        ).scalar_one_or_none()
                        if not meeting:
                            print(f"  {body} meeting {meeting_id} not found in database. Run --sync first.")
                            return 1
                        db_items = session.execute(
                            select(AgendaItem)
                            .where(
                                AgendaItem.body == body,
                                AgendaItem.meeting_id == meeting_id,
                            )
                            .order_by(AgendaItem.agenda_item_number)
                        ).scalars().all()
                        agenda_items = [
                            {
                                "agenda_item_number": str(it.agenda_item_number),
                                "c_number": it.c_number or "",
                            }
                            for it in db_items
                        ]
                        print(f"  Found {len(agenda_items)} agenda items for C-number matching")
                    except Exception as e:
                        print(f"  WARNING: Could not load agenda items: {e}")
                        agenda_items = []
                    finally:
                        session.close()

                    supervisors, votes = await extract_votes_from_summary(
                        page, summary_url, agenda_items
                    )

                    if not votes:
                        print(f"  No vote results found in summary for meeting {meeting_id}")
                        if supervisors:
                            print(f"  Found {len(supervisors)} supervisors present")
                        return 0

                    print(f"  Found {len(supervisors)} supervisor(s)")
                    for sup in supervisors:
                        district_str = f", District {sup['district']}" if sup.get('district') else ""
                        role_str = f" ({sup['role']})" if sup.get('role') else ""
                        print(f"    {sup['name']}{district_str}{role_str}")

                    print(f"  Found {len(votes)} item(s) with votes")
                    for v in votes:
                        c_str = f" ({v.get('c_number', '')})" if v.get('c_number') else ""
                        sv_summary = ", ".join(
                            f"{sv['name']}: {sv['vote']}"
                            for sv in v.get("supervisor_votes", [])
                        )
                        print(f"    #{v['agenda_item_number']}{c_str}: {v.get('motion_result', 'unknown')}")
                        if sv_summary:
                            print(f"      {sv_summary}")

                    # Persist to database
                    session = get_session()
                    try:
                        vote_count = persist_votes(session, body, meeting_id, supervisors, votes)
                        print(f"  Persisted {vote_count} vote record(s)")
                    finally:
                        session.close()
                else:
                    print("--sync-votes requires --meeting-id to specify a meeting")
                    return 1
            finally:
                await browser.close()
        return 0

    if args.count_agenda_items or args.list_agenda_items:
        if not args.start_date or not args.end_date:
            raise SystemExit("--start-date and --end-date are required")
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
        if end_date < start_date:
            raise SystemExit("--end-date must be on or after --start-date")

        search_url = build_search_url(start_date, end_date)
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
                await page.goto(search_url, wait_until="domcontentloaded")
                meetings = await extract_meetings(page, search_url)

                if not meetings:
                    print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                    return 0

                if args.limit is not None:
                    meetings = meetings[: args.limit]

                if args.count_agenda_items:
                    print(f"{'ID':>6}  {'Date':<12}  {'Count':>5}  {'Title'}")
                    print(f"{'------':>6}  {'------------':<12}  {'-----':>5}  {'-----'}")
                    total = 0
                    for meeting in meetings:
                        count = await count_agenda_items_for_meeting(page, meeting.agenda_url)
                        total += count
                        print(f"{meeting.meeting_id:>6}  {meeting.meeting_date:<12}  {count:>5}  {meeting.meeting_title}")
                    print()
                    print(f"{len(meetings)} meeting(s), {total} total items")
                else:
                    for meeting in meetings:
                        items = await extract_agenda_item_titles(page, meeting.agenda_url)
                        print()
                        print(f"{'=' * 70}")
                        print(f"{meeting.meeting_id}  {meeting.meeting_date}  {meeting.meeting_type}  {meeting.meeting_title}")
                        print(f"{len(items)} items")
                        print(f"{'=' * 70}")
                        for num, title in items:
                            print(f"  {num:>4}.  {title}")
                    print()
                    print(f"{len(meetings)} meeting(s)")
            finally:
                await browser.close()
        return 0

    if args.extract_agenda_items:
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.debug_agenda_html:
                    meeting_rows = read_agenda_metadata_rows()
                    if meeting_rows:
                        await page.goto((meeting_rows[0].get("document_url") or meeting_rows[0].get("agenda_url") or "").strip(), wait_until="domcontentloaded")
                        await page.wait_for_timeout(1000)
                        await write_agenda_debug_files(page, meeting_rows[0])
                start_date = parse_date(args.start_date) if args.start_date else None
                end_date = parse_date(args.end_date) if args.end_date else None
                wrote = await extract_agenda_items_from_metadata(
                    page, start_date=start_date, end_date=end_date, limit=args.limit
                )
                print(f"Extracted {wrote} agenda item row(s)")
            finally:
                await browser.close()
        return 0

    if args.extract_raw_agenda_blocks:
        start_date = parse_date(args.start_date) if args.start_date else None
        end_date = parse_date(args.end_date) if args.end_date else None
        meeting_rows = filter_agenda_metadata_rows(read_agenda_metadata_rows(), start_date, end_date, args.limit)
        ensure_dir(AGENDA_ITEMS_ROOT)
        if not RAW_AGENDA_ITEMS_CSV.exists():
            RAW_AGENDA_ITEMS_CSV.write_text(
                "source_body,meeting_id,meeting_date,meeting_type,raw_block_index,raw_text,source_url\n",
                encoding="utf-8",
            )
        if not meeting_rows:
            print("No agenda metadata rows matched the selected date range/limit.")
            return 0
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                wrote_raw = await extract_raw_agenda_blocks_from_metadata(page, meeting_rows)
                if wrote_raw == 0:
                    print("No raw agenda blocks were extracted for the selected meeting(s).")
                    print("Possible causes: no matching agenda rows, selector mismatch, or the agenda HTML layout changed.")
                    return 0
                wrote_structured = split_raw_agenda_blocks_to_structured()
                print(f"Extracted {wrote_raw} raw agenda block row(s)")
                print(f"Extracted {wrote_structured} structured agenda item row(s)")
                if wrote_structured == 0:
                    print("All raw blocks were rejected; see data/agenda-items/rejected_raw_blocks.csv")
            finally:
                await browser.close()
        return 0

    if args.split_raw_agenda_blocks:
        wrote = split_raw_agenda_blocks_to_structured()
        print(f"Extracted {wrote} structured agenda item row(s)")
        return 0

    if not args.start_date or not args.end_date:
        raise SystemExit("--start-date and --end-date are required unless --extract-agenda-items, --extract-raw-agenda-blocks, --split-raw-agenda-blocks, or --sync is used")

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if end_date < start_date:
        raise SystemExit("--end-date must be on or after --start-date")

    search_url = build_search_url(start_date, end_date)
    existing = read_existing_rows()
    existing_agenda_urls = read_existing_agenda_urls([DISCOVERY_CSV, *AGENDAS_ROOT.rglob("metadata.csv")])
    existing_discovery_keys = read_existing_discovery_keys(DISCOVERY_CSV)

    ensure_dir(AGENDAS_ROOT)
    ensure_dir(SUPPORT_ROOT)
    ensure_dir(AGENDA_ITEMS_ROOT)
    ensure_dir(DISCOVERY_CSV.parent)

    print(f"Agenda Online search URL: {search_url}")

    async_playwright = get_async_playwright()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        page = await browser.new_page()
        page.set_default_timeout(60000)
        try:
            await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
            await page.goto(search_url, wait_until="domcontentloaded")
            meetings = await extract_meetings(page, search_url)
            print(f"Detected {len(meetings)} meeting row(s)")

            if not meetings:
                print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                return 0

            processed = 0
            for meeting in meetings:
                if args.limit is not None and processed >= args.limit:
                    break
                processed += 1

                agenda_month_dir = month_dir_for_date(meeting.meeting_date, AGENDAS_ROOT)
                support_month_dir = month_dir_for_date(meeting.meeting_date, SUPPORT_ROOT)
                ensure_dir(agenda_month_dir)
                ensure_dir(support_month_dir)

                existing_row = existing.get(meeting.agenda_url)
                if args.download and existing_row and row_paths_present(existing_row):
                    continue

                time_part = f"{slugify(meeting.meeting_time)}_" if meeting.meeting_time else ""
                prefix = f"{meeting.meeting_date}_{time_part}{slugify(meeting.meeting_type)}_{meeting.meeting_id}"
                agenda_path = agenda_month_dir / f"{prefix}_agenda.pdf"
                supporting_paths: list[str] = []

                if not args.download:
                    print(f"{meeting.meeting_date} | {meeting.meeting_title} | {meeting.meeting_type}")
                    print(f"  agenda_url: {meeting.agenda_url}")
                    print(f"  summary_url: {meeting.summary_url or 'none'}")
                    print(f"  minutes_url: {meeting.minutes_url or 'none'}")
                    print(f"  video_url: {meeting.video_url or 'none'}")
                    print("  supporting_materials_url: none")

                if args.download:
                    if not agenda_path.exists():
                        agenda_path, _ = download_url(meeting.agenda_url, agenda_path)
                else:
                    supporting_paths = []

                row = {
                    "source_body": "Board of Supervisors",
                    "document_category": "agenda",
                    "record_id": meeting.meeting_id,
                    "record_date": meeting.meeting_date,
                    "record_time": meeting.meeting_time,
                    "record_title": meeting.meeting_title,
                    "meeting_type": meeting.meeting_type,
                    "source_page_url": SOURCE_PAGE,
                    "document_url": meeting.agenda_url,
                    "local_path": str(agenda_path.relative_to(ROOT)),
                    "download_status": "downloaded",
                    "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "source_search_url": search_url,
                    "notes": "",
                }

                if args.download and meeting.agenda_url not in existing:
                    write_download_row(row)
                    existing[meeting.agenda_url] = row
                elif not args.download:
                    write_discovery_rows(meeting, search_url, existing_discovery_keys)
        finally:
            await browser.close()

    return 0


if __name__ == "__main__":


    raise SystemExit(asyncio.run(main()))
