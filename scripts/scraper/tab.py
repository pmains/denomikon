"""Transportation Advisory Board (TAB) meeting and agenda extraction.

Transportation Advisory Board uses the same AgendaCenter system as PZ/ADJ/DRAIN/Health
(CID=11 instead of CID=9/3/19/13), hosted on mcdot.maricopa.gov.

Year-tab pagination and catAgendaRow structure are identical to PZ/ADJ/DRAIN/Health.

Key differences:
- Agenda pages are PDF-only (no HTML table version available)
- Meeting titles use format: "Transportation Advisory Board Meeting (DATE)"
  or "TAB Agenda (DATE)"
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.io_utils import _normalize_text_date
from scraper.models import Meeting
from scraper.utils import PZ_SEARCH_BASE, PZ_AGENDA_BASE
from scraper.utils import CASE_PATTERN

# ── Constants ──

TAB_CID = "11,"
TAB_DOMAIN = "https://mcdot.maricopa.gov"
TAB_SEARCH_BASE = "https://mcdot.maricopa.gov/AgendaCenter/Search/"
TAB_AGENDA_BASE = "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/"


# ── Date formatting ──


def _format_mm_dd_yyyy(date_iso: str) -> str | None:
    """Convert YYYY-MM-DD to MM/DD/YYYY. Returns None if input is empty."""
    if not date_iso:
        return None
    try:
        d = dt.date.fromisoformat(date_iso)
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    except (ValueError, TypeError):
        if re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_iso):
            return date_iso
        return date_iso


# ── URL building ──


def build_tab_search_url(start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for TAB meetings (CID=11)."""
    params = {
        "term": "",
        "CIDs": TAB_CID,
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"{TAB_SEARCH_BASE}?{qs}"


# ── Year tab extraction ──


def _extract_tab_year_tabs_from_html(html: str) -> list[int]:
    """Extract unique year values from changeYear(...) links."""
    years: set[int] = set()
    for m in re.finditer(r"changeYear\((\d{4})", html):
        years.add(int(m.group(1)))
    return sorted(years)


# ── Meeting extraction ──


async def extract_tab_meetings(page, search_url: str) -> list[Meeting]:
    """Extract TAB meetings from AgendaCenter search results.

    Same year-tab clicking pattern as PZ/ADJ/DRAIN/Health.
    """
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    all_meetings: list[Meeting] = []
    seen_ids: set[str] = set()

    html = await page.content()
    initial_meetings = parse_tab_meetings_from_html(html, search_url)
    all_meetings.extend(initial_meetings)
    for m in initial_meetings:
        seen_ids.add(m.meeting_id)

    year_tabs = await page.evaluate(
        r"""
        () => {
            const seen = new Set();
            const years = [];
            for (const a of document.querySelectorAll('a[href*="changeYear"]')) {
                const m = (a.getAttribute('href') || '').match(/changeYear\((\d{4})/);
                if (m && !seen.has(m[1])) {
                    seen.add(m[1]);
                    years.push(parseInt(m[1], 10));
                }
            }
            return years.sort((a, b) => a - b);
        }
        """
    )

    loaded_years = {int(m.meeting_date[:4]) for m in all_meetings}

    for year in year_tabs:
        if year in loaded_years:
            continue
        try:
            tab_locator = page.locator(f'a[href*="changeYear({year}"]').first
            await tab_locator.evaluate("el => el.click()")
        except Exception:
            continue
        await page.wait_for_timeout(2500)
        html = await page.content()
        year_meetings = parse_tab_meetings_from_html(html, search_url)
        for m in year_meetings:
            if m.meeting_id not in seen_ids:
                all_meetings.append(m)
                seen_ids.add(m.meeting_id)

    return all_meetings


def parse_tab_meetings_from_html(html: str, base_url: str) -> list[Meeting]:
    """Parse TAB meetings from AgendaCenter search HTML.

    Same catAgendaRow structure as PZ/ADJ/DRAIN/Health.
    body='tab', meeting_type='Transportation Advisory Board'.
    """
    root = _parse_html(html)
    meetings: list[Meeting] = []

    rows = _find_all(root, "tr")
    for row in rows:
        classes = (row.attrs.get("class") or "").split()
        if "catAgendaRow" not in classes:
            continue

        cells = _find_all(row, "td")
        if not cells:
            continue

        first_cell = cells[0]

        # Extract meeting date from <strong> in <h3>
        meeting_date = ""
        for h3 in _find_all(first_cell, "h3"):
            for strong in _find_all(h3, "strong"):
                aria = strong.attrs.get("aria-label", "")
                if aria:
                    dm = re.search(r"(\w+ \d{1,2},? \d{4})", aria)
                    if dm:
                        meeting_date = _normalize_text_date(dm.group(1))
                        break
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

        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=meeting_title,
            meeting_type="Transportation Advisory Board",
            body="tab",
            row_text=_clean_html_text(_node_text(row)),
            detail_url="",
            agenda_url=agenda_url,
        ))

    return meetings


# ── Agenda extraction (PDF-only) ──


async def extract_tab_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract agenda items from a TAB meeting.

    TAB agendas are PDF-only. The PDF is downloaded directly and parsed
    via pdftotext to extract numbered items.
    """
    pdf_url = meeting_url
    pdf_path = Path(f"/tmp/tab_agenda_{Path(meeting_url).stem}.pdf")
    try:
        pdf_req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_path.write_bytes(pdf_resp.read())
        if not pdf_path.exists() or pdf_path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded file is {pdf_path.stat().st_size} bytes, too small")
    except Exception as e:
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

            # Collect continuation lines
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
                "source_body": "Transportation Advisory Board",
                "meeting_id": "",
                "meeting_date": "",
                "meeting_type": "Transportation Advisory Board",
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

    # Try a broader extraction if numbered items not found
    if not items:
        for m in re.finditer(r"([A-Z][A-Za-z\s-]{3,50})\s*[:.]\s*(\d+|Action|Discuss|Information)", pdf_text):
            title = m.group(1).strip()
            if title and len(title) > 5:
                title_key = f"auto:{title}"
                if title_key not in seen_titles:
                    seen_titles.add(title_key)
                    item = {
                        "source_body": "Transportation Advisory Board",
                        "meeting_id": "",
                        "meeting_date": "",
                        "meeting_type": "Transportation Advisory Board",
                        "agenda_item_number": str(len(items) + 1),
                        "agenda_item_id": "",
                        "agenda_item_title": title,
                        "agenda_item_text": title,
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
