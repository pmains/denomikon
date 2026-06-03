"""
City of Tucson Mayor & Council (City Council) meeting extraction via OnBase
Agenda Online.

Tucson uses OnBase at ``tucsonaz.hylandcloud.com/221agendaonline`` with a
POST-based search (like Tempe) that requires a CSRF token.

Meeting types in the OnBase instance:
  - 107: Regular Meeting
  - 108: Regular Meeting Addendum
  - 109: Regular Special Meeting
  - 110: Study Session
  - 111: Study Session Addendum

Bodies on this OnBase instance:
  - Mayor & Council (City Council) — all meeting types
  - Public Housing Authority Board of Commissioners — Regular only

Usage:
    ./scrape tucson --sync [--start-date=YYYY-MM-DD] [--end-date=YYYY-MM-DD]
    ./scrape tucson --sync --year=2026
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from scraper.onbase import (
    TUCSON_CONFIG,
    _do_search,
    fetch_agenda_sync,
    fetch_item_details_sync,
    parse_agenda_html,
    parse_item_details,
    parse_meetings_from_html,
)

log = logging.getLogger(__name__)

# ── Identifiers ──
JURISDICTION_ID = 8          # City of Tucson
# Naming note: the Tucson City Council is titled 'Mayor & Council' on the
# OnBase instance, but we use 'Tucson City Council' as the body name for
# consistency with other jurisdictions.
PUBLIC_BODY_CODE = "tucson-cc"
PUBLIC_BODY_CODE_PHA = "tucson-pha"

# All meeting type IDs on this OnBase instance
ALL_TYPE_IDS = [107, 108, 109, 110, 111]

# Meeting type IDs for City Council only
COUNCIL_TYPE_IDS = [107, 108, 109, 110, 111]

# Mapping from OnBase meeting type ID to our body code
ONBASE_TYPE_TO_BODY = {
    107: PUBLIC_BODY_CODE,   # Regular Meeting — Mayor & Council
    108: PUBLIC_BODY_CODE,   # Regular Meeting Addendum — Mayor & Council
    109: PUBLIC_BODY_CODE,   # Regular Special Meeting — Mayor & Council
    110: PUBLIC_BODY_CODE,   # Study Session — Mayor & Council
    111: PUBLIC_BODY_CODE,   # Study Session Addendum — Mayor & Council
}

# Body codes derived from meeting title patterns
BODY_CODES_BY_TITLE = {
    "mayor & council": PUBLIC_BODY_CODE,
    "public housing authority board of commissioners": PUBLIC_BODY_CODE_PHA,
}

# Default body code when title-based matching fails
DEFAULT_BODY_CODE = PUBLIC_BODY_CODE


def extract_body_code_from_title(title: str) -> str:
    """Derive the public body code from the meeting title.

    Tucson OnBase meeting titles include the body name, e.g.
    'Mayor & Council - Regular', 'Mayor & Council - Study Session',
    'Public Housing Authority Board of Commissioners - Regular Meeting'.
    """
    title_lower = title.lower().strip()
    for pattern, body_code in BODY_CODES_BY_TITLE.items():
        if pattern in title_lower:
            return body_code
    return DEFAULT_BODY_CODE


def normalize_meeting_type(raw_type: str) -> str:
    """Normalize a Tucson meeting type to a canonical form."""
    # Strip cancel/reschedule prefixes
    t = raw_type.replace("\u2013", "-").strip()
    t = re.sub(r"^CANCEL(?:LED|ED)?\s*-\s*", "", t, count=1, flags=re.IGNORECASE)
    t = re.sub(r"^RESCHEDULED TO \d{1,2}/\d{1,2}/\d{4}\s*-\s*", "", t, count=1, flags=re.IGNORECASE)
    t = re.sub(r"^\s*-\s+", "", t)
    t = t.strip()

    # Canonicalize common variants
    t_lower = t.lower()
    if "regular" in t_lower:
        return "Regular Meeting"
    if "study session" in t_lower or "study" in t_lower:
        return "Study Session"
    if "special" in t_lower:
        return "Special Meeting"
    if "retreat" in t_lower:
        return "Retreat"
    if "executive" in t_lower:
        return "Executive Session"
    if "addendum" in t_lower or "addendum" in t_lower:
        return "Addendum"
    return t


async def search_tucson_meetings(page, start_date: str, end_date: str,
                                  meeting_type_ids: Optional[list[int]] = None) -> list[dict]:
    """Search for Tucson meetings via OnBase POST search.

    Parameters
    ----------
    page : playwright Page (may be None; search uses urllib directly)
    start_date : str in MM/DD/YYYY format
    end_date : str in MM/DD/YYYY format
    meeting_type_ids : list[int], optional. Defaults to all type IDs.

    Returns
    -------
    list[dict] — meeting dicts with keys: meeting_id, meeting_date,
        meeting_time, meeting_title, meeting_type, body, etc.
    """
    if meeting_type_ids is None:
        meeting_type_ids = ALL_TYPE_IDS

    meetings = _do_search(
        TUCSON_CONFIG, start_date, end_date,
        meeting_type_ids=meeting_type_ids,
        public_body_code=DEFAULT_BODY_CODE,
    )

    # Assign per-meeting body code based on title
    for m in meetings:
        title = m.get("meeting_title", "")
        body_code = extract_body_code_from_title(title)
        m["body"] = body_code

        raw_type = m.get("meeting_type", "")
        m["meeting_type"] = normalize_meeting_type(raw_type)

    return meetings


async def extract_tucson_agenda_items(page, agenda_url: str) -> list[dict]:
    """Extract agenda items from a Tucson meeting's agenda page.

    Parameters
    ----------
    page : playwright Page
    agenda_url : str — URL to the meeting agenda view

    Returns
    -------
    list[dict] — agenda item dicts
    """
    mid_match = re.search(r"[?&]meetingId=(\d+)", agenda_url)
    if not mid_match:
        mid_match = re.search(r"[?&]id=(\d+)", agenda_url)
    if not mid_match:
        log.warning("Could not extract meeting ID from agenda URL: %s", agenda_url)
        return []
    meeting_id = mid_match.group(1)

    html = fetch_agenda_sync(TUCSON_CONFIG, int(meeting_id))
    items = parse_agenda_html(html, meeting_id, PUBLIC_BODY_CODE)

    for item in items:
        item["source_url"] = agenda_url
        item["body"] = PUBLIC_BODY_CODE

    # Assign categories based on level-1 section headings
    _assign_tucson_categories(items)

    return items


def _assign_tucson_categories(items: list[dict]) -> None:
    """Walk items and set ``agenda_category`` based on enclosing level-1 section."""
    current_category = ""
    for item in items:
        level = item.get("section_level", 0) or 0
        title = (item.get("agenda_item_title") or "").strip().upper()

        if level == 1:
            if title == "CONSENT AGENDA":
                current_category = "Consent"
            elif title == "NON-CONSENT AGENDA" or title == "REGULAR AGENDA":
                current_category = "Non-Consent"
            elif title.startswith("CALL TO ORDER"):
                current_category = "Call to Order"
            elif title.startswith("PUBLIC COMMENT") or title.startswith("PUBLIC APPEARANCES"):
                current_category = "Public Comment"
            elif title.startswith("REPORTS") or title.startswith("DISCUSSION"):
                current_category = "Discussion / Report"
            elif title.startswith("ADJOURNMENT"):
                current_category = "Adjournment"
            elif "MEETING MINUTES" in title or title.startswith("MINUTES"):
                current_category = "Meeting Minutes"
            else:
                current_category = title

        item["agenda_category"] = current_category


# ── Tucson Planning Commission (direct page + PDF scraping) ──

PC_PAGE_URL = (
    "https://www.tucsonaz.gov/Departments/Planning-Development-Services/"
    "Public-Meetings-Boards-Committees-Commissions/Planning-Commission"
)
PUBLIC_BODY_CODE_PC = "tucson-pc"
PC_JURISDICTION_ID = JURISDICTION_ID  # Same jurisdiction (City of Tucson)

PC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fetch_html(url: str, page: Optional[object] = None) -> str:
    """Fetch a URL and return the text content.

    Strategy:
      1. Use Playwright page.content() if a ``page`` object is provided.
      2. Use curl (via subprocess) — handles Akamai CDN better than urllib.
      3. Fallback to urllib.

    Parameters
    ----------
    url : str — URL to fetch
    page : object, optional — a Playwright Page object for browser-based fetch

    Returns
    -------
    str — page HTML content
    """
    # Strategy 1: Playwright page (accepts Akamai CDN)
    if page is not None:
        try:
            import asyncio
            coro = page.goto(url, wait_until="networkidle", timeout=30000)
            if asyncio.iscoroutine(coro):
                asyncio.get_event_loop().run_until_complete(coro)
            html = page.content()
            if asyncio.iscoroutine(html):
                html = asyncio.get_event_loop().run_until_complete(html)
            return html
        except RuntimeError:
            # event loop already running — caller must await
            raise
        except Exception as e:
            log.warning("Playwright fetch failed for %s: %s", url, e)

    # Strategy 2: curl
    import subprocess
    try:
        result = subprocess.run(
            [
                "curl", "-sL",
                "-A", PC_HEADERS["User-Agent"],
                "-H", "Accept: " + PC_HEADERS.get("Accept", "text/html"),
                "--max-time", "30",
                url,
            ],
            capture_output=True, text=True, timeout=35,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        log.debug("curl fetch failed for %s: %s", url, e)

    # Strategy 3: urllib
    import urllib.request
    req = urllib.request.Request(url, headers=PC_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("All fetch strategies failed for %s: %s", url, e)
        raise


def _download_pdf_bytes(url: str) -> Optional[bytes]:
    """Download PDF bytes from a URL."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "curl", "-sL",
                "-A", PC_HEADERS["User-Agent"],
                "-H", "Accept: " + PC_HEADERS.get("Accept", "text/html"),
                "--max-time", "30",
                url,
            ],
            capture_output=True, timeout=35,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        log.debug("curl download failed for %s: %s", url, e)

    # Fallback to urllib
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=PC_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Failed to download PDF %s: %s", url, e)
        return None


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Convert PDF bytes to plain text using pdftotext or pypdf fallback."""
    import subprocess, tempfile
    from pathlib import Path

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        Path(pdf_path).unlink(missing_ok=True)
        if result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        Path(pdf_path).unlink(missing_ok=True)

    # Fallback to pypdf
    try:
        from io import BytesIO
        from pypdf import PdfReader as _PdfReader
        reader = _PdfReader(BytesIO(pdf_bytes))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception as e:
        log.warning("pypdf fallback also failed: %s", e)
        return ""


def _url_to_meeting_id(pdf_url: str, meeting_date: Optional[str] = None) -> str:
    """Derive a unique meeting ID from the meeting date.

    Uses the normalized meeting date (YYYYMMDD) which is inherently unique
    since the Planning Commission has at most one meeting per day.
    """
    if meeting_date:
        return meeting_date.replace("-", "")
    # Fallback: extract folder name from URL
    m = re.search(r"/([^/]+?)/[^/]+\.pdf$", pdf_url)
    if m:
        folder = m.group(1).strip()
        return folder.replace(".", "-").replace("_", "-")
    return f"tucson-pc-{hash(pdf_url) % 10000}"


def search_tucson_pc_meetings(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Search for Tucson Planning Commission meetings by scraping the listing page.

    Downloads the page via curl/urllib. If the Akamai CDN blocks request,
    returns empty list. In production, the main.py sync block should
    fetch the HTML via Playwright and call ``parse_tucson_pc_meetings``.

    The page at PC_PAGE_URL lists all past and upcoming meetings with direct
    PDF agenda links. Each meeting entry looks like:
      <a href="...6.3.26/06-03-26-agenda.pdf">6/3/2026 Agenda</a>
      ...
      <a href="...5.20.26/...cancellation-notice.pdf">5/20/2026 Agenda (Cancellation)</a>

    Parameters
    ----------
    start_date : str, optional — YYYY-MM-DD filter
    end_date : str, optional — YYYY-MM-DD filter

    Returns
    -------
    list[dict] with keys:
      meeting_id, meeting_date (YYYY-MM-DD), source_url (agenda PDF),
      meeting_type, canceled
    """
    import datetime as _dt
    try:
        html = _fetch_html(PC_PAGE_URL)
    except Exception:
        log.warning("Failed to fetch Tucson PC page (likely Akamai block)")
        return []
    return parse_tucson_pc_meetings_from_html(html, start_date, end_date)


