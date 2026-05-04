#!/usr/bin/env python3
"""Capture offline HTML fixtures for Maricopa County Agenda Online tests.

This script intentionally captures agenda HTML pages only. It never requests
Agenda Online PDF DownloadFile URLs and never calls OpenAI APIs.

Do not run broad fixture captures casually. Use --dry-run first.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
AGENDA_FIXTURE_DIR = FIXTURE_ROOT / "agendas"
MANIFEST_CSV = FIXTURE_ROOT / "fixtures_manifest.csv"
LOGS_ROOT = ROOT / "logs"

SEARCH_BASE = "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/Search"
AGENDA_URL_TEMPLATE = "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id={meeting_id}&doctype=1"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MANIFEST_FIELDS = [
    "meeting_id",
    "meeting_date",
    "meeting_type",
    "source_url",
    "local_fixture_path",
    "reason_included",
    "validation_status",
    "html_sha256",
    "captured_at",
]


@dataclass(frozen=True)
class FixtureTarget:
    meeting_id: str
    meeting_date: str
    meeting_type: str
    source_url: str
    reason_included: str
    meeting_time: str = ""


@dataclass(frozen=True)
class CaptureValidationResult:
    passed: bool
    validation_status: str
    html_sha256: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


# Seed targets are deliberately stored in the script so fixture capture is
# reproducible and reviewable. Dynamic rules below may add targets when local
# metadata or Agenda Online search pages expose matching meetings.
STATIC_FIXTURE_TARGETS: list[FixtureTarget] = [
    FixtureTarget(
        meeting_id="4470",
        meeting_date="2025-01-29",
        meeting_type="Special",
        source_url=AGENDA_URL_TEMPLATE.format(meeting_id="4470"),
        reason_included="required 2025-01-29 Special fixture; known agenda-table shape",
    ),
    FixtureTarget(
        meeting_id="4449",
        meeting_date="2025-01-29",
        meeting_type="Formal",
        source_url=AGENDA_URL_TEMPLATE.format(meeting_id="4449"),
        reason_included="required 2025-01-29 Formal fixture; baseline formal meeting",
    ),
    FixtureTarget(
        meeting_id="4471",
        meeting_date="2025-01-27",
        meeting_type="Executive",
        source_url=AGENDA_URL_TEMPLATE.format(meeting_id="4471"),
        reason_included="required 2025 Executive fixture from existing January metadata",
    ),
    FixtureTarget(
        meeting_id="4448",
        meeting_date="2025-01-27",
        meeting_type="Informal",
        source_url=AGENDA_URL_TEMPLATE.format(meeting_id="4448"),
        reason_included="required 2025 Informal fixture from existing January metadata",
    ),
]

DYNAMIC_TARGET_RULES = [
    {
        "name": "later_2025_formal_from_local_metadata",
        "source": "local_metadata",
        "year": 2025,
        "meeting_type_any": ["Formal"],
        "after": "2025-01-31",
        "limit": 1,
        "reason": "later 2025 Formal fixture discovered from local metadata",
    },
    {
        "name": "formal_2026_from_agenda_online",
        "source": "agenda_online_search",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "meeting_type_any": ["Formal"],
        "limit": 1,
        "reason": "2026 Formal fixture discovered from Agenda Online search if available",
    },
    {
        "name": "special_or_informal_2026_from_agenda_online",
        "source": "agenda_online_search",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "meeting_type_any": ["Special", "Informal"],
        "limit": 1,
        "reason": "2026 Special or Informal fixture discovered from Agenda Online search if available",
    },
]


class _LinkTableParser(HTMLParser):
    """Small tolerant parser for Agenda Online table rows and links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, object]]] = []
        self._current_row: Optional[list[dict[str, object]]] = None
        self._current_cell: Optional[dict[str, object]] = None
        self._current_link: Optional[dict[str, str]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        tag = tag.lower()
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = {"text": "", "links": []}
        elif tag == "a" and self._current_cell is not None:
            self._current_link = {"text": "", "href": attrs_dict.get("href", "")}

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._current_link is not None and self._current_cell is not None:
            self._current_cell["links"].append(self._current_link)
            self._current_link = None
        elif tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_cell["text"] = clean_text(str(self._current_cell.get("text", "")))
            self._current_row.append(self._current_cell)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell["text"] = str(self._current_cell.get("text", "")) + data
        if self._current_link is not None:
            self._current_link["text"] = self._current_link.get("text", "") + data


class _VisibleTextAndLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible_text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._current_link: Optional[dict[str, str]] = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            attrs_dict = {k.lower(): v or "" for k, v in attrs}
            self._current_link = {"href": attrs_dict.get("href", ""), "text": ""}

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._current_link is not None:
            self.links.append(self._current_link)
            self._current_link = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._current_link is not None:
            self._current_link["text"] = self._current_link.get("text", "") + data
        text = clean_text(data)
        if text:
            self.visible_text_parts.append(text)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def target_date_label(meeting_date: str) -> str:
    date = dt.date.fromisoformat(meeting_date)
    return f"{date:%A}, {date:%B} {date.day}, {date:%Y}"


def normalized_visible_text(parser: _VisibleTextAndLinkParser) -> str:
    return clean_text(" ".join(parser.visible_text_parts))


def expected_heading_for_type(meeting_type: str) -> str:
    return f"{clean_text(meeting_type).upper()} MEETING AGENDA"


def capture_html_validation(target: FixtureTarget, html: str, source_url: str = "", captured_url: str = "") -> CaptureValidationResult:
    digest = sha256_hex(html)
    parser = _VisibleTextAndLinkParser()
    parser.feed(html or "")
    visible_text = normalized_visible_text(parser)
    visible_upper = visible_text.upper()
    html_lower = (html or "").lower()
    html_upper = (html or "").upper()
    meeting_id = target.meeting_id.strip()
    expected_date = target_date_label(target.meeting_date)
    expected_type = target.meeting_type.strip()
    expected_heading = expected_heading_for_type(expected_type)
    errors: list[str] = []
    warnings: list[str] = []
    has_agenda_table = 'id="agenda-table"' in html_lower or "id='agenda-table'" in html_lower
    has_agenda_items = bool(re.search(r'lnkagendaitem_\d+', html_lower) or re.search(r'\b\d+\.\s+\w', html))
    page_is_onbase_error = "error - onbase agenda online" in html_lower
    right_meeting_context = bool(
        meeting_id
        and meeting_id in (captured_url or source_url or "")
        and has_agenda_table
        and not page_is_onbase_error
        and has_agenda_items
    )

    if meeting_id:
        link_matches = [
            link
            for link in parser.links
            if meeting_id in (link.get("href", "") or "")
            or meeting_id in clean_text(link.get("text", ""))
        ]
        if not link_matches and meeting_id not in html_lower and meeting_id not in (captured_url or "") and meeting_id not in (source_url or ""):
            errors.append(f"missing expected meeting_id {meeting_id} in links, anchors, source URL, or HTML")
        elif not link_matches and meeting_id not in html_lower:
            warnings.append(f"meeting_id {meeting_id} only confirmed via source URL, not page links/anchors")

    if expected_heading.upper() not in visible_upper and expected_heading.upper() not in html_upper:
        errors.append(f"missing expected visible agenda type {expected_type}")

    if expected_date not in visible_text:
        if right_meeting_context:
            warnings.append(f"visible meeting date did not match expected {expected_date}")
        else:
            errors.append(f"missing expected visible meeting date {expected_date}")

    validation_status = "passed" if not errors else "failed"
    return CaptureValidationResult(
        passed=not errors,
        validation_status=validation_status,
        html_sha256=digest,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def print_hash_and_validation(target: FixtureTarget, result: CaptureValidationResult) -> None:
    print(f"sha256: {target.meeting_id} {target.meeting_date} {target.meeting_type} {result.html_sha256}", file=sys.stderr)
    for warning in result.warnings:
        print(f"warning: {target.meeting_id} {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"error: {target.meeting_id} {error}", file=sys.stderr)


def warn_if_duplicate_fixture_hash(destination: Path, html_sha256: str) -> None:
    if not AGENDA_FIXTURE_DIR.exists():
        return
    for existing in sorted(AGENDA_FIXTURE_DIR.glob("*.html")):
        if existing == destination:
            continue
        try:
            existing_hash = sha256_hex(existing.read_text(encoding="utf-8"))
        except Exception:
            continue
        if existing_hash == html_sha256:
            print(
                f"warning: identical fixture hash detected for {relpath(destination)} and {relpath(existing)} ({html_sha256})",
                file=sys.stderr,
            )
            return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture Maricopa Agenda Online HTML fixtures")
    parser.add_argument("--dry-run", action="store_true", help="Show planned captures; do not write fixture files or manifest")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing fixture HTML files and manifest rows")
    parser.add_argument("--no-playwright", action="store_true", help="Opt out of Playwright for capture and use direct HTTP instead")
    parser.add_argument("--offline-targets-only", action="store_true", help="Use only static targets and local metadata; skip Agenda Online discovery rules")
    parser.add_argument("--meeting-id", help="Capture only the matching meeting_id fixture")
    return parser.parse_args()


def agenda_url(meeting_id: str) -> str:
    return AGENDA_URL_TEMPLATE.format(meeting_id=meeting_id)


def build_search_url(start_date: str, end_date: str) -> str:
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    query = urllib.parse.urlencode(
        {
            "dropid": "11",
            "dropsv": f"{start:%m/%d/%Y} 00:00:00",
            "dropev": f"{end:%m/%d/%Y} 23:59:59",
        },
        quote_via=urllib.parse.quote,
    )
    return f"{SEARCH_BASE}?{query}"


def build_search_url_for_meeting_date(meeting_date: str) -> str:
    return build_search_url(meeting_date, meeting_date)


def normalize_date(raw: str) -> str:
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw or "")
    if not match:
        return raw or ""
    return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"


def meeting_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    params = urllib.parse.parse_qs(parsed.query)
    for key in ("id", "ID", "meetingId"):
        if params.get(key):
            return params[key][0]
    match = re.search(r"[?&](?:id|ID|meetingId)=(\d+)", url or "")
    return match.group(1) if match else ""


def safe_filename(target: FixtureTarget) -> str:
    meeting_type = re.sub(r"[^A-Za-z0-9]+", "-", target.meeting_type).strip("-").lower() or "meeting"
    return f"{target.meeting_date}_{meeting_type}_{target.meeting_id}_agenda.html"


def local_fixture_path(target: FixtureTarget) -> Path:
    return AGENDA_FIXTURE_DIR / safe_filename(target)


def relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def read_local_metadata_targets(rule: dict[str, object]) -> list[FixtureTarget]:
    metadata_paths = [ROOT / "data" / "discovery_metadata.csv", *sorted((ROOT / "data" / "agendas").glob("**/metadata.csv"))]
    wanted_types = {str(t).lower() for t in rule.get("meeting_type_any", [])}
    after = str(rule.get("after", ""))
    year = int(rule["year"])
    limit = int(rule.get("limit", 1))
    targets: list[FixtureTarget] = []
    seen: set[str] = set()

    for metadata_path in metadata_paths:
        if not metadata_path.exists():
            continue
        with metadata_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row.get("document_category") or row.get("documentCategory") or "").strip().lower() != "agenda":
                    continue
                meeting_date = (row.get("record_date") or row.get("meeting_date") or "").strip()
                if not meeting_date.startswith(f"{year}-"):
                    continue
                if after and meeting_date <= after:
                    continue
                meeting_type = (row.get("meeting_type") or row.get("record_title") or "").strip()
                if wanted_types and meeting_type.lower() not in wanted_types:
                    continue
                source_url = (row.get("document_url") or row.get("agenda_url") or "").strip()
                if not is_agenda_html_url(source_url):
                    continue
                meeting_id = (row.get("record_id") or row.get("meeting_id") or meeting_id_from_url(source_url)).strip()
                if not meeting_id or meeting_id in seen:
                    continue
                seen.add(meeting_id)
                targets.append(FixtureTarget(meeting_id, meeting_date, meeting_type, source_url, str(rule["reason"])))
                if len(targets) >= limit:
                    return targets
    return targets


def parse_search_targets(html: str, base_url: str, rule: dict[str, object]) -> list[FixtureTarget]:
    parser = _LinkTableParser()
    parser.feed(html or "")
    wanted_types = {str(t).lower() for t in rule.get("meeting_type_any", [])}
    limit = int(rule.get("limit", 1))
    targets: list[FixtureTarget] = []
    seen: set[str] = set()

    for row in parser.rows:
        cells = [clean_text(str(cell.get("text", ""))) for cell in row]
        row_text = clean_text(" ".join(cells))
        date_match = re.search(r"\d{1,2}/\d{1,2}/\d{4}", row_text)
        if not date_match:
            continue
        meeting_date = normalize_date(date_match.group(0))
        meeting_type = cells[1] if len(cells) > 1 else ""
        if wanted_types and meeting_type.lower() not in wanted_types:
            continue
        links: list[dict[str, str]] = []
        for cell in row:
            links.extend(cell.get("links", []))  # type: ignore[arg-type]
        agenda_link = find_agenda_link(links, base_url)
        if not agenda_link:
            continue
        meeting_id = meeting_id_from_url(agenda_link)
        if not meeting_id or meeting_id in seen:
            continue
        seen.add(meeting_id)
        targets.append(FixtureTarget(meeting_id, meeting_date, meeting_type, agenda_link, str(rule["reason"])))
        if len(targets) >= limit:
            break
    return targets


def find_agenda_link(links: Iterable[dict[str, str]], base_url: str) -> str:
    for link in links:
        text = clean_text(link.get("text", "")).lower()
        href = urllib.parse.urljoin(base_url, link.get("href", ""))
        parsed = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(parsed.query)
        if text == "agenda" or (params.get("doctype") or [""])[0] == "1":
            return href
    return ""


def discover_online_targets(rule: dict[str, object], allow_playwright: bool) -> list[FixtureTarget]:
    search_url = build_search_url(str(rule["start_date"]), str(rule["end_date"]))
    html = fetch_html_direct(search_url)
    if html is None and allow_playwright:
        html = fetch_html_playwright(search_url)
    if html is None:
        print(f"warning: unable to discover online targets for rule {rule['name']}", file=sys.stderr)
        return []
    return parse_search_targets(html, search_url, rule)


def is_agenda_html_url(url: str) -> bool:
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    if "DownloadFile" in parsed.path:
        return False
    return parsed.path.endswith("/Meetings/ViewMeeting") and (params.get("doctype") or [""])[0] == "1"


def fetch_html_direct(url: str) -> Optional[str]:
    if not is_safe_html_request_url(url):
        raise ValueError(f"Refusing non-HTML or PDF-like URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec - public county records HTML only
            content_type = response.headers.get_content_type() if response.headers else ""
            body = response.read()
    except (urllib.error.URLError, TimeoutError):
        return None
    if body.startswith(b"%PDF") or "pdf" in content_type.lower():
        raise RuntimeError(f"Refusing to save PDF response from {url}")
    if "html" not in content_type.lower() and b"<html" not in body[:500].lower():
        return None
    return body.decode("utf-8", errors="replace")


def is_safe_html_request_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc.endswith("databankcloud.com"):
        return False
    if "DownloadFile" in parsed.path:
        return False
    if parsed.path.endswith("/Meetings/ViewMeeting"):
        return (urllib.parse.parse_qs(parsed.query).get("doctype") or [""])[0] == "1"
    return parsed.path.endswith("/Meetings/Search")


def fetch_html_playwright(url: str) -> Optional[str]:
    if not is_safe_html_request_url(url):
        raise ValueError(f"Refusing non-HTML or PDF-like URL: {url}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"warning: Playwright unavailable for fallback capture: {exc}", file=sys.stderr)
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1_000)
            html = page.content()
        finally:
            browser.close()
    if "%PDF" in html[:20] or "DownloadFile" in url:
        raise RuntimeError(f"Refusing to save PDF-like response from {url}")
    return html


def capture_agenda_html_playwright(url: str) -> Optional[str]:
    if not is_safe_html_request_url(url):
        raise ValueError(f"Refusing non-HTML or PDF-like URL: {url}")
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"error: Playwright is required for default fixture capture: {exc}", file=sys.stderr)
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("div#agenda-table.container-fluid", state="attached", timeout=60_000)
            page.wait_for_timeout(1_000)
            html = page.content()
        except PlaywrightTimeoutError:
            title = safe_page_title(page)
            current_url = safe_page_url(page)
            save_fixture_capture_debug(page, url, title=title, current_url=current_url)
            print(f"error: timed out waiting for agenda table at {url}", file=sys.stderr)
            print(f"error: page title={title!r} url={current_url!r}", file=sys.stderr)
            return None
        finally:
            browser.close()

    return html


def safe_page_title(page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def safe_page_url(page) -> str:
    try:
        return page.url
    except Exception:
        return ""


def target_debug_prefix(meeting_id: str) -> Path:
    return LOGS_ROOT / f"fixture_capture_debug_{meeting_id}"


def write_target_debug_text(meeting_id: str, suffix: str, text: str) -> None:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    path = LOGS_ROOT / f"fixture_capture_debug_{meeting_id}{suffix}"
    try:
        path.write_text(text or "", encoding="utf-8")
    except Exception as exc:
        print(f"error: unable to write debug file {path.name}: {exc}", file=sys.stderr)


def write_target_debug_png(page, meeting_id: str) -> None:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    path = LOGS_ROOT / f"fixture_capture_debug_{meeting_id}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:
        print(f"error: unable to write debug screenshot {path.name}: {exc}", file=sys.stderr)


def format_capture_context(context: dict[str, str]) -> str:
    return (
        f"target={context.get('meeting_id', '')} {context.get('meeting_date', '')} {context.get('meeting_type', '')}\n"
        f"search_url={context.get('search_url', '')}\n"
        f"rows_found={context.get('rows_found', '')}\n"
        f"matched_row_text={context.get('matched_row_text', '')}\n"
        f"agenda_href={context.get('agenda_href', '')}\n"
        f"opened_url={context.get('opened_url', '')}\n"
        f"final_url={context.get('final_url', '')}\n"
        f"page_title={context.get('page_title', '')}\n"
    )


def _search_result_match_script() -> str:
    return """
            ({ meetingId, meetingDate, meetingType, meetingTime, baseUrl }) => {
              const clean = s => (s || '').replace(/\s+/g, ' ').trim();
              const absUrl = href => {
                try { return new URL(href, baseUrl).toString(); } catch { return href || ''; }
              };
              const normalizeDateTime = text => clean(text).replace(/\s+/g, ' ');
              const expectedDate = clean(meetingDate);
              const expectedType = clean(meetingType).toLowerCase();
              const expectedTime = clean(meetingTime);
              const tables = Array.from(document.querySelectorAll('table'));
              const table = tables.find(t => {
                const ttext = clean(t.innerText || t.textContent || '').toLowerCase();
                return ttext.includes('meeting name') && ttext.includes('meeting type') && ttext.includes('meeting date') && ttext.includes('links');
              });
              if (!table) return { ready: false, rowsFound: 0, matchedRowText: '', agendaHref: '', openedUrl: '', finalUrl: '', pageTitle: '', rowsSummary: [] };

              const rows = Array.from(table.querySelectorAll('tr'));
              const rowsSummary = [];
              for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('th, td')).map(cell => clean(cell.innerText || cell.textContent || ''));
                const rowText = clean(row.innerText || row.textContent || '');
                if (!rowText || rowText.toLowerCase().includes('meeting name')) continue;
                rowsSummary.push(rowText);

                const agendaAnchor = Array.from(row.querySelectorAll('a[href]')).find(a => {
                  const text = clean(a.innerText || a.textContent || '').toLowerCase();
                  const href = absUrl(a.getAttribute('href') || '');
                  let doctype = '';
                  try { doctype = new URL(href).searchParams.get('doctype') || ''; } catch {}
                  return text === 'agenda' || doctype === '1';
                });
                if (!agendaAnchor) continue;

                const href = absUrl(agendaAnchor.getAttribute('href') || '');
                const lowerRow = rowText.toLowerCase();
                const lowerType = (cells[1] || '').toLowerCase();
                const rowDateRaw = cells.find(c => /\d{1,2}\/\d{1,2}\/\d{4}/.test(c)) || '';
                let rowDateIso = '';
                const rowDateMatch = rowDateRaw.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
                if (rowDateMatch) {
                  rowDateIso = `${rowDateMatch[3]}-${rowDateMatch[1].padStart(2, '0')}-${rowDateMatch[2].padStart(2, '0')}`;
                }
                let rowMeetingId = '';
                try {
                  const parsed = new URL(href);
                  rowMeetingId = parsed.searchParams.get('id') || parsed.searchParams.get('ID') || parsed.searchParams.get('meetingId') || '';
                } catch {}

                rowsSummary.push(`${rowMeetingId} ${rowText}`.trim());

                const rowTypeMatches = expectedType ? lowerType === expectedType || lowerType.includes(expectedType) : true;
                const rowDateMatches = expectedDate ? rowDateIso === expectedDate || lowerRow.includes(expectedDate.toLowerCase()) : true;
                const rowTimeMatches = expectedTime ? normalizeDateTime(rowText).includes(expectedTime) : true;

                const matchesById = meetingId && rowMeetingId === meetingId && rowTypeMatches && rowDateMatches;
                const matchesByDateTime = !meetingId && rowTypeMatches && rowDateMatches && rowTimeMatches;

                if (matchesById || matchesByDateTime) return {
                  ready: true,
                  rowsFound: rowsSummary.length,
                  matchedRowText: rowText,
                  agendaHref: href,
                  openedUrl: href,
                  finalUrl: '',
                  pageTitle: '',
                  rowsSummary,
                };
              }
              return { ready: false, rowsFound: rowsSummary.length, matchedRowText: '', agendaHref: '', openedUrl: '', finalUrl: '', pageTitle: '', rowsSummary };
            }
            """


def extract_agenda_href_from_search_results(page, target: FixtureTarget, search_url: str) -> Optional[dict[str, str]]:
    try:
        return page.evaluate(
            _search_result_match_script(),
            {
                "meetingId": target.meeting_id,
                "meetingDate": target.meeting_date,
                "meetingType": target.meeting_type,
                "meetingTime": target.meeting_time,
                "baseUrl": search_url,
            },
        )
    except Exception:
        return None


def capture_agenda_html_from_search_context(target: FixtureTarget) -> Optional[str]:
    search_url = build_search_url_for_meeting_date(target.meeting_date)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"error: Playwright is required for default fixture capture: {exc}", file=sys.stderr)
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            context = {
                "meeting_id": target.meeting_id,
                "meeting_date": target.meeting_date,
                "meeting_type": target.meeting_type,
                "search_url": search_url,
                "rows_found": "",
                "matched_row_text": "",
                "agenda_href": "",
                "opened_url": "",
                "final_url": "",
                "page_title": "",
            }
            print(format_capture_context(context), file=sys.stderr, end="")
            page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector('table:has(th:has-text("Meeting Name"))', state="attached", timeout=60_000)
            try:
                page.wait_for_function(
                    _search_result_match_script(),
                    arg={
                    "meetingId": target.meeting_id,
                    "meetingDate": target.meeting_date,
                    "meetingType": target.meeting_type,
                    "meetingTime": target.meeting_time,
                    "baseUrl": search_url,
                    },
                    timeout=60_000,
                )
            except PlaywrightTimeoutError:
                search_html = page.content()
                write_target_debug_text(target.meeting_id, "_search.html", search_html)
                write_target_debug_png(page, target.meeting_id)
                match = extract_agenda_href_from_search_results(page, target, search_url)
                rows_summary = (match or {}).get("rowsSummary", [])
                print(
                    f"error: search results never showed target meeting for {target.meeting_id} ({target.meeting_date} {target.meeting_type}); rows_found={rows_summary}",
                    file=sys.stderr,
                )
                print(format_capture_context({
                    **context,
                    "rows_found": str((match or {}).get("rowsFound", "")),
                    "matched_row_text": str((match or {}).get("matchedRowText", "")),
                    "agenda_href": str((match or {}).get("agendaHref", "")),
                    "opened_url": str((match or {}).get("openedUrl", "")),
                    "final_url": safe_page_url(page),
                    "page_title": safe_page_title(page),
                }), file=sys.stderr, end="")
                return None

            page.wait_for_timeout(1_000)
            search_html = page.content()
            write_target_debug_text(target.meeting_id, "_search.html", search_html)
            match = extract_agenda_href_from_search_results(page, target, search_url)
            if not match or not match.get("agendaHref"):
                write_target_debug_png(page, target.meeting_id)
                print(
                    f"error: no exact matching agenda row found in search results for {target.meeting_id} ({target.meeting_date} {target.meeting_type}); refusing fallback match",
                    file=sys.stderr,
                )
                print(format_capture_context({
                    **context,
                    "rows_found": str((match or {}).get("rowsFound", "")),
                    "matched_row_text": str((match or {}).get("matchedRowText", "")),
                    "agenda_href": str((match or {}).get("agendaHref", "")),
                    "opened_url": str((match or {}).get("openedUrl", "")),
                    "final_url": safe_page_url(page),
                    "page_title": safe_page_title(page),
                }), file=sys.stderr, end="")
                return None

            rows_summary = match.get("rowsSummary", []) or []
            if target.meeting_id and not any(target.meeting_id in row for row in rows_summary):
                write_target_debug_text(target.meeting_id, "_search.html", search_html)
                write_target_debug_png(page, target.meeting_id)
                print(
                    f"error: search results rows did not include target meeting_id {target.meeting_id}; refusing capture",
                    file=sys.stderr,
                )
                print(format_capture_context({
                    **context,
                    "rows_found": str(match.get("rowsFound", "")),
                    "matched_row_text": str(match.get("matchedRowText", "")),
                    "agenda_href": str(match.get("agendaHref", "")),
                    "opened_url": str(match.get("openedUrl", "")),
                    "final_url": safe_page_url(page),
                    "page_title": safe_page_title(page),
                }), file=sys.stderr, end="")
                return None

            context.update(
                rows_found=str(match.get("rowsFound", "")),
                matched_row_text=str(match.get("matchedRowText", "")),
                agenda_href=str(match.get("agendaHref", "")),
                opened_url=str(match.get("openedUrl", "")),
            )
            print(format_capture_context(context), file=sys.stderr, end="")

            page.goto(match["agendaHref"], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("div#agenda-table.container-fluid", state="attached", timeout=60_000)
            page.wait_for_timeout(1_000)
            html = page.content()
            context["final_url"] = safe_page_url(page)
            context["page_title"] = safe_page_title(page)
            write_target_debug_text(target.meeting_id, "_agenda.html", html)
            write_target_debug_png(page, target.meeting_id)
            print(format_capture_context(context), file=sys.stderr, end="")
        except PlaywrightTimeoutError:
            title = safe_page_title(page)
            current_url = safe_page_url(page)
            write_target_debug_text(target.meeting_id, "_agenda.html", page.content())
            write_target_debug_png(page, target.meeting_id)
            print(
                f"error: timed out during search-context capture for {target.meeting_id} ({target.meeting_date} {target.meeting_type})",
                file=sys.stderr,
            )
            print(f"error: page title={title!r} url={current_url!r}", file=sys.stderr)
            print(format_capture_context({
                "meeting_id": target.meeting_id,
                "meeting_date": target.meeting_date,
                "meeting_type": target.meeting_type,
                "search_url": search_url,
                "rows_found": "",
                "matched_row_text": "",
                "agenda_href": "",
                "opened_url": "",
                "final_url": current_url,
                "page_title": title,
            }), file=sys.stderr, end="")
            return None
        finally:
            browser.close()

    return html


def save_fixture_capture_debug(page, source_url: str, title: str = "", current_url: str = "") -> None:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    meeting_id = meeting_id_from_url(source_url) or "unknown"
    html_path = LOGS_ROOT / f"fixture_capture_debug_{meeting_id}.html"
    png_path = LOGS_ROOT / f"fixture_capture_debug_{meeting_id}.png"
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"error: unable to write debug HTML for {meeting_id}: {exc}", file=sys.stderr)
    try:
        page.screenshot(path=str(png_path), full_page=True)
    except Exception as exc:
        print(f"error: unable to write debug screenshot for {meeting_id}: {exc}", file=sys.stderr)
    if title or current_url:
        print(f"debug: title={title!r} url={current_url!r}", file=sys.stderr)


def validate_captured_agenda_html(html: str) -> bool:
    normalized = (html or "").lower()
    if "error - onbase agenda online" in normalized:
        print("error: captured HTML is the OnBase error page, not an agenda page", file=sys.stderr)
        return False
    if 'id="agenda-table"' not in normalized and "id='agenda-table'" not in normalized:
        print("error: captured HTML does not contain div#agenda-table", file=sys.stderr)
        return False
    return True


def collect_targets(args: argparse.Namespace) -> list[FixtureTarget]:
    targets = list(STATIC_FIXTURE_TARGETS)
    seen = {target.meeting_id for target in targets}

    if args.meeting_id:
        static_match = [target for target in targets if target.meeting_id == args.meeting_id]
        if static_match:
            return static_match

    for rule in DYNAMIC_TARGET_RULES:
        if rule["source"] == "local_metadata":
            discovered = read_local_metadata_targets(rule)
        elif rule["source"] == "agenda_online_search":
            if args.offline_targets_only:
                discovered = []
            else:
                discovered = discover_online_targets(rule, allow_playwright=not args.no_playwright)
        else:
            discovered = []

        for target in discovered:
            if target.meeting_id in seen:
                continue
            seen.add(target.meeting_id)
            targets.append(target)

    if args.meeting_id:
        targets = [target for target in targets if target.meeting_id == args.meeting_id]
    return targets


def read_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST_CSV.exists():
        return {}
    with MANIFEST_CSV.open(newline="", encoding="utf-8") as f:
        rows: dict[str, dict[str, str]] = {}
        seen_hashes: dict[str, str] = {}
        for row in csv.DictReader(f):
            meeting_id = row.get("meeting_id")
            if not meeting_id:
                continue
            normalized = {field: row.get(field, "") for field in MANIFEST_FIELDS}
            fixture_path = ROOT / normalized["local_fixture_path"] if normalized["local_fixture_path"] else None
            if fixture_path and fixture_path.exists():
                try:
                    html = fixture_path.read_text(encoding="utf-8")
                    validation = capture_html_validation(
                        FixtureTarget(
                            meeting_id=normalized["meeting_id"],
                            meeting_date=normalized["meeting_date"],
                            meeting_type=normalized["meeting_type"],
                            source_url=normalized["source_url"],
                            reason_included=normalized["reason_included"],
                        ),
                        html,
                        source_url=normalized["source_url"],
                        captured_url=normalized["source_url"],
                    )
                    normalized["validation_status"] = validation.validation_status
                    normalized["html_sha256"] = validation.html_sha256
                    if validation.html_sha256 in seen_hashes and seen_hashes[validation.html_sha256] != normalized["local_fixture_path"]:
                        print(
                            f"warning: identical fixture hash detected for {normalized['local_fixture_path']} and {seen_hashes[validation.html_sha256]} ({validation.html_sha256})",
                            file=sys.stderr,
                        )
                    else:
                        seen_hashes[validation.html_sha256] = normalized["local_fixture_path"]
                except Exception:
                    pass
            rows[meeting_id] = normalized
        return rows


def write_manifest(rows_by_id: dict[str, dict[str, str]]) -> None:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows_by_id.values(), key=lambda r: (r["meeting_date"], r["meeting_type"], r["meeting_id"])):
            writer.writerow({field: row.get(field, "") for field in MANIFEST_FIELDS})


