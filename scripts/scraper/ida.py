"""Industrial Development Authority (IDA) meeting and document extraction.

IDA uses a WordPress site at mcida.com with a static HTML table
containing all meetings. This is NOT an AgendaCenter source.

Key characteristics:
- Single page with all meetings in one <table id="table-public-meetings">
- No native meeting IDs — synthetic IDs created from ISO dates
- Agenda and Minutes are PDF links (or "Not Available")
- Cancellations replace agenda PDFs with Notice-of-Cancellation PDFs
- All meetings on a single page, no pagination or AJAX loading
- No Playwright needed — static HTML is sufficient
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.io_utils import _normalize_text_date, normalize_meeting_date
from scraper.models import Meeting


# ── Source URL ──

IDA_SOURCE_URL = "https://mcida.com/about-us/public-meetings/"
IDA_DOCUMENT_BASE = "https://mcida.com"


# ── Synthetic meeting ID ──


def make_ida_meeting_id(date_iso: str) -> str:
    """Create a stable synthetic meeting ID from an ISO date string.

    IDA has no native meeting IDs, so we use the meeting date as the ID.
    This works because IDA only schedules one meeting per day.
    """
    return date_iso


# ── Document classification ──


def classify_ida_document(url: str) -> str:
    """Classify an IDA document by its URL.

    Returns one of: 'agenda', 'minutes', 'cancellation', 'other'
    """
    url_lower = url.lower()
    filename = url_lower.rstrip("/").split("/")[-1]

    if "notice-of-cancellation" in filename:
        return "cancellation"
    if "agenda" in filename:
        return "agenda"
    if "minutes" in filename or "results-of-public-meeting" in filename:
        return "minutes"
    if "result" in filename or "minute" in filename:
        return "minutes"
    return "other"


# ── Meeting extraction ──


async def extract_ida_meetings(page, search_url: str = "") -> list[Meeting]:
    """Extract IDA meetings from the public meetings page.

    IDA is a single static page — no search needed.
    The page argument is accepted for API compatibility but not used.
    Playwright is not required for IDA; we fetch the page directly.
    """
    import urllib.request

    req = urllib.request.Request(
        IDA_SOURCE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    return parse_ida_meetings_from_html(html)


def parse_ida_meetings_from_html(html: str) -> list[Meeting]:
    """Parse IDA meetings from the WordPress public meetings HTML.

    The page contains:
      <table id="table-public-meetings">
        <tr><td>Regular Meeting 2026-05-12</td>
            <td>05/12/26</td>
            <td><a href="...">Download</a></td>
            <td>Not Available or <a href="...">Download</a></td></tr>
        ...
      </table>
    """
    meetings: list[Meeting] = []

    root = _parse_html(html)

    # Find the meeting table by its ID
    table = None
    for el in _find_all(root, "table"):
        if el.attrs.get("id") == "table-public-meetings":
            table = el
            break

    if table is None:
        # Fallback: find any table with a header containing "Name" and "Date"
        for el in _find_all(root, "table"):
            ths = _find_all(el, "th")
            th_text = " ".join(_clean_html_text(_node_text(th)) for th in ths).lower()
            if "name" in th_text and "date" in th_text:
                table = el
                break

    if table is None:
        return []

    rows = _find_all(table, "tr")
    for row in rows:
        cells = _find_all(row, "td")
        if len(cells) < 4:
            continue

        # Cell 0: meeting title (e.g., "Regular Meeting 2026-05-12")
        title_text = _clean_html_text(_node_text(cells[0])).strip()
        if not title_text:
            continue

        # Cell 1: date (e.g., "05/12/26")
        raw_date = _clean_html_text(_node_text(cells[1])).strip()
        if not raw_date:
            continue

        # Normalize date: 05/12/26 -> 2026-05-12
        meeting_date = _normalize_ida_date(raw_date)
        if not meeting_date:
            continue

        # Cell 2: agenda link
        agenda_url = ""
        agenda_links = _find_all(cells[2], "a")
        for a in agenda_links:
            href = a.attrs.get("href", "").strip()
            if href and "wp-content/uploads" in href:
                agenda_url = href
                break

        if not agenda_url:
            # Try any link in the cell
            for a in agenda_links:
                href = a.attrs.get("href", "").strip()
                if href:
                    agenda_url = href
                    break

        # Cell 3: minutes link or "Not Available"
        minutes_url = ""
        minutes_text = _clean_html_text(_node_text(cells[3])).strip()
        if minutes_text.lower() != "not available":
            minutes_links = _find_all(cells[3], "a")
            for a in minutes_links:
                href = a.attrs.get("href", "").strip()
                if href and "wp-content/uploads" in href:
                    minutes_url = href
                    break

        meeting_id = make_ida_meeting_id(meeting_date)

        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=title_text,
            meeting_type="Industrial Development Authority",
            body="ida",
            row_text=_clean_html_text(_node_text(row)),
            detail_url=f"{IDA_SOURCE_URL}?meeting={meeting_id}",
            agenda_url=agenda_url,
            summary_url="",
            minutes_url=minutes_url,
            video_url="",
        ))

    return meetings


def _normalize_ida_date(raw: str) -> str:
    """Convert IDA date formats to ISO.

    Handles:
      - MM/DD/YY (e.g., "05/12/26")
      - MM/DD/YYYY (e.g., "05/12/2026")
      - YYYY-MM-DD (e.g., "2026-05-12")
    """
    raw = raw.strip()

    # Already ISO
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return raw

    # MM/DD/YY or MM/DD/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", raw)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year_raw = m.group(3)
        if len(year_raw) == 2:
            year = 2000 + int(year_raw)
        else:
            year = int(year_raw)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"

    return ""


# ── Agenda extraction ──


async def extract_ida_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract agenda items from an IDA meeting.

    IDA agendas are PDF-only. The PDF is downloaded via HTTP
    (no Playwright needed) and parsed with pdftotext.
    """
    import subprocess
    from pathlib import Path

    pdf_url = meeting_url if meeting_url else ""

    if not pdf_url or not pdf_url.startswith("http"):
        return []

    pdf_path = Path(f"/tmp/ida_agenda_{Path(pdf_url).stem}.pdf")
    try:
        import urllib.request
        pdf_req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_path.write_bytes(pdf_resp.read())
        if not pdf_path.exists() or pdf_path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded file is {pdf_path.stat().st_size} bytes, too small")
    except Exception:
        pdf_path.unlink(missing_ok=True)
        return []

    # Parse PDF via pdftotext
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        pdf_text = result.stdout
    except Exception:
        pdf_path.unlink(missing_ok=True)
        return []

    pdf_path.unlink(missing_ok=True)

    # Extract numbered items from PDF text
    items: list[dict] = []
    seen_titles: set[str] = set()
    lines = pdf_text.split("\n")

    for i, line in enumerate(lines):
        line = line.strip()
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if m:
            item_num = int(m.group(1))
            title = m.group(2).strip()

            full_text = title
            for j in range(i + 1, min(i + 10, len(lines))):
                next_line = lines[j].strip()
                if not next_line:
                    break
                if re.match(r"^\d+\.\s+", next_line):
                    break
                full_text += " " + next_line

            title_key = f"{item_num}:{title}"
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            item = {
                "source_body": "Industrial Development Authority",
                "meeting_id": "",
                "meeting_date": "",
                "meeting_type": "Industrial Development Authority",
                "agenda_item_number": str(item_num),
                "agenda_item_id": "",
                "agenda_item_title": title,
                "agenda_item_text": full_text,
                "agenda_item_url": meeting_url,
                "vote_or_action": "",
                "source_url": meeting_url,
                "c_number": "",
                "c_number_base": "",
                "c_number_revision": None,
                "case_number": "",
                "supporting_doc_dicts": [],
                "staff_report_url": None,
            }
            items.append(item)

    return items
