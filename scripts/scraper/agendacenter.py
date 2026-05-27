"""Generic Maricopa County AgendaCenter scraper.

Handles any CID (Content ID) on the Maricopa County AgendaCenter platform
(www.maricopa.gov or mcdot.maricopa.gov).  Uses the same catAgendaRow parsing
and year-tab clicking patterns as the PZ/ADJ scrapers but in a generic form
configured by a BODY_MAP.

Supports:
  - CID → body code → body name mapping
  - Domain selection (www.maricopa.gov or mcdot.maricopa.gov)
  - Meeting extraction with year-tab AJAX loading
  - Basic agenda item extraction from overview/detail pages
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.io_utils import _normalize_text_date
from scraper.models import Meeting

# ── Body map ──
# Each entry: body_code → (CID, domain, display_name)
# domain: "www" for www.maricopa.gov, "mcdot" for mcdot.maricopa.gov

MCACC_BODY_MAP: dict[str, tuple[str, str, str]] = {
    # New boards from the task spec (all www.maricopa.gov)
    "mc-audit":            ("48", "www", "Audit Advisory Committee"),
    "mc-benefit-trust":    ("16", "www", "Benefit Board of Trustees"),
    "mc-community-action": ("45", "www", "Community Action Commission"),
    "mc-cdac":             ("10", "www", "Community Development Advisory Committee"),
    "mc-eed-policy":       ("39", "www", "Early Education Division Policy Council"),
    "mc-flood-advisory":   ("14", "www", "Flood Control Advisory Board"),
    "mc-home":             ("12", "www", "HOME Consortium"),
    "mc-mclepc":           ("27", "www", "Local Emergency Planning Committee"),
    "mc-mcao-psprs":       ("55", "www", "MCAO PSPRS Local Board"),
    "mc-mcso-corp":        ("42", "www", "MCSO Correctional Officer Retirement Plan Local Board"),
    "mc-mcso-psprs":       ("43", "www", "MCSO PSPRS Local Board"),
    "mc-merit":            ("15", "www", "Merit Systems Commission"),
    "mc-psfc":             ("57", "www", "Public Safety Funding Committee"),
    "mc-risk-trust":       ("50", "www", "Self-Insured Risk Trust Fund Board of Trustees"),
    "mc-smart-savings":    ("47", "www", "Smart Savings Committee (Deferred Compensation)"),
    "mc-stadium":          ("60", "www", "Stadium District Board"),
    "mc-trp":              ("61", "www", "Travel Reduction Program"),
    "mc-air-pollution":    ("36", "www", "Air Pollution Hearing Board"),
    "mc-bcab":             ("26", "www", "Building Code Advisory Board"),
    "mc-flood-stakeholder":("33", "www", "Flood Control District Stakeholder Group"),
}

# Domains
_DOMAIN_MAP = {
    "www":   "https://www.maricopa.gov",
    "mcdot": "https://mcdot.maricopa.gov",
}

_SEARCH_PATH  = "/AgendaCenter/Search/"
_AGENDA_PATH  = "/AgendaCenter/ViewFile/Agenda/"


def _get_domain_url(domain_key: str) -> str:
    return _DOMAIN_MAP.get(domain_key, "https://www.maricopa.gov")


def body_code_to_cid(body_code: str) -> str | None:
    """Look up a CID string for a given body code.  Returns '{CID},' for use in URLs."""
    entry = MCACC_BODY_MAP.get(body_code)
    if entry:
        return entry[0] + ","
    return None


def body_code_to_name(body_code: str) -> str:
    """Look up display name for a body code."""
    entry = MCACC_BODY_MAP.get(body_code)
    if entry:
        return entry[2]
    return body_code


# ── Date formatting ──


def _format_mm_dd_yyyy(date_iso: str) -> str | None:
    """Convert YYYY-MM-DD to MM/DD/YYYY."""
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


def build_ac_search_url(cid: str, start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for a given CID (including trailing comma)."""
    params = {
        "term": "",
        "CIDs": cid,
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"https://www.maricopa.gov/AgendaCenter/Search/?{qs}"


# ── Year tab extraction ──


def _extract_year_tabs_from_html(html: str) -> list[int]:
    years: set[int] = set()
    for m in re.finditer(r"changeYear\((\d{4})", html):
        years.add(int(m.group(1)))
    return sorted(years)


# ── Meeting extraction ──

def parse_ac_meetings_from_html(html: str, base_url: str, body_code: str) -> list[Meeting]:
    """Parse meetings from AgendaCenter search HTML for any CID body.

    Same catAgendaRow structure as PZ/ADJ.
    """
    root = _parse_html(html)
    meetings: list[Meeting] = []
    display_name = body_code_to_name(body_code)

    rows = _find_all(root, "tr")
    for row in rows:
        classes = (row.attrs.get("class") or "").split()
        if "catAgendaRow" not in classes:
            continue

        cells = _find_all(row, "td")
        if not cells:
            continue

        first_cell = cells[0]

        # Extract meeting date from the <strong> in the <h3>
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
                    meeting_date = _normalize_text_date(
                        f"{dm.group(1)} {dm.group(2)}, {dm.group(3)}"
                    )
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

        # Extract minutes link (from the minutes td cell)
        minutes_url = ""
        if len(cells) >= 2:
            minutes_cell = cells[1]
            for a in _find_all(minutes_cell, "a"):
                href = a.attrs.get("href", "")
                if href:
                    minutes_url = urllib.parse.urljoin(base_url, href)
                    break

        # Extract video/media link
        video_url = ""
        media_cell = cells[2] if len(cells) >= 3 else None
        if media_cell:
            for a in _find_all(media_cell, "a"):
                href = a.attrs.get("href", "")
                if href:
                    video_url = href  # Could be YouTube or other URL
                    break

        # Clean up meeting title: strip location/webinar suffix
        clean_title = re.sub(
            r"\s*[-–—]\s*BOS\s+Auditorium\s*&?\s*(?:Go\s*To)?\s*Web\w*\s*$",
            "", meeting_title
        ).strip()
        if not clean_title:
            clean_title = meeting_title

        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=clean_title,
            meeting_type=display_name,
            body=body_code,
            row_text=_clean_html_text(_node_text(row)),
            detail_url=agenda_url,
            agenda_url=agenda_url,
            minutes_url=minutes_url,
            video_url=video_url,
        ))

    return meetings