def capture_target(target: FixtureTarget, args: argparse.Namespace) -> Optional[dict[str, str]]:
    if not is_agenda_html_url(target.source_url):
        raise ValueError(f"Target is not an agenda HTML URL: {target.source_url}")

    destination = local_fixture_path(target)
    manifest_row = {
        "meeting_id": target.meeting_id,
        "meeting_date": target.meeting_date,
        "meeting_type": target.meeting_type,
        "source_url": target.source_url,
        "local_fixture_path": relpath(destination),
        "reason_included": target.reason_included,
        "validation_status": "",
        "html_sha256": "",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }

    if args.dry_run:
        status = "would overwrite" if destination.exists() and args.overwrite else "would skip existing" if destination.exists() else "would capture"
        print(f"{status}: {target.meeting_id} {target.meeting_date} {target.meeting_type} -> {relpath(destination)}")
        return None

    if destination.exists() and not args.overwrite:
        print(f"skip existing: {relpath(destination)}")
        return None

    try:
        if args.no_playwright:
            html = fetch_html_direct(target.source_url)
            if html is None:
                print(f"error: unable to capture HTML via direct HTTP for {target.meeting_id} ({target.source_url})", file=sys.stderr)
                return None
        else:
            html = capture_agenda_html_from_search_context(target)
            if html is None:
                print(f"warning: search-context capture failed for {target.meeting_id}; trying direct ViewMeeting fallback", file=sys.stderr)
                html = capture_agenda_html_playwright(target.source_url)
            if html is None:
                print(f"error: Playwright capture failed for {target.meeting_id} ({target.source_url}); not writing file or manifest", file=sys.stderr)
                return None
    except TimeoutError:
        print(f"error: timeout capturing {target.meeting_id} ({target.meeting_date} {target.meeting_type}); skipping target", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"error: capture failed for {target.meeting_id} ({target.source_url}): {exc}", file=sys.stderr)
        return None

    if not validate_captured_agenda_html(html):
        print(f"error: basic agenda HTML validation failed for {target.meeting_id} ({target.source_url}); not writing file or manifest", file=sys.stderr)
        return None

    result = capture_html_validation(target, html, source_url=target.source_url, captured_url=target.source_url)
    print_hash_and_validation(target, result)
    if not result.passed:
        print(f"error: target validation failed for {target.meeting_id}; not writing file or manifest", file=sys.stderr)
        return None

    AGENDA_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    warn_if_duplicate_fixture_hash(destination, result.html_sha256)
    manifest_row["validation_status"] = result.validation_status
    manifest_row["html_sha256"] = result.html_sha256
    print(f"captured: {target.meeting_id} -> {relpath(destination)}")
    time.sleep(0.5)
    return manifest_row


def main() -> int:
    args = parse_args()
    targets = collect_targets(args)
    if args.meeting_id and not targets:
        print(f"error: no fixture targets matched meeting_id={args.meeting_id}", file=sys.stderr)
        return 1
    manifest_rows = read_manifest()

    print(f"fixture target count: {len(targets)}")
    for target in targets:
        row = capture_target(target, args)
        if row is not None:
            manifest_rows[target.meeting_id] = row

    if not args.dry_run:
        write_manifest(manifest_rows)
        print(f"manifest: {relpath(MANIFEST_CSV)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
