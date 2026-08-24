from __future__ import annotations

import re
import urllib.parse

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text, _search_results_table_present
from scraper.common.io_utils import normalize_meeting_date
from scraper.common.models import Meeting
from scraper.common.utils import SEARCH_BASE

def parse_search_results_html(html: str, base_url: str) -> list[Meeting]:
    root = _parse_html(html)
    tables = _find_all(root, "table")
    meeting_tables = []
    for candidate in tables:
        table_text = _clean_html_text(_node_text(candidate)).lower()
        if all(token in table_text for token in ["meeting name", "meeting type", "meeting date", "links"]):
            meeting_tables.append(candidate)

    # Collect rows from all matching tables (year-tab sections each have
    # their own table).  Fall back to scanning all <tr> elements if no
    # matching table is found.
    row_candidates: list = []
    if meeting_tables:
        for mt in meeting_tables:
            row_candidates.extend(_find_all(mt, "tr"))
    else:
        row_candidates = _find_all(root, "tr")

    meetings: list[Meeting] = []
    seen_ids: set[str] = set()  # deduplicate by meeting_id (from agenda URL)

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
            # Decode HTML entities (e.g. &amp; → &) in href values
            decoded_href = __import__('html').unescape(href) if href else ""
            abs_url = urllib.parse.urljoin(base_url, decoded_href) if decoded_href else ""
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

        # Deduplicate: extract meeting_id from agenda URL
        mid = None
        for url in (agenda.get("href", ""),):
            m = re.search(r"[?&]ID=(\d+)", url, re.I)
            if m:
                mid = m.group(1)
                break
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)

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



def build_search_url(start_date: dt.date, end_date: dt.date) -> str:
    params = {
        "dropid": "11",
        "dropsv": f"{start_date:%m/%d/%Y} 00:00:00",
        "dropev": f"{end_date:%m/%d/%Y} 23:59:59",
    }
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{SEARCH_BASE}?{query}"



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

