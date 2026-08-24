"""Board of Health meeting and agenda extraction.

Board of Health uses the same AgendaCenter system as PZ/ADJ/DRAIN
(CID=13 instead of CID=9/3/19), hosted on mcdot.maricopa.gov.

Year-tab pagination and catAgendaRow structure are identical to PZ/ADJ/DRAIN.

Key differences from PZ/ADJ/DRAIN:
- Agenda pages are BOS-style HTML (table with numbered items) or PDF-only
- Not PDF-first extraction like PZ
- Meeting titles are "Board of Health Meeting Agenda" or "(PDF)" variant
- <base href="https://www.maricopa.gov/"> on agenda pages
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from html import unescape
from typing import Optional

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.common.io_utils import _normalize_text_date
from scraper.common.models import Meeting
from scraper.common.utils import PZ_SEARCH_BASE, PZ_AGENDA_BASE
from scraper.common.utils import CASE_PATTERN

# ── Constants ──

HEALTH_CID = "13,"
HEALTH_DOMAIN = "https://mcdot.maricopa.gov"
HEALTH_SEARCH_BASE = "https://mcdot.maricopa.gov/AgendaCenter/Search/"
HEALTH_AGENDA_BASE = "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/"

# Fallback domain for <base> tags on agenda pages
HEALTH_WWW_DOMAIN = "https://www.maricopa.gov"


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


def build_health_search_url(start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for Board of Health meetings.

    Uses mcdot.maricopa.gov domain with CID=13.
    """
    params = {
        "term": "",
        "CIDs": HEALTH_CID,
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"{HEALTH_SEARCH_BASE}?{qs}"


# ── Year tab extraction ──


def _extract_health_year_tabs_from_html(html: str) -> list[int]:
    """Extract unique year values from changeYear(...) links."""
    years: set[int] = set()
    for m in re.finditer(r"changeYear\((\d{4})", html):
        years.add(int(m.group(1)))
    return sorted(years)


# ── Meeting extraction ──


async def extract_health_meetings(page, search_url: str) -> list[Meeting]:
    """Extract Board of Health meetings from AgendaCenter search results.

    Same year-tab clicking pattern as PZ/ADJ/DRAIN.
    """
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    all_meetings: list[Meeting] = []
    seen_ids: set[str] = set()

    # Collect from the initial (default year) HTML
    html = await page.content()
    initial_meetings = parse_health_meetings_from_html(html, search_url)
    all_meetings.extend(initial_meetings)
    for m in initial_meetings:
        seen_ids.add(m.meeting_id)

    # Find available year tabs
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
        year_meetings = parse_health_meetings_from_html(html, search_url)

        for m in year_meetings:
            if m.meeting_id not in seen_ids:
                all_meetings.append(m)
                seen_ids.add(m.meeting_id)

    return all_meetings


def parse_health_meetings_from_html(html: str, base_url: str) -> list[Meeting]:
    """Parse Board of Health meetings from AgendaCenter search HTML.

    Same catAgendaRow structure as PZ/ADJ/DRAIN.
    body='health', meeting_type='Board of Health'.
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
        cell_text = _clean_html_text(_node_text(first_cell))

        # Extract meeting date from <strong> in <h3>
        meeting_date = ""
        for h3 in _find_all(first_cell, "h3"):
            for strong in _find_all(h3, "strong"):
                aria = strong.attrs.get("aria-label", "")
                if aria:
                    dm = re.search(r"(\w+ \d{1,2},? \d{4})", aria)
                    if dm:
                        date_str = dm.group(1)
                        meeting_date = _normalize_text_date(date_str)
                        break
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

        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=meeting_title,
            meeting_type="Board of Health",
            body="health",
            row_text=_clean_html_text(_node_text(row)),
            detail_url="",
            agenda_url=agenda_url,
        ))

    return meetings


# ── Agenda overview / HTML extraction ──


async def extract_health_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract agenda items from a Board of Health meeting.

    Board of Health agenda pages are BOS-style HTML with numbered items in
    a table, or PDF-only. Unlike PZ, there is no overview page that lists
    supporting documents — the agenda itself contains all items.
    """
    is_direct_pdf = "?html=true" not in meeting_url

    html = ""
    base_for_url = _get_health_base_url(meeting_url)

    if not is_direct_pdf:
        try:
            await page.goto(meeting_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            html = await page.content()
        except Exception:
            pass

        if html:
            items = parse_health_agenda_html(html, meeting_url, base_for_url)
            if items:
                return items

    # PDF-only meeting — try to parse the PDF
    pdf_items = await _extract_health_agenda_from_pdf(page, meeting_url, base_for_url)
    if pdf_items:
        return pdf_items

    return []


def _get_health_base_url(agenda_url: str) -> str:
    """Determine the correct base URL for resolving relative links.

    Agenda pages on mcdot.maricopa.gov set <base href="https://www.maricopa.gov/">,
    so relative links need to resolve against that domain.
    """
    return HEALTH_WWW_DOMAIN + "/"


def parse_health_agenda_html(html: str, source_url: str, base_url: str) -> list[dict]:
    """Extract structured agenda items from a Board of Health HTML agenda.

    Board of Health agendas use a BOS-style HTML table format:
      <table>
        <tr><td>1.</td><td><strong>Topic</strong><p>Description</p></td>
            <td>Action/Discuss</td><td>Facilitator</td></tr>
        ...
      </table>

    Returns a list of item dicts compatible with the existing pipeline.
    """
    items: list[dict] = []
    seen_titles: set[str] = set()

    root = _parse_html(html)

    # Find tables inside the main document
    tables = _find_all(root, "table")
    for table in tables:
        # Look for a table with item-numbered rows (Board of Health format)
        rows = _find_all(table, "tr")
        is_agenda_table = False
        header_found = False

        for row in rows:
            cells = _find_all(row, "td")
            if not cells:
                # Check headers
                ths = _find_all(row, "th")
                header_text = " ".join(_clean_html_text(_node_text(th)) for th in ths)
                if "Item" in header_text and "Topic" in header_text:
                    header_found = True
                continue

            # First cell should contain an item number like "1.", "2.", etc.
            first_text = _clean_html_text(_node_text(cells[0])).strip()

            # Skip tables that don't look like agenda tables
            item_num = 0
            m = re.match(r"^(\d+)\.?\s*$", first_text)
            if m:
                item_num = int(m.group(1))
                is_agenda_table = True
            else:
                if not is_agenda_table:
                    continue

            # Extract title from the second cell
            title = ""
            desc_text = ""
            if len(cells) >= 2:
                cell2_html = _node_text(cells[1])
                cell2_text = _clean_html_text(cell2_html)

                # Title is typically in <strong> within the cell
                strongs = _find_all(cells[1], "strong")
                if strongs:
                    title = _clean_html_text(_node_text(strongs[0])).strip()
                else:
                    title = cell2_text.split("\n")[0].strip()

                # Rest is description text
                desc_text = cell2_text

            # Extract type (Action/Discuss) from third cell
            vote_or_action = ""
            if len(cells) >= 3:
                vote_or_action = _clean_html_text(_node_text(cells[2])).strip()

            # Extract facilitator from fourth cell
            facilitator = ""
            if len(cells) >= 4:
                facilitator = _clean_html_text(_node_text(cells[3])).strip()

            if not title:
                title = f"Agenda Item #{item_num}"

            # Build the full item text
            full_text = desc_text
            if facilitator and facilitator not in full_text:
                full_text = f"{full_text}\nFacilitator/Presenter: {facilitator}" if full_text else facilitator
            if vote_or_action and vote_or_action not in full_text:
                full_text = f"{full_text}\nType: {vote_or_action}" if full_text else vote_or_action

            # Deduplicate by title
            title_key = f"{item_num}:{title}"
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            item = {
                "source_body": "Board of Health",
                "meeting_id": "",
                "meeting_date": "",
                "meeting_type": "Board of Health",
                "agenda_item_number": str(item_num),
                "agenda_item_id": "",
                "agenda_item_title": title,
                "agenda_item_text": full_text,
                "agenda_item_url": source_url,
                "vote_or_action": vote_or_action,
                "source_url": source_url,
                "c_number": "",
                "c_number_base": "",
                "c_number_revision": None,
                "case_number": "",
                "supporting_doc_dicts": [],
                "staff_report_url": None,
                "facilitator": facilitator,
            }
            items.append(item)

    # If no items found via numbered rows, try a broader scan
    if not items:
        items = _extract_health_items_from_freeform_html(html, source_url)

    return items


def _extract_health_items_from_freeform_html(html: str, source_url: str) -> list[dict]:
    """Fallback: extract items from free-form HTML agenda pages.

    Some Board of Health meetings use a looser HTML structure.
    """
    items: list[dict] = []
    seen_titles: set[str] = set()

    # Look for numbered paragraphs or list items
    for m in re.finditer(
        r"(?:\A|\n)\s*(?:<[^>]*>)*\s*(\d+)\.\s*(?:</[^>]*>)*\s*<strong[^>]*>(.*?)</strong>",
        html,
        re.DOTALL | re.I,
    ):
        item_num = int(m.group(1))
        title = re.sub(r"<[^>]+>", " ", m.group(2)).strip()

        if not title:
            continue

        title_key = f"{item_num}:{title}"
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        item = {
            "source_body": "Board of Health",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "Board of Health",
            "agenda_item_number": str(item_num),
            "agenda_item_id": "",
            "agenda_item_title": title,
            "agenda_item_text": "",
            "agenda_item_url": source_url,
            "vote_or_action": "",
            "source_url": source_url,
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": "",
            "supporting_doc_dicts": [],
            "staff_report_url": None,
        }
        items.append(item)

    return items


async def _extract_health_agenda_from_pdf(
    page, meeting_url: str, base_url: str
) -> list[dict]:
    """Try to extract agenda items from a PDF-only Board of Health meeting.

    Some meetings are PDF-only (the ?html=true page returns the PDF directly,
    and Playwright raises a 'Download is starting' error).
    """
    import subprocess
    import tempfile
    import urllib.request
    from pathlib import Path

    pdf_url = meeting_url.replace("?html=true", "")

    pdf_path = Path(f"/tmp/health_agenda_{Path(meeting_url).stem}.pdf")
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

    # Parse the PDF
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

    # Extract numbered items
    items: list[dict] = []
    for line in pdf_text.split("\n"):
        m = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if m:
            item_num = int(m.group(1))
            title = m.group(2).strip()
            item = {
                "source_body": "Board of Health",
                "meeting_id": "",
                "meeting_date": "",
                "meeting_type": "Board of Health",
                "agenda_item_number": str(item_num),
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