async def extract_ac_meetings(page, search_url: str, body_code: str) -> list[Meeting]:
    """Extract meetings from AgendaCenter search results with year-tab clicking.

    Same pattern as PZ/ADJ — the AgendaCenter only shows one year at a time.
    """
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    all_meetings: list[Meeting] = []
    seen_ids: set[str] = set()
    base_for_url = urllib.parse.urljoin(search_url, "/")

    # Collect from initial HTML (default year)
    html = await page.content()
    initial = parse_ac_meetings_from_html(html, base_for_url, body_code)
    all_meetings.extend(initial)
    for m in initial:
        seen_ids.add(m.meeting_id)

    # Iterate over individual years using direct HTTP requests — more reliable
    # than Playwright year-tab clicking, which breaks on CSS selector escaping.
    # The AgendaCenter search API returns results for a single year when the
    # date range is confined to that year.
    parsed = urllib.parse.urlparse(search_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    cids = qs.get("CIDs", [""])[0]
    start_year = int(qs.get("startDate", ["01/01/2023"])[0].split("/")[2])
    end_year = int(qs.get("endDate", ["12/31/2026"])[0].split("/")[2])

    for year in range(start_year, end_year + 1):
        if year in {int(m.meeting_date[:4]) for m in all_meetings}:
            continue
        yr_url = (
            f"https://www.maricopa.gov/AgendaCenter/Search/"
            f"?term=&CIDs={urllib.parse.quote(cids)}&startDate=01/01/{year}&endDate=12/31/{year}"
        )
        try:
            await page.goto(yr_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            yr_html = await page.content()
            yr_meetings = parse_ac_meetings_from_html(yr_html, base_for_url, body_code)
            for m in yr_meetings:
                if m.meeting_id not in seen_ids:
                    all_meetings.append(m)
                    seen_ids.add(m.meeting_id)
        except Exception:
            continue

    return all_meetings


# ── Agenda item extraction ──

async def extract_ac_agenda_items(page, meeting_url: str, body_code: str) -> list[dict]:
    """Extract agenda items from a meeting detail page.

    Many MCACC boards use the same structure as Health board:
    - HTML page with an embedded table of items
    - Or a direct PDF download link
    - Or an overview page (like PZ) with an agenda PDF

    This function tries multiple strategies:
    1. Try to load the HTML overview page (with ?html=true)
    2. Parse HTML table of items (BOS/Health style)
    3. If the URL triggers a PDF download, download and parse it
    4. Find agenda PDF link in overview and parse PDF (PZ/ADJ style)
    5. Extract numbered items from page text
    """
    from scraper.pz import parse_pz_overview

    if not meeting_url:
        return []

    # Try loading the HTML agenda page
    html = ""
    try:
        await page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        html = await page.content()
    except Exception as e:
        err_str = str(e)
        if "Download is starting" in err_str or "net::ERR_ABORTED" in err_str:
            # The URL is a direct PDF download — download and parse
            print(f"      (agenda URL triggers PDF download, downloading directly)")
            return _download_and_parse_pdf(meeting_url)
        else:
            raise

    base_for_url = urllib.parse.urljoin(meeting_url, "/")

    # Strategy 1: Try to find numbered agenda items in the HTML table
    items = _parse_html_agenda_items(html, meeting_url)
    if items:
        return items

    # Strategy 2: Try overview + PDF parsing (PZ/ADJ style)
    overview = parse_pz_overview(html, meeting_url, base_for_url)
    if overview and overview.get("agenda_pdf_url"):
        pdf_url = overview["agenda_pdf_url"]
        pdf_items = _download_and_parse_pdf(pdf_url)
        if pdf_items:
            return pdf_items

    # Strategy 3: Extract anything that looks like an item from the page text
    text_items = _extract_items_from_page_text(html, meeting_url)
    if text_items:
        return text_items

    return []


def _parse_cac_table(html: str, source_url: str) -> list[dict]:
    """Parse a 3-column agenda table (Item, Agenda Item, Presenter).

    Used by Community Action Commission and similar boards on the
    Maricopa County AgendaCenter platform.

    Table format:
      <table>
        <tr><th>Item</th><th>Agenda Item</th><th>Presenter</th></tr>
        <tr><td>1.</td><td>Call to Order</td><td>Danielle Olaya</td></tr>
      </table>
    """
    from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
    import re

    items: list[dict] = []
    root = _parse_html(html)
    tables = _find_all(root, "table")

    for table in tables:
        rows = _find_all(table, "tr")
        is_cac_table = False

        for row in rows:
            cells = _find_all(row, "td")
            if not cells:
                # Check for the CAC header pattern
                ths = _find_all(row, "th")
                header_text = " ".join(_clean_html_text(_node_text(th)) for th in ths).lower()
                if "agenda item" in header_text and "presenter" in header_text:
                    is_cac_table = True
                continue

            if not is_cac_table:
                continue

            # First cell: item number
            first_text = _clean_html_text(_node_text(cells[0])).strip()
            m = re.match(r"^(\d+)\.?\s*$", first_text)
            if not m:
                continue
            item_num = m.group(1)

            # Second cell: agenda item title
            title = ""
            desc = ""
            if len(cells) >= 2:
                title = _clean_html_text(_node_text(cells[1])).strip()

            # Third cell: presenter
            presenter = ""
            if len(cells) >= 3:
                presenter = _clean_html_text(_node_text(cells[2])).strip()

            if not title:
                continue

            full_text = title
            if presenter:
                full_text += f"\n\nPresented by: {presenter}"

            items.append({
                "source_body": "mcacc",
                "meeting_id": "",
                "meeting_date": "",
                "meeting_type": "",
                "agenda_item_number": item_num,
                "agenda_item_id": "",
                "agenda_item_title": title,
                "agenda_item_text": full_text,
                "agenda_item_url": source_url,
                "vote_or_action": "",
                "source_url": source_url,
                "c_number": "",
                "c_number_base": "",
                "c_number_revision": None,
                "case_number": "",
                "supporting_doc_dicts": [],
            })

    return items


def _parse_html_agenda_items(html: str, source_url: str) -> list[dict]:
    """Try to extract numbered agenda items from HTML content.

    Looks for patterns like:
    - <table class="agenda"> with item rows
    - Numbered lists with bold item headers
    - <div class="agenda-item"> structures
    """
    from scraper.health import parse_health_agenda_html
    try:
        items = parse_health_agenda_html(html, source_url, "mcacc", "mcacc")
        if items:
            return items
    except Exception:
        pass

    # Try Community Action Commission / AgencyCenter 3-column table format
    # (Item, Agenda Item, Presenter)
    try:
        items = _parse_cac_table(html, source_url)
        if items:
            return items
    except Exception:
        pass

    # Fallback: look for a simple numbered list structure
    import re
    root = _parse_html(html)
    items: list[dict] = []

    def _make_item(num: str, title: str) -> dict:
        return {
            "source_body": "mcacc",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "",
            "agenda_item_number": num,
            "agenda_item_id": "",
            "agenda_item_title": title[:200],
            "agenda_item_text": title[:500],
            "agenda_item_url": source_url,
            "vote_or_action": "",
            "source_url": source_url,
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": "",
            "supporting_doc_dicts": [],
        }

    # Try to find ordered lists (<ol>) — used by some boards (e.g. TRP with Roman numerals)
    for ol in _find_all(root, "ol"):
        list_style = (ol.attrs.get("style") or "").lower()
        is_roman = "upper-roman" in list_style or "lower-roman" in list_style
        lis = _find_all(ol, "li")
        for idx, li in enumerate(lis):
            li_text = _clean_html_text(_node_text(li)).strip()
            if not li_text or len(li_text) < 3:
                continue
            # Skip sub-items (nested lists)
            if _find_all(li, "ol"):
                sub_title = _clean_html_text(_node_text(li)).strip()
                items.append(_make_item(str(idx + 1), sub_title))
            else:
                items.append(_make_item(str(idx + 1), li_text))
        if items:
            return items

    # Try to find a table with class="agenda" or similar
    for table in _find_all(root, "table"):
        classes = (table.attrs.get("class") or "").split()
        table_classes = " ".join(classes).lower()
        if any(c in table_classes for c in ["agenda", "items", "content", "meeting", "file-list"]):
            rows = _find_all(table, "tr")
            for row in rows:
                cells = _find_all(row, "td")
                if not cells:
                    continue
                cell_text = _clean_html_text(_node_text(row)).strip()
                if not cell_text or len(cell_text) < 10:
                    continue
                # Check if this looks like a numbered item
                num_m = re.match(r"^\s*(\d+)\.?\s", cell_text)
                if num_m:
                    title = cell_text[num_m.end():].strip()
                    items.append(_make_item(num_m.group(1), title))
        if items:
            return items

    return items


def _extract_items_from_page_text(html: str, source_url: str) -> list[dict]:
    """Fallback: find numbered items in page text using regex."""
    import re

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    items: list[dict] = []
    # Look for numbered items: "1. Some Title" or "1) Some Title"
    for m in re.finditer(r"(?:^|\s)(\d+)\.\s+(.{10,200}?)(?=\s+\d+\.\s+|\s*$)", text):
        items.append({
            "source_body": "mcacc",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "",
            "agenda_item_number": m.group(1),
            "agenda_item_id": "",
            "agenda_item_title": m.group(2).strip()[:200],
            "agenda_item_text": m.group(2).strip()[:500],
            "agenda_item_url": source_url,
            "vote_or_action": "",
            "source_url": source_url,
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": "",
            "supporting_doc_dicts": [],
        })

    return items


def _download_and_parse_pdf(pdf_url: str) -> list[dict]:
    """Download a PDF from a URL and try to extract numbered items from it."""
    import tempfile

    if not pdf_url:
        return []

    try:
        pdf_req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_data = pdf_resp.read()
        if len(pdf_data) < 100:
            return []

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = f.name
            f.write(pdf_data)

        items = _parse_ac_agenda_pdf(pdf_path)
        Path(pdf_path).unlink(missing_ok=True)
        return items
    except Exception:
        return []


def _parse_ac_agenda_pdf(filepath: str) -> list[dict]:
    """Generic PDF parser for MCACC agenda PDFs.

    Attempts to extract numbered items from a text-extracted PDF.
    """
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

    import re
    items: list[dict] = []
    seen_numbers: set[str] = set()

    def _make_pdf_item(num: str, title: str) -> dict:
        return {
            "source_body": "mcacc",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "",
            "agenda_item_number": num,
            "agenda_item_id": "",
            "agenda_item_title": title[:200],
            "agenda_item_text": title[:500],
            "agenda_item_url": "",
            "vote_or_action": "",
            "source_url": "",
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": "",
            "supporting_doc_dicts": [],
        }

    def _add(num: str, title: str) -> None:
        if num not in seen_numbers:
            seen_numbers.add(num)
            items.append(_make_pdf_item(num, title))

    # Strategy 1: Line-by-line table-aware matching.
    # The -layout output preserves column positions, so each row of a
    # meeting table appears as a single line with the item number at the start.
    # This handles the HOME Consortium table-layout PDFs correctly.
    for m in re.finditer(
        r"^\s*(\d+)\.\s+([A-Za-z0-9*#\"'\[({-][^\n]{3,200}?)$",
        text, re.MULTILINE
    ):
        _add(m.group(1), m.group(2).strip())

    # Strategy 2: Multi-line matching for PDFs where the item title spans
    # multiple lines (no table layout, just numbered paragraphs).
    if len(items) < 3:
        items.clear()
        seen_numbers.clear()
        for m in re.finditer(
            r"^\s*(\d+)\.\s+([A-Za-z0-9*#\"'\[({].{3,300}?)(?=\n\s*\d+\.\s+|\n\s*\n\s*\d+\.|\n\s*$|\Z)",
            text, re.MULTILINE
        ):
            _add(m.group(1), m.group(2).strip())

    return items


def _download_pdf_text(pdf_url: str) -> str | None:
    """Download a PDF and extract text via pdftotext."""
    import subprocess
    import tempfile

    if not pdf_url:
        return None
    try:
        pdf_req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_data = pdf_resp.read()
        if len(pdf_data) < 100:
            return None
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            pdf_path = f.name
            f.write(pdf_data)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            txt_path = f.name
        subprocess.run(
            ["pdftotext", "-layout", pdf_path, txt_path],
            capture_output=True, timeout=30,
        )
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        Path(pdf_path).unlink(missing_ok=True)
        Path(txt_path).unlink(missing_ok=True)
        return text
    except Exception:
        return None


def extract_members_from_minutes_text(text: str) -> list[dict]:
    """Extract member names and roles from MCACC minutes PDF text.

    MCACC minutes follow a standard format with sections for:
    - Voting Members Present: name, District X
    - Voting Members Absent: District X (or name, District X)
    - Non-Voting Members Present: name, title
    - Non-Voting Members Absent: name, title

    Returns a list of dicts with keys:
      name, normalized_name, role, attendance_status, voting_status (voting|non-voting)
    """
    if not text:
        return []

    members: list[dict] = []
    seen_names: set[str] = set()

    # Section headers to capture (in display order within minutes)
    section_patterns = [
        (r"Voting\s+Members\s+Present:", "voting", "present"),
        (r"Voting\s+Members\s+Absent:", "voting", "absent"),
        (r"Non-Voting\s+Members\s+Present:", "non-voting", "present"),
        (r"Non-Voting\s+Members\s+Absent:", "non-voting", "absent"),
        (r"Interested\s+Persons\s+Present:", "interested", "present"),
    ]
    # Section terminator patterns — stop collecting when we see these
    _TERMINATORS = [
        r"^\d+\.\s+",
        r"^Call\s+(?:Meeting|to)",
        r"^The\s+meeting\s+was\s+called",
        r"^Approved\s+by:",
        r"^Prepared\s+by:",
        r"\.\.\/\/",
        r"^MARICOPA\s+COUNTY",
    ]

    lines = text.split("\n")
    current_section: str | None = None
    current_status: str | None = None
    collecting = False

    def _is_terminator(line: str) -> bool:
        for pat in _TERMINATORS:
            if re.search(pat, line):
                return True
        return False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for section header
        found_header = False
        for pattern, section_type, status in section_patterns:
            if re.search(pattern, stripped, re.IGNORECASE):
                current_section = section_type
                current_status = status
                collecting = True
                found_header = True
                break
        if found_header:
            continue

        # Stop collecting when we hit numbered agenda items or terminator lines
        if collecting and _is_terminator(stripped):
            collecting = False

        if collecting:
            result = _extract_member_line(stripped)
            if result:
                name, norm, role = result
                if norm and norm not in seen_names:
                    seen_names.add(norm)
                    members.append({
                        "name": name,
                        "normalized_name": norm,
                        "role": role,
                        "attendance_status": current_status or "present",
                        "voting_status": current_section or "voting",
                    })

    return members


def _extract_member_line(line: str) -> tuple[str, str, str] | None:
    """Extract (name, normalized_name, role) from a minutes member line.

    Handles formats:
    - "Stacey Linch, District 1" → ("Stacey Linch", "stacey linch", "District 1")
    - "Michael McGee, County Chief Financial Officer"
    - "District 4"  (unnamed — returns None)
    - "District 4                           Katherine Edwards Decker"  (layout artifact)
    """
    # Skip pure district references like "District 4" alone
    if re.match(r"^District\s+\d+\s*$", line, re.IGNORECASE):
        return None

    # Try to extract "name, role" pattern
    if "," in line:
        parts = [p.strip() for p in line.split(",", 1)]
        name_part = parts[0] if parts[0] else None
        role_part = parts[1].strip() if len(parts) > 1 else ""
        # Validate: must be at least 2 words or contain a capitalized first/last name
        if name_part and _looks_like_name(name_part):
            return name_part, name_part.lower().strip(), role_part

    # Single-line name without comma (less common in MCACC)
    if _looks_like_name(line):
        return line, line.lower().strip(), ""

    return None


def _looks_like_name(text: str) -> bool:
    """Heuristic: does this text fragment look like a person's name?

    Must have 2+ words, each starting with capital letter, no digits.
    Avoids picking up agenda items, motion text, etc.
    """
    words = text.strip().split()
    if len(words) < 2:
        return False
    if len(words) > 6:
        return False
    for ch in text:
        if ch.isdigit():
            return False
    # All words must start uppercase (proper names)
    if not all(w[0].isupper() for w in words if w):
        return False
    return True


def extract_minutes_outcomes(text: str) -> list[dict]:
    """Extract agenda item outcomes from meeting minutes text.

    Scans the text for keywords like "motion", "approved", "adopted",
    "carried", "failed", "denied", "received" near item numbers.

    Returns list of dicts: {agenda_item_number, outcome, context}
    """
    import re
    outcomes: list[dict] = []

    # Known outcome keywords
    outcome_kws = re.compile(
        r"(motion\s+(?:made|by|carried|failed|approved|denied|adopted|passed|tabled)|"
        r"approved|adopted|carried|failed|denied|received|ratified|accepted|"
        r"unanimously|majority)", re.I
    )

    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Look for item number references near outcome keywords
        item_match = re.search(r"Item\s+#?(\d+)", stripped, re.I)
        kw_match = outcome_kws.search(stripped)

        if kw_match:
            item_num = item_match.group(1) if item_match else ""
            outcome = kw_match.group(0).strip()

            # Normalize outcome
            outcome_lower = outcome.lower()
            if any(w in outcome_lower for w in ["carried", "approved", "adopted", "ratified", "accepted"]):
                norm = "Approved"
            elif any(w in outcome_lower for w in ["failed", "denied", "rejected", "tabled"]):
                norm = "Failed"
            elif "received" in outcome_lower or "unanimously" in outcome_lower:
                norm = outcome
            else:
                norm = outcome

            outcomes.append({
                "agenda_item_number": item_num,
                "outcome": norm,
                "context": stripped[:300],
            })

    return outcomes


def extract_members_from_minutes_pdf(pdf_url: str) -> list[dict]:
    """Download MCACC minutes PDF and extract member names/roles."""
    text = _download_pdf_text(pdf_url)
    return extract_members_from_minutes_text(text)


# ── MCACC body registration ──

# Map from MCACC body_code to (slug, name) for public_bodies registration.
# The slug follows the pattern "mc-{name}" matching the body_code.
MCACC_PUBLIC_BODY_REGISTRATIONS: dict[str, tuple[str, str]] = {
    code: (code, name) for code, (_, _, name) in MCACC_BODY_MAP.items()
}

MCACC_JURISDICTION_SLUG = "maricopa-county"


def ensure_agendacenter_public_bodies(session) -> dict[str, int]:
    """Register all MCACC bodies in the public_bodies table if not present.

    Returns a dict mapping body_code → public_body_id.
    """
    from sqlalchemy import select
    from db.models import PublicBody, Jurisdiction

    # Resolve Maricopa County jurisdiction
    jur = session.execute(
        select(Jurisdiction).where(Jurisdiction.slug == MCACC_JURISDICTION_SLUG)
    ).scalar_one_or_none()
    if not jur:
        # Try to create it
        jur = Jurisdiction(
            name="Maricopa County",
            slug=MCACC_JURISDICTION_SLUG,
            state="AZ",
        )
        session.add(jur)
        session.flush()

    pb_map: dict[str, int] = {}
    for body_code, (slug, display_name) in MCACC_PUBLIC_BODY_REGISTRATIONS.items():
        existing = session.execute(
            select(PublicBody).where(PublicBody.body_code == body_code)
        ).scalar_one_or_none()
        if existing:
            pb_map[body_code] = existing.id
        else:
            pb = PublicBody(
                jurisdiction_id=jur.id,
                name=display_name,
                slug=slug,
                body_code=body_code,
                body_type="advisory_board",
            )
            session.add(pb)
            session.flush()
            pb_map[body_code] = pb.id

    return pb_map


# ── Multi-body sync helpers ──

MCACC_BODY_CODES = list(MCACC_BODY_MAP.keys())