def parse_tucson_pc_meetings_from_html(
    html: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Parse Tucson PC meeting list from pre-fetched HTML.

    Use this function when the HTML was already fetched via Playwright
    (to bypass Akamai CDN).

    The page at PC_PAGE_URL lists all past and upcoming meetings with direct
    PDF agenda links. Each meeting entry looks like:
      <a href="...6.3.26/06-03-26-agenda.pdf">6/3/2026 Agenda</a>
      ...
      <a href="...5.20.26/...cancellation-notice.pdf">5/20/2026 Agenda (Cancellation)</a>

    Parameters
    ----------
    html : str — the full page HTML
    start_date : str, optional — YYYY-MM-DD filter
    end_date : str, optional — YYYY-MM-DD filter

    Returns
    -------
    list[dict] with keys:
      meeting_id, meeting_date (YYYY-MM-DD), source_url (agenda PDF),
      meeting_type, canceled
    """
    import datetime as _dt

    # Find the meeting list section: look for <h2>Meeting Date/Agendas and Materials</h2>
    # then capture everything until the next <h2> or end of content.
    meeting_section = ""
    m_start = re.search(
        r'<h2[^>]*>Meeting Date/Agendas and Materials</h2>',
        html, re.IGNORECASE,
    )
    if m_start:
        sec_start = m_start.end()
        next_h2 = re.search(r'<h2[^>]*>', html[sec_start:])
        if next_h2:
            meeting_section = html[sec_start:sec_start + next_h2.start()]
        else:
            meeting_section = html[sec_start:]

    if not meeting_section:
        meeting_section = html  # fall back to full page

    # Parse individual meeting entries.
    # Some meetings appear twice (once as a cancellation notice, once as a
    # struck-through original agenda). We deduplicate by normalized_date and
    # prefer the cancellation PDF when both exist for the same date.
    meetings_by_date: dict[str, dict] = {}

    # Regex: match an <a> tag with href pointing to a PDF,
    # then optional inline tags, then date + "Agenda" + optional "(Cancellation)"
    # Also capture the text between this meeting entry and the next one
    # for extracting supporting documents.
    pc_pattern = re.compile(
        r'<a\s+[^>]*href="([^"]+\.pdf)"[^>]*>'  # capture PDF URL
        r'(?:<[^>]+>)*\s*'
        r'(\d{1,2}/\d{1,2}/\d{4})'  # capture date like 6/3/2026
        r'\s+Agenda'
        r'(?:\s*\((Cancellation)\))?',  # optional (Cancellation)
        re.IGNORECASE,
    )

    # Pattern for item and attachment links within each meeting block
    # Matches links that contain Item, Attachment, or Minutes in their full
    # inner HTML (after stripping HTML tags). The inner content may include
    # <i>, <span>, and other inline elements.
    doc_pattern = re.compile(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>'
        r'(.*?)'
        r'</a>',
        re.IGNORECASE | re.DOTALL,
    )

    # Find all meeting match positions to extract document blocks
    all_matches = list(pc_pattern.finditer(meeting_section))

    for i, m in enumerate(all_matches):
        pdf_url = m.group(1).strip()
        date_str = m.group(2)
        cancellation = bool(m.group(3))

        # Build absolute URL if needed
        if pdf_url.startswith("/"):
            pdf_url = "https://www.tucsonaz.gov" + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = "https://www.tucsonaz.gov/" + pdf_url.lstrip("/")

        # Normalize meeting date to YYYY-MM-DD
        parts = date_str.split("/")
        month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
        normalized_date = f"{year:04d}-{month:02d}-{day:02d}"

        # Apply date filter
        if start_date and normalized_date < start_date:
            continue
        if end_date and normalized_date > end_date:
            continue

        meeting_id = _url_to_meeting_id(pdf_url, normalized_date)

        # Deduplicate by date — prefer cancellation entry
        if normalized_date in meetings_by_date:
            existing = meetings_by_date[normalized_date]
            if cancellation and not existing["canceled"]:
                pass  # Replace with cancellation entry
            else:
                continue  # Keep existing

        meeting_type = "Regular Meeting" if not cancellation else "Cancelled"

        # Extract supporting documents from the block between this
        # meeting entry and the next one, with hierarchical item matching.
        # Top-level <li> with "Item #N" = parent item.
        # Nested <li> inside a nested <ul> = attachment of preceding parent.
        supporting_docs = []
        block_start = m.end()
        if i + 1 < len(all_matches):
            block_end = all_matches[i + 1].start()
        else:
            block_end = len(meeting_section)
        meeting_block = meeting_section[block_start:block_end]
        
        # Track <ul> depth to understand item vs attachment nesting
        _ul_depth = 0
        _current_item = ""
        _pos = 0
        while _pos < len(meeting_block):
            if meeting_block[_pos:_pos+4] == '<ul ' or meeting_block[_pos:_pos+4] == '<ul>':
                _ul_depth += 1
                _pos += 4
                continue
            if meeting_block[_pos:_pos+5] == '</ul>':
                _ul_depth -= 1
                _pos += 5
                continue
            
            # Find <li> tags
            _is_li = False
            if meeting_block[_pos:_pos+3] == '<li':
                _next_char = meeting_block[_pos+3] if _pos+3 < len(meeting_block) else ''
                if _next_char in ('>', ' ', '\n', '\t', '\r'):
                    _is_li = True
            
            if _is_li:
                # Find the matching </li> accounting for nested <ul>/</ul>
                _li_start = _pos
                _li_open = 0
                _j = _li_start + 3
                while _j < len(meeting_block):
                    if meeting_block[_j:_j+4] == '<ul>' or meeting_block[_j:_j+5] == '<ul ':
                        _li_open += 1
                    if meeting_block[_j:_j+5] == '</ul>':
                        _li_open -= 1
                    if meeting_block[_j:_j+5] == '</li>' and _li_open == 0:
                        _li_content = meeting_block[_li_start:_j+5]
                        break
                    _j += 1
                
                _is_top_level = (_ul_depth == 1)
                _has_nested = '<ul' in _li_content
                
                # Extract link from this li
                _a_m = re.search(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', _li_content, re.DOTALL)
                if _a_m:
                    _a_url = _a_m.group(1).strip()
                    _a_inner = _a_m.group(2)
                    _a_title = re.sub(r'<[^>]+>', '', _a_inner).strip()
                    _a_title = re.sub(r'\s+', ' ', _a_title)
                    _a_title_clean = re.sub(r'\s*\(PDF,\s*\d+\s*KB\)', '', _a_title).strip()
                    
                    _item_match = re.search(r'Item #(\d+[A-Z]*)', _a_title_clean, re.IGNORECASE)
                    
                    if _is_top_level and _item_match:
                        _current_item = _item_match.group(1)
                        _a_type = "Meeting Minutes" if re.search(r'Minutes|Legal Action', _a_title_clean, re.IGNORECASE) else "Supporting Document"
                        # Build absolute URL
                        if _a_url.startswith("/"):
                            _a_url = "https://www.tucsonaz.gov" + _a_url
                        elif not _a_url.startswith("http"):
                            _a_url = "https://www.tucsonaz.gov/" + _a_url.lstrip("/")
                        supporting_docs.append({
                            "agenda_item_number": _current_item,
                            "document_title": _a_title_clean,
                            "document_url": _a_url,
                            "document_type": _a_type,
                        })
                    
                    if _has_nested:
                        # Extract nested attachments
                        _nested_ul = re.search(r'<ul[^>]*>(.*?)</ul>', _li_content, re.DOTALL)
                        if _nested_ul:
                            for _a in re.finditer(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', _nested_ul.group(1), re.DOTALL):
                                _att_url = _a.group(1).strip()
                                _att_inner = _a.group(2)
                                _att_title = re.sub(r'<[^>]+>', '', _att_inner).strip()
                                _att_title = re.sub(r'\s+', ' ', _att_title)
                                _att_title_clean = re.sub(r'\s*\(PDF,\s*\d+\s*KB\)', '', _att_title).strip()
                                _att_type = "Meeting Minutes" if re.search(r'Minutes|Legal Action', _att_title_clean, re.IGNORECASE) else "Supporting Document"
                                if _att_url.startswith("/"):
                                    _att_url = "https://www.tucsonaz.gov" + _att_url
                                elif not _att_url.startswith("http"):
                                    _att_url = "https://www.tucsonaz.gov/" + _att_url.lstrip("/")
                                supporting_docs.append({
                                    "agenda_item_number": _current_item,
                                    "document_title": _att_title_clean,
                                    "document_url": _att_url,
                                    "document_type": _att_type,
                                })
                
                _pos = _j + 5
            else:
                _pos += 1

        meetings_by_date[normalized_date] = {
            "meeting_id": meeting_id,
            "meeting_date": normalized_date,
            "source_url": pdf_url,
            "meeting_type": meeting_type,
            "canceled": cancellation,
            "body": PUBLIC_BODY_CODE_PC,
            "supporting_documents": supporting_docs,
        }

    meetings = sorted(meetings_by_date.values(), key=lambda m: m["meeting_date"])
    return meetings


def extract_tucson_pc_agenda_items(pdf_url: str) -> list[dict]:
    """Download a Tucson PC agenda PDF and extract numbered agenda items.

    Uses curl/urllib for download. If the PDF is served from the same
    Akamai-protected domain, downloads may fail. See
    ``extract_items_from_pdf_bytes`` for a version that accepts
    pre-downloaded bytes (e.g. downloaded via Playwright).
    """
    pdf_bytes = _download_pdf_bytes(pdf_url)
    if not pdf_bytes:
        return []
    meeting_id = _url_to_meeting_id(pdf_url)
    return _parse_agenda_items_from_pdf_bytes(pdf_bytes, meeting_id)


def extract_items_from_pdf_bytes(pdf_bytes: bytes, meeting_id: str) -> list[dict]:
    """Extract agenda items from pre-downloaded PDF bytes.

    Parameters
    ----------
    pdf_bytes : bytes — raw PDF content
    meeting_id : str — the meeting identifier

    Returns
    -------
    list[dict] — parsed agenda items
    """
    return _parse_agenda_items_from_pdf_bytes(pdf_bytes, meeting_id)


def _parse_agenda_items_from_pdf_bytes(pdf_bytes: bytes, meeting_id: str) -> list[dict]:
    """Parse numbered agenda items from Tucson PC PDF content.

    Tucson PC agenda PDFs have this format:
      AGENDA ITEMS:

      1. Roll Call

      2. Approval of Minutes/Legal Action Report - Date                  Action

      3. Item Title                                       Public Hearing
    """
    import datetime as _dt

    text = _pdf_to_text(pdf_bytes)
    if not text:
        return []

    items: list[dict] = []
    sort_order = 0

    # Look for "AGENDA ITEMS:" section — parse from there
    lines = text.splitlines()

    # Find the start of agenda items
    start_idx = None
    for i, line in enumerate(lines):
        if "AGENDA ITEMS" in line.upper():
            start_idx = i + 1
            break

    if start_idx is None:
        # Fallback: parse from the beginning
        start_idx = 0

    for i in range(start_idx, len(lines)):
        line = lines[i].strip()
        if not line:
            continue

        # Check if we've entered the projected items / adjournment section
        if line.upper().startswith("PROJECTED UPCOMING") or line.upper().startswith("DISCUSSION AND/OR ACTION"):
            break
        if "If you require an accommodation" in line:
            break

        # Match numbered items: "1. Roll Call" or " 1. Roll Call"
        item_match = re.match(r"^(\d+)\.\s+(.*)", line)
        if not item_match:
            continue

        item_number = item_match.group(1)
        remainder = item_match.group(2).strip()

        # The remainder may contain "ItemTitle                                              ActionType"
        # where the action type is right-aligned (separated by lots of whitespace).
        # Split on 3+ spaces to separate title from action type.
        title = remainder
        action_type = ""
        parts = re.split(r"\s{3,}", remainder, maxsplit=1)
        if len(parts) == 2:
            title = parts[0].strip()
            action_type = parts[1].strip()

        # Some PDFs put the action type on the NEXT line (non-layout extraction).
        # Check the next non-blank line.
        if not action_type:
            for j in range(i + 1, min(i + 5, len(lines))):
                next_line = lines[j].strip()
                if not next_line:
                    continue
                # If next line is NOT a new item number
                if not re.match(r"^\d+\.", next_line):
                    if re.match(r"^(Action|Study Session|Public Hearing|Information|Discussion|Presentation)",
                                next_line, re.IGNORECASE):
                        action_type = next_line
                        lines[j] = ""  # mark as consumed
                break

        # Determine item_type
        action_lower = action_type.lower()
        if action_lower in ("action", "discussion"):
            item_type = "action"
        elif "hearing" in action_lower:
            item_type = "public-hearing"
        elif "study" in action_lower:
            item_type = "study-session"
        elif action_lower in ("information", "report"):
            item_type = "information"
        else:
            item_type = ""

        sort_order += 1
        items.append({
            "meeting_id": meeting_id,
            "agenda_item_number": item_number,
            "agenda_item_title": title,
            "agenda_item_text": action_type,
            "vote_or_action": action_type,
            "item_type": item_type,
            "agenda_category": "",
            "sort_order": sort_order,
        })

    return items
