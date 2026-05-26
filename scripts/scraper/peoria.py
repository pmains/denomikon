"""
City of Peoria agenda extraction via NovusAgenda.

Peoria uses the NovusAgenda platform at ``peoriaaz.novusagenda.com/agendapublic/``.

The search page (Default.aspx) is a Telerik ASP.NET WebForms page.  It renders a
RadGrid of meetings with columns for date, body name, location, and links to
HTML agendas, PDF agendas, minutes, and legal minutes.

The search is a POST back to the same page with ``__VIEWSTATE``,
``__VIEWSTATEGENERATOR``, and form fields:
  - ``ddlDateRange`` (predefined date spans like ``l6m``, ``lyr``, ``6ms``)
  - ``ctl00`` (body / committee ID: 1=City Council, 2=Planning & Zoning, etc.)
  - ``imageButtonSearch`` (click to trigger the search)

Meeting detail pages at ``MeetingView.aspx?MeetingID=X`` show the full agenda
HTML, and ``CoverSheet.aspx?ItemID=X&MeetingID=X`` shows individual agenda-item
details with attachments.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
import urllib.parse
import urllib.request
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text

log = logging.getLogger(__name__)

# ── Jurisdiction / body constants ──

JURISDICTION_ID = 10  # City of Peoria
SOURCE_SYSTEM = "novusagenda"
SOURCE_INSTANCE_URL = "https://peoriaaz.novusagenda.com/agendapublic"

# ── Public body code mapping ──
# Maps NovusAgenda committee ID → our internal slug.

BODY_CODE_MAP: dict[str, int] = {
    "peoria-board-of-adjustment": 5,
    "peoria-city-council": 1,
    "peoria-design-review-board": 6,
    "peoria-historic-preservation-commission": 7,
    "peoria-planning-and-zoning-commission": 2,
    "peoria-planning-and-zoning-special-meeting": 4,
    "peoria-subcommittee-meeting": 3,
}

# Reverse: NovusAgenda numeric committee ID → slug
_BODY_ID_TO_SLUG: dict[int, str] = {
    5: "peoria-board-of-adjustment",
    1: "peoria-city-council",
    6: "peoria-design-review-board",
    7: "peoria-historic-preservation-commission",
    2: "peoria-planning-and-zoning-commission",
    4: "peoria-planning-and-zoning-special-meeting",
    3: "peoria-subcommittee-meeting",
}

# Reverse: NovusAgenda numeric committee ID → body name (as rendered in HTML)
_BODY_ID_TO_NAME: dict[int, str] = {
    5: "Board of Adjustment",
    1: "City Council Meetings",
    6: "Design Review Board",
    7: "Historic Preservation Commission",
    2: "Planning and Zoning Commission",
    4: "Planning and Zoning Special Meeting",
    3: "Subcommittee Meeting",
}

# Map HTML body name text → slug (for parsing the grid)
_BODY_NAME_TO_SLUG: dict[str, str] = {
    "board of adjustment": "peoria-board-of-adjustment",
    "city council meetings": "peoria-city-council",
    "design review board": "peoria-design-review-board",
    "historic preservation commission": "peoria-historic-preservation-commission",
    "planning and zoning commission": "peoria-planning-and-zoning-commission",
    "planning and zoning special meeting": "peoria-planning-and-zoning-special-meeting",
    "subcommittee meeting": "peoria-subcommittee-meeting",
}

# Default: only sync City Council meetings initially
DEFAULT_BODY_SLUGS = ["peoria-city-council"]

# Map body slug to internal short code (for meeting body field)
SLUG_TO_CODE: dict[str, str] = {
    "peoria-board-of-adjustment": "peoria-boa",
    "peoria-city-council": "peoria-cc",
    "peoria-design-review-board": "peoria-drb",
    "peoria-historic-preservation-commission": "peoria-hpc",
    "peoria-planning-and-zoning-commission": "peoria-pz",
    "peoria-planning-and-zoning-special-meeting": "peoria-pz-special",
    "peoria-subcommittee-meeting": "peoria-sub",
}

# ── URL patterns ──

BASE_URL = "https://peoriaaz.novusagenda.com/agendapublic/"
SEARCH_URL = BASE_URL
MEETING_VIEW_URL = urllib.parse.urljoin(BASE_URL, "MeetingView.aspx")
COVERSHEET_URL = urllib.parse.urljoin(BASE_URL, "CoverSheet.aspx")
AGENDA_PDF_URL = urllib.parse.urljoin(BASE_URL, "DisplayAgendaPDF.ashx")


# ── HTTP helpers ──

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch an HTML page from Peoria NovusAgenda."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def _extract_form_fields(html: str) -> dict[str, str]:
    """Extract ASP.NET WebForms hidden fields from a page."""
    fields: dict[str, str] = {}
    for name in (
        "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
        "__PREVIOUSFOCUSED", "__LASTFOCUS",
    ):
        # Match value="..." in input elements
        m = re.search(
            rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', html
        )
        if not m:
            m = re.search(
                rf'id="{re.escape(name)}"[^>]*value="([^"]*)"', html
            )
        fields[name] = m.group(1) if m else ""
    return fields


def _resolve_body_slug(body_name: str) -> str:
    """Normalize a NovusAgenda body name to our slug."""
    key = body_name.lower().strip()
    if key in _BODY_NAME_TO_SLUG:
        return _BODY_NAME_TO_SLUG[key]
    # Partial matching
    for pattern, slug in sorted(_BODY_NAME_TO_SLUG.items(), key=lambda x: -len(x[0])):
        if pattern in key or key in pattern:
            return slug
    log.debug("Unknown Peoria body name: '%s', defaulting to peoria-city-council", body_name)
    return "peoria-city-council"


# ── Parsing helpers ──

def _text(node) -> str:
    """Get cleaned text from an HTML node."""
    if node is None:
        return ""
    return _clean_html_text(_node_text(node))


def _attr(node, key: str) -> str:
    """Get an attribute value from an HTML node, or empty string."""
    return (node.attrs.get(key) or "").strip()


def _find_link_in_cell(cell) -> Optional[str]:
    """Find the href of the first <a> within a table cell."""
    for a in _find_all(cell, "a"):
        href = _attr(a, "href")
        if href:
            return href
    return None


def _extract_meeting_id_from_link(cell) -> tuple[str, str, str, str]:
    """Extract MeetingID, MinutesMeetingID, meeting_view_url, minutes_view_url
    from the onclick attribute of <a> tags within a table cell.

    The NovusAgenda RadGrid uses onclick=javascript:ViewHTML=window.open(
        'MeetingView.aspx?MeetingID=X&MinutesMeetingID=Y&doctype=Agenda')
    rather than href for the Online Agenda and Minutes Recap links.
    """
    meeting_id = ""
    minutes_meeting_id = ""
    meeting_view_url = ""
    minutes_view_url = ""

    for a in _find_all(cell, "a"):
        onclick = a.attrs.get("onclick", "") or ""
        img_alt = ""
        for img in _find_all(a, "img"):
            img_alt = (img.attrs.get("alt") or "").lower()

        if "window.open" in onclick and "MeetingView.aspx" in onclick:
            # Extract URL from onclick: window.open('URL', ...)
            m = re.search(r"MeetingView\.aspx\?([^'\"]+)", onclick)
            if m:
                query_string = m.group(1)
                params = urllib.parse.parse_qs(query_string)

                # Extract MeetingID from the URL
                mid = params.get("MeetingID", [""])[0]
                mmid = params.get("MinutesMeetingID", [""])[0]
                doctype = params.get("doctype", [""])[0]

                if "view agenda html" in img_alt or doctype == "Agenda":
                    meeting_id = mid or meeting_id
                    minutes_meeting_id = mmid or minutes_meeting_id
                    if mid:
                        meeting_view_url = urllib.parse.urljoin(
                            BASE_URL,
                            f"MeetingView.aspx?MeetingID={mid}&doctype=Agenda"
                        )
                elif "view minutes" in img_alt or doctype == "Minutes":
                    if mid:
                        minutes_view_url = urllib.parse.urljoin(
                            BASE_URL,
                            f"MeetingView.aspx?MeetingID={mid}&doctype=Minutes"
                        )
                    if not minutes_meeting_id:
                        minutes_meeting_id = mmid

    return meeting_id, minutes_meeting_id, meeting_view_url, minutes_view_url


def _normalize_date(mm_dd_yy: str) -> str:
    """Convert '05/07/26' to '2026-05-07'."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", mm_dd_yy.strip())
    if not m:
        return mm_dd_yy
    month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if len(year) == 2:
        year = f"20{year}"
    return f"{year}-{month:02d}-{day:02d}"


# ── Meeting list extraction ──

def parse_meetings_from_html(html: str) -> list[dict]:
    """Parse the RadGrid meeting table from the NovusAgenda search page.

    Returns a list of meeting dicts with keys:
      - body_name           : Full body name
      - body_slug           : Normalized body slug
      - meeting_date        : YYYY-MM-DD
      - meeting_type        : Body name (also used as type)
      - meeting_location    : Location text
      - meeting_id          : NovusAgenda MeetingID from MeetingView link
      - minutes_meeting_id  : MinutesMeetingID (may be -1 or empty)
      - meeting_view_url    : MeetingView.aspx?MeetingID=X
      - agenda_html_url     : MeetingView.aspx URL (same as meeting_view_url for Agenda doctype)
      - agenda_pdf_url      : DisplayAgendaPDF.ashx?MeetingID=X
      - minutes_html_url    : MeetingView.aspx?doctype=Minutes URL
      - minutes_pdf_url     : DisplayAgendaPDF.ashx?MinutesMeetingID=X
      - source_url          : URL the page was parsed from
    """
    root = _parse_html(html)
    meetings: list[dict] = []

    # Find the RadGrid meetings table
    table = _find_meetings_grid(root)
    if table is None:
        log.warning("No meetings RadGrid table found in HTML")
        return meetings

    # Find data rows (skip header rows)
    rows = [tr for tr in _find_all(table, "tr")
            if "rgRow" in (tr.attrs.get("class") or "")
            or "rgAltRow" in (tr.attrs.get("class") or "")]

    log.debug("Found %d meeting rows in RadGrid", len(rows))

    for row in rows:
        cells = [td for td in _find_all(row, "td") if td.tag == "td"]
        if len(cells) < 7:
            continue

        # Column 0: Meeting Date
        meeting_date_raw = _text(cells[0]) if len(cells) > 0 else ""
        meeting_date = _normalize_date(meeting_date_raw)

        # Column 1: Meeting Type (body name)
        meeting_type = _text(cells[1]) if len(cells) > 1 else ""

        # Column 2: Meeting Location
        meeting_location = _text(cells[2]) if len(cells) > 2 else ""

        # Column 3: Online Agenda HTML (MeetingView.aspx)
        # Note: NovusAgenda uses onclick=javascript: not href for these links
        meeting_view_url = ""
        meeting_id = ""
        minutes_meeting_id = ""
        minutes_view_url = ""
        if len(cells) > 3:
            mid, mmid, mv_url, min_url = _extract_meeting_id_from_link(cells[3])
            meeting_id = mid
            if mv_url:
                meeting_view_url = mv_url
            minutes_meeting_id = mmid or minutes_meeting_id
            minutes_view_url = min_url or minutes_view_url

        if not meeting_id:
            log.debug("Skipping row without MeetingID: %s", meeting_type)
            continue

        # Column 4: Download Agenda PDF
        agenda_pdf_url = ""
        if len(cells) > 4:
            link_href = _find_link_in_cell(cells[4])
            if link_href:
                agenda_pdf_url = urllib.parse.urljoin(BASE_URL, link_href)

        # Column 5: Minutes Recap HTML (also uses onclick, not href)
        if len(cells) > 5:
            _, _, idx_min_view_url = "", "", ""
            mid2, mmid2, _, min_url2 = _extract_meeting_id_from_link(cells[5])
            if min_url2:
                minutes_view_url = min_url2
            if mmid2:
                minutes_meeting_id = mmid2
        if not minutes_view_url and meeting_id:
            # Build from meeting_id
            minutes_view_url = urllib.parse.urljoin(
                BASE_URL,
                f"MeetingView.aspx?MeetingID={meeting_id}"
                f"&MinutesMeetingID={minutes_meeting_id}&doctype=Minutes"
            )

        # Column 6: Legal Minutes PDF
        minutes_pdf_url = ""
        if len(cells) > 6:
            link_href = _find_link_in_cell(cells[6])
            if link_href:
                minutes_pdf_url = urllib.parse.urljoin(BASE_URL, link_href)

        body_slug = _resolve_body_slug(meeting_type)

        m = {
            "body_name": meeting_type,
            "body_slug": body_slug,
            "meeting_date": meeting_date,
            "meeting_type": meeting_type,
            "meeting_location": meeting_location,
            "meeting_id": meeting_id,
            "minutes_meeting_id": minutes_meeting_id,
            "meeting_view_url": meeting_view_url,
            "agenda_pdf_url": agenda_pdf_url,
            "minutes_view_url": minutes_view_url,
            "minutes_pdf_url": minutes_pdf_url,
        }
        meetings.append(m)

    return meetings


def _find_meetings_grid(root) -> Optional[object]:
    """Find the RadGrid master table for meetings in the parsed HTML tree.

    The meetings grid has id containing ``radGridMeetings`` and class
    ``rgMasterTable``.
    """
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        tbl_id = table.attrs.get("id") or ""
        if "rgMasterTable" in cls and "radGridMeetings" in tbl_id:
            return table
    # Fallback
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        if "rgMasterTable" in cls:
            return table
    return None


def meetings_for_body(meetings: list[dict], body_slug: str) -> list[dict]:
    """Filter meeting list to only those belonging to a specific body."""
    return [m for m in meetings if m.get("body_slug") == body_slug]


# ── Agenda item extraction from MeetingView.aspx ──

def parse_agenda_items_from_html(html: str, meeting_id: str) -> list[dict]:
    """Parse agenda items from a MeetingView.aspx HTML page.

    The MeetingView page displays the full agenda in an HTML table where:
      - column2 has the item number (e.g., "4", "14")
      - column3 has the item type letter ("R"=Regular, "C"=Consent)
      - column4 has the item title (hyperlinked to CoverSheet.aspx)

    Returns a list of item dicts with keys:
      - meeting_id
      - agenda_item_number  : e.g. "4C", "14R"
      - item_type_category  : "section" for section headers, "item" for action items
      - section_level       : nested depth (0 for items, 1+ for sections)
      - agenda_item_title   : Title text
      - agenda_item_text    : Description / body text
      - coversheet_url      : CoverSheet.aspx URL (if available)
      - item_id             : NovusAgenda ItemID (from CoverSheet link)
      - sort_order          : Sequence number
    """
    items: list[dict] = []

    # Primary method: parse the agenda table structure (column2/column3/column4)
    # This handles the standard MeetingView.aspx pages
    table_items = _extract_agenda_table_items(html, meeting_id)
    if table_items:
        items = table_items
    else:
        # Fallback: parse via text patterns
        root = _parse_html(html)
        items = _parse_novus_agenda_text(root, meeting_id)

    return items


def _find_content_area(root) -> Optional[object]:
    """Find the main content area div in the MeetingView page."""
    for div in _find_all(root, "div"):
        div_id = div.attrs.get("id") or ""
        if div_id == "content":
            return div
    return None


def _extract_agenda_table_items(html: str, meeting_id: str) -> list[dict]:
    """Parse agenda items from the MeetingView.aspx table structure.

    The MeetingView page renders agenda items in an HTML table where:
      - column2 contains the item number (e.g., "4", "14")
      - column3 contains the item type letter ("R"=Regular, "C"=Consent)
      - column4 contains the item title, often with a CoverSheet.aspx link

    Section headings (Consent Agenda, Regular Agenda, New Business, etc.)
    are found as text rows or <strong> tags in the table.

    Returns a list of item dicts.
    """
    import re
    items: list[dict] = []
    sort_order = 0

    # Find the main agenda table — look for the table that contains
    # column1, column2, column3, column4 markers
    table_match = re.search(
        r'(<table[^>]*width="?100%"?[^>]*>.*?)(?=<br\s*/?>\s*<br\s*/?>|$)',
        html, re.DOTALL | re.IGNORECASE
    )
    table_html = table_match.group(1) if table_match else html

    # If we can't find the specific table, just use the whole HTML
    if 'column2' not in table_html:
        # Try finding the first table with column id attributes
        alt_match = re.search(
            r'<table[^>]*>.*?(?:<td[^>]*id="column1".*?){3,}.*?</table>',
            html, re.DOTALL | re.IGNORECASE
        )
        if alt_match:
            table_html = alt_match.group(0)

    # Track current section context
    current_section = ""
    section_level = 0

    # Find section headings — these are typically <strong> or <b> tags
    # or standalone text rows before item rows
    section_keywords_lower = [
        "consent agenda", "regular agenda", "new business",
        "call to the public", "reports from staff", "adjournment",
        "public hearing", "opening statement", "roll call",
        "executive session", "presentation", "discussion and possible action",
        "consent", "regular",
    ]

    # Find section headings in the table HTML using <strong>/<b>/<h*> tags
    for strong_match in re.finditer(
        r'<(?:strong|b|h[1-6])(?:[^>]*)>\s*([^<]+?)\s*</(?:strong|b|h[1-6])>',
        table_html, re.IGNORECASE | re.DOTALL
    ):
        heading_text = re.sub(r'\s+', ' ', strong_match.group(1)).strip()
        heading_lower = heading_text.lower()
        if any(kw in heading_lower for kw in section_keywords_lower):
            sort_order += 1
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": "",
                "item_type_category": "section",
                "section_level": 1,
                "agenda_item_title": heading_text,
                "agenda_item_text": "",
                "coversheet_url": "",
                "item_id": "",
                "sort_order": sort_order,
            })
            current_section = heading_text
            section_level = 1

    # Find item rows: <tr> containing column2 and column4
    # Use regex to parse each row
    row_pattern = re.compile(
        r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE
    )

    for row_match in row_pattern.finditer(table_html):
        row_html = row_match.group(1)

        # Skip header rows (contain <th>)
        if '<th' in row_html.lower():
            continue

        # Extract column2 (item number)
        col2_match = re.search(
            r'<td[^>]*id="column2"[^>]*>(.*?)</td>',
            row_html, re.DOTALL | re.IGNORECASE
        )
        col3_match = re.search(
            r'<td[^>]*id="column3"[^>]*>(.*?)</td>',
            row_html, re.DOTALL | re.IGNORECASE
        )
        col4_match = re.search(
            r'<td[^>]*id="column4"[^>]*>(.*?)</td>',
            row_html, re.DOTALL | re.IGNORECASE
        )

        if not col2_match or not col3_match or not col4_match:
            continue

        col2_raw = re.sub(r'<[^>]+>', '', col2_match.group(1)).strip()
        col3_raw = re.sub(r'<[^>]+>', '', col3_match.group(1)).strip().rstrip('.')
        col4_html = col4_match.group(1)
        col4_text = re.sub(r'<[^>]+>', '', col4_html).strip()

        # Check if this is an item row (has a number)
        if not col2_raw:
            continue

        # Build item number
        item_number = col2_raw + col3_raw

        # Check for CoverSheet link
        coversheet_href = ""
        item_id = ""
        cover_match = re.search(
            r'href="([^"]*Cover[Ss]heet[^"]*)"', col4_html
        )
        if cover_match:
            coversheet_href = cover_match.group(1)
            coversheet_url = urllib.parse.urljoin(BASE_URL, coversheet_href)
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(coversheet_href).query
            )
            item_id = params.get("ItemID", [""])[0]
        else:
            coversheet_url = ""

        # Use item title or the text with CoverSheet link
        # If there's an <a> tag, the link text is the title
        a_match = re.search(
            r'<a[^>]*>(.*?)</a>', col4_html, re.DOTALL | re.IGNORECASE
        )
        title = col4_text
        if a_match:
            title = re.sub(r'<[^>]+>', '', a_match.group(1)).strip()
            if not title:
                title = col4_text

        # HTML decode the title and the URL
        title = html_mod.unescape(title).strip()
        if coversheet_href:
            coversheet_href = html_mod.unescape(coversheet_href)
            coversheet_url = urllib.parse.urljoin(BASE_URL, coversheet_href)

        sort_order += 1
        items.append({
            "meeting_id": meeting_id,
            "agenda_item_number": item_number,
            "item_type_category": "item",
            "section_level": 0,
            "agenda_item_title": title,
            "agenda_item_text": "",
            "coversheet_url": coversheet_url,
            "item_id": item_id,
            "sort_order": sort_order,
        })

    return items


def _find_coversheet_items(root) -> list[dict]:
    """Find CoverSheet.aspx links which identify individual agenda items.

    Each CoverSheet link in the MeetingView page marks an agenda item.

    Returns a list of dicts with keys: number, title, description,
    coversheet_url, item_id.
    """
    items: list[dict] = []

    for a in _find_all(root, "a"):
        href = _attr(a, "href")
        if "Coversheet.aspx" in href or "CoverSheet.aspx" in href:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            item_id = params.get("ItemID", [""])[0]
            title = _text(a).strip()
            coversheet_url = urllib.parse.urljoin(BASE_URL, href)

            items.append({
                "number": f"item-{item_id}" if item_id else "agenda-item",
                "title": title,
                "description": "",
                "coversheet_url": coversheet_url,
                "item_id": item_id,
            })

    return items


def _find_agenda_sections(root) -> list[dict]:
    """Find section headings in the agenda page.

    NovusAgenda MeetingView pages typically have section headings like:
      - "Consent Agenda" (often bold/heading)
      - "Regular Agenda"
      - "New Business"
      - "Reports from Staff"

    Returns a list of dicts with keys: title, level, position.
    """
    sections: list[dict] = []
    section_keywords = [
        "consent agenda", "regular agenda", "new business",
        "call to the public", "reports from staff", "adjournment",
        "public hearing", "opening statement", "roll call",
        "executive session", "presentation", "discussion and possible action",
    ]

    for tag in ("strong", "b", "h1", "h2", "h3", "h4", "h5", "h6", "p", "div"):
        for elem in _find_all(root, tag):
            elem_text = _text(elem).strip().lower()
            original_text = _text(elem).strip()
            if any(kw in elem_text for kw in section_keywords):
                if len(original_text) < 100 or tag in ("h1", "h2", "h3", "h4", "strong", "b"):
                    sections.append({
                        "title": original_text,
                        "level": 1 if tag in ("strong", "b") else 2,
                    })
            elif len(original_text) > 3 and len(original_text) < 80:
                alpha_text = re.sub(r'[^a-zA-Z\s]', '', original_text)
                if alpha_text and alpha_text == alpha_text.upper() and len(alpha_text.split()) <= 6:
                    if any(word in alpha_text.lower() for word in
                           ["agenda", "business", "public", "staff", "reports", "discussion",
                            "consent", "regular", "executive", "adjournment", "roll call",
                            "opening", "call to"]):
                        sections.append({
                            "title": original_text,
                            "level": 1,
                        })

    return sections


def _parse_novus_agenda_text(root, meeting_id: str) -> list[dict]:
    """Parse the MeetingView HTML by extracting sections and items from
    the visible text content.

    This is a fallback parser for when explicit CoverSheet links aren't found.

    Returns a list of item dicts.
    """
    items: list[dict] = []
    sort_order = 0

    body_text = _text(root)

    # Find item patterns
    item_pattern = re.compile(r'^\s*(\d+[A-Z]?)\s+(.+?)(?:\s*\(.*?\))?\s*$', re.MULTILINE)

    for para in _find_all(root, "p"):
        para_text = _text(para).strip()
        m = item_pattern.match(para_text)
        if m:
            sort_order += 1
            item_number = m.group(1)
            item_title = m.group(2).strip()
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": item_number,
                "item_type_category": "item",
                "section_level": 0,
                "agenda_item_title": item_title,
                "agenda_item_text": "",
                "coversheet_url": "",
                "item_id": "",
                "sort_order": sort_order,
            })

    for a in _find_all(root, "a"):
        href = _attr(a, "href")
        if "CoverSheet" in href or "Coversheet" in href:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            item_id = params.get("ItemID", [""])[0]
            title = _text(a).strip()
            already = any(i.get("item_id") == item_id for i in items)
            if not already and item_id:
                sort_order += 1
                items.append({
                    "meeting_id": meeting_id,
                    "agenda_item_number": f"item-{item_id}",
                    "item_type_category": "item",
                    "section_level": 0,
                    "agenda_item_title": title,
                    "agenda_item_text": "",
                    "coversheet_url": urllib.parse.urljoin(BASE_URL, href),
                    "item_id": item_id,
                    "sort_order": sort_order,
                })

    return items


# ── CoverSheet / attachment extraction ──

def parse_coversheet_from_html(html: str) -> dict:
    """Parse a CoverSheet.aspx page for item details and attachments.

    Returns a dict with keys:
      - item_id
      - meeting_id
      - case_name
      - proposal
      - location
      - attachments : list of dicts [{title, url}]
    """
    root = _parse_html(html)
    result: dict = {
        "case_name": "",
        "proposal": "",
        "location": "",
        "attachments": [],
    }

    # Extract Case Name / Title
    for tag in ("span", "div", "p"):
        for elem in _find_all(root, tag):
            elem_text = _text(elem).strip()
            if "Case Name:" in elem_text or "CaseName" in (elem.attrs.get("class") or ""):
                # Extract value after "Case Name:"
                m = re.search(r"Case\s*Name:\s*(.+?)(?:$|\n)", elem_text)
                if m:
                    result["case_name"] = m.group(1).strip()
                break

    # Extract Proposal
    for tag in ("span", "div", "p"):
        for elem in _find_all(root, tag):
            elem_text = _text(elem).strip()
            if elem_text.startswith("Proposal:") or elem_text.startswith("Proposal"):
                m = re.search(r"Proposal(?:ition)?:\s*(.+?)(?:$|\n)", elem_text)
                if m:
                    result["proposal"] = m.group(1).strip()
                break

    # Extract Location
    for tag in ("span", "div", "p"):
        for elem in _find_all(root, tag):
            elem_text = _text(elem).strip()
            if elem_text.startswith("Location:"):
                m = re.search(r"Location:\s*(.+?)(?:$|\n)", elem_text)
                if m:
                    result["location"] = m.group(1).strip()
                break

    # Find attachments (links to AttachmentViewer.ashx)
    for a in _find_all(root, "a"):
        href = _attr(a, "href")
        if "AttachmentViewer.ashx" in href:
            title = _text(a).strip()
            if title:
                result["attachments"].append({
                    "title": title,
                    "url": urllib.parse.urljoin(BASE_URL, href),
                })

    return result


# ── Search ──

def search_peoria_meetings(
    body_slugs: Optional[list[str]] = None,
    body_ids: Optional[list[int]] = None,
    date_range: str = "l6m",
) -> list[dict]:
    """Search for Peoria meetings on the NovusAgenda search page.

    Fetches the search page, extracts ASP.NET form fields, then POSTs
    back with the date range and body/committee filter.

    Parameters
    ----------
    body_slugs : list[str], optional
        Only return meetings for these body slugs.  Defaults to
        ``["peoria-city-council"]``.
    body_ids : list[int], optional
        NovusAgenda committee IDs to filter by.  If not specified,
        derives from *body_slugs*.
    date_range : str
        Date range value for the dropdown.  Options:
        ``lmn`` (Last Month), ``l6m`` (Last 6 Months), ``lyr`` (Last Year),
        ``nmn`` (Next Month), ``n6m`` (Next 6 Months), ``nyr`` (Next Year),
        ``6ms`` (6 Month Span - default), ``cus`` (Custom).

    Returns
    -------
    list[dict]
        Meeting dicts as returned by ``parse_meetings_from_html()``.
    """
    if body_slugs is None:
        body_slugs = DEFAULT_BODY_SLUGS

    if body_ids is None:
        body_ids = [_BODY_NAME_TO_SLUG.get(s, "") for s in body_slugs]
        # Convert slugs to numeric IDs
        body_ids = [BODY_CODE_MAP.get(s) for s in body_slugs]
        body_ids = [b for b in body_ids if b is not None]
        if not body_ids:
            body_ids = [1]  # Default to City Council

    all_meetings: list[dict] = []

    # Search for each body ID separately (NovusAgenda only supports one
    # committee ID per POST)
    for body_id in body_ids:
        try:
            meetings = _search_single_body(body_id, date_range)
            all_meetings.extend(meetings)
        except Exception as e:
            log.warning(
                "Failed to search Peoria meetings for body_id=%s: %s",
                body_id, e,
            )

    # Deduplicate by meeting_id (a meeting might appear for multiple filters
    # if the body_id is -1 "all")
    seen_ids: set[str] = set()
    unique_meetings: list[dict] = []
    for m in all_meetings:
        mid = m.get("meeting_id", "")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            unique_meetings.append(m)
        elif not mid:
            unique_meetings.append(m)

    log.info("Found %d unique Peoria meeting(s)", len(unique_meetings))
    return unique_meetings


def _search_single_body(body_id: int, date_range: str = "l6m") -> list[dict]:
    """Search Peoria meetings for a single committee/body ID.

    Parameters
    ----------
    body_id : int
        NovusAgenda committee ID (1=City Council, 2=Planning & Zoning, etc.).
        Use -1 for all bodies.
    date_range : str
        Date range code.

    Returns
    -------
    list[dict]
        Meeting dicts.
    """
    # Step 1: GET the search page to extract form fields
    html = fetch_page(SEARCH_URL)
    fields = _extract_form_fields(html)

    # Step 2: POST back with search parameters
    form_data_list = [
        ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
        ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
        ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
        # Date range
        ("ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ddlDateRange", date_range),
        # Body/committee filter
        ("ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ctl00", str(body_id)),
        # Category (leave empty)
        ("ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ctl01", "-1"),
        # Text search (leave empty)
        ("ctl00$ContentPlaceHolder1$SearchAgendasMeetings$ctl02", ""),
        # Search button click
        ("ctl00$ContentPlaceHolder1$SearchAgendasMeetings$imageButtonSearch.x", "0"),
        ("ctl00$ContentPlaceHolder1$SearchAgendasMeetings$imageButtonSearch.y", "0"),
        # Hidden RadAjax fields
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radTSMain_ClientState", ""),
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radMPMain_ClientState", ""),
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radGridMeetings_ClientState", ""),
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radGridItems_ClientState", ""),
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_sharedDynamicCalendar_ClientState", ""),
        # RadAjaxManager update panels
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radCalendarFrom_dateInput_ClientState", ""),
        ("ctl00_ContentPlaceHolder1_SearchAgendasMeetings_radCalendarTo_dateInput_ClientState", ""),
    ]

    data = urllib.parse.urlencode(form_data_list).encode("utf-8")

    req = urllib.request.Request(
        SEARCH_URL,
        data=data,
        headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to POST search for body_id=%s: %s", body_id, e)
        raise

    meetings = parse_meetings_from_html(result_html)
    log.info(
        "Found %d Peoria meeting(s) for body_id=%s (date_range=%s)",
        len(meetings), body_id, date_range,
    )
    return meetings


def fetch_agenda_items_async(
    meeting_view_url: str, meeting_id: str
) -> list[dict]:
    """Fetch and parse agenda items from a MeetingView page via plain HTTP.

    Parameters
    ----------
    meeting_view_url : str
        Full URL to the MeetingView.aspx page.
    meeting_id : str
        NovusAgenda MeetingID.

    Returns
    -------
    list[dict]
        Agenda item dicts.
    """
    try:
        html = fetch_page(meeting_view_url)
        return parse_agenda_items_from_html(html, meeting_id)
    except Exception as e:
        err_str = str(e)
        if "410" in err_str or "404" in err_str or "Gone" in err_str:
            log.warning(
                "Meeting view page not available (410/404) for %s: %s",
                meeting_id, meeting_view_url,
            )
            return []
        raise


async def fetch_coversheet_async(coversheet_url: str) -> dict:
    """Fetch and parse a CoverSheet page for item details and attachments.

    Parameters
    ----------
    coversheet_url : str
        Full URL to the CoverSheet.aspx page.

    Returns
    -------
    dict
        CoverSheet data with case_name, proposal, location, attachments.
    """
    try:
        html = fetch_page(coversheet_url)
        return parse_coversheet_from_html(html)
    except Exception as e:
        log.warning("Failed to fetch coversheet %s: %s", coversheet_url, e)
        return {"case_name": "", "proposal": "", "location": "", "attachments": []}


# ── Minutes PDF helpers ──

def fetch_minutes_pdf_bytes(minutes_pdf_url: str) -> Optional[bytes]:
    """Download a Peoria Minutes PDF from a DisplayAgendaPDF.ashx URL.

    Returns raw PDF bytes, or None on failure.
    """
    try:
        req = urllib.request.Request(
            minutes_pdf_url,
            headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Minutes PDF not available for %s: %s", minutes_pdf_url, e)
        return None


def extract_minutes_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a Peoria Minutes PDF using pdftotext."""
    import subprocess, tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.warning("pdftotext failed for Peoria minutes: %s", e)
        return None
    finally:
        import os
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def parse_peoria_minutes_votes(text: str, meeting_id: str) -> dict:
    """Parse Peoria City Council vote data from Minutes PDF text.

    Peoria minutes have a consent agenda (all C items voted in one motion)
    and regular agenda items (R items with individual votes).

    The vote format is:
        Upon tabulation of votes, it showed:
        AYES - Name, Name, Name...  (or AYES - Name-Name-Name)
        NAYS - None  (or NAYS - Name)
        ABSENT - None

    Returns dict with:
      - supervisors: list of {name, normalized_name, present}
      - votes: list of {agenda_item_number, ayes, nays, motion_result, supervisor_votes}
    """
    import re
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()

    # Find all vote blocks
    vote_blocks = re.split(r"\n\s*(?=Upon tabulation of votes)", text)

    for block in vote_blocks:
        if "Upon tabulation of votes" not in block:
            continue

        # Extract AYES, NAYS, ABSENT
        ayes_match = re.search(r"AYES[\s\-–]+(.+?)(?:\n|$)", block)
        nays_match = re.search(r"NAYS[\s\-–]+(.+?)(?:\n|$)", block)
        absent_match = re.search(r"ABSENT[\s\-–]+(.+?)(?:\n|$)", block)

        ayes_text = ayes_match.group(1).strip() if ayes_match else ""
        nays_text = nays_match.group(1).strip() if nays_match else ""
        absent_text = absent_match.group(1).strip() if absent_match else ""

        # Parse names (formats: "Beck, Crawford, Finn" or "Beck-Crawford-Finn" or "None")
        def _parse_names(text: str) -> list[str]:
            text = text.strip().strip(".").strip()
            if not text or text.lower() in ("none", ""):
                return []
            if "-" in text and "," not in text:
                return [n.strip().strip(".").strip() for n in text.split("-") if n.strip().strip(".")]
            return [n.strip().strip(".").strip() for n in text.replace(",", "\n").split("\n") if n.strip().strip(".")]

        ayes_list = _parse_names(ayes_text)
        nays_list = _parse_names(nays_text)
        absent_list = _parse_names(absent_text)

        # Determine motion result
        if "unanimously" in block.lower():
            result = "Carried Unanimously"
        elif nays_list:
            result = "Carried"
        else:
            result = "Carried"

        # Determine item number context
        # Look backwards from the vote block for the item number
        item_nums: list[str] = []

        # For consent agenda blocks
        if "Consent Agenda" in block or "consent agenda" in block:
            item_nums = ["Consent"]
        else:
            # Look for item numbers like "22 R." or "22 R" in the surrounding text
            item_matches = re.findall(r"(?m)^\s*(\d+)\s*[Rr]", block)
            if item_matches:
                item_nums = item_matches
            else:
                # Look backwards in the full text before this block
                idx = text.find(block)
                before = text[max(0, idx - 500):idx]
                item_matches = re.findall(r"(?m)^\s*(\d+)\s*[Rr]\s*[.\u2013]", before)
                if item_matches:
                    item_nums = [item_matches[-1]]

        if not item_nums:
            item_nums = ["unknown"]

        # Collect supervisors
        all_names = ayes_list + nays_list + absent_list
        for name in all_names:
            norm = name.lower().strip()
            if norm and norm not in seen_sup:
                seen_sup.add(norm)
                supervisors.append({
                    "name": name.strip(),
                    "normalized_name": norm,
                    "present": norm not in [a.lower() for a in absent_list],
                })

        # Create supervisor_votes list
        sup_votes: list[dict] = []
        for name in ayes_list:
            sup_votes.append({"name": name.strip(), "vote": "yes", "raw_vote_text": name.strip()})
        for name in nays_list:
            sup_votes.append({"name": name.strip(), "vote": "no", "raw_vote_text": name.strip()})

        for an in item_nums:
            votes.append({
                "agenda_item_number": an,
                "ayes": ayes_list,
                "nays": nays_list,
                "motion_result": result,
                "supervisor_votes": sup_votes,
                "vote_text": f"Ayes: {len(ayes_list)}; Nays: {len(nays_list)}",
            })

    return {"supervisors": supervisors, "votes": votes}


# ── Main / CLI entry point ──

def main() -> None:
    """Simple CLI entry point for testing."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(name)s - %(message)s",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "meetings":
        # Test: search meetings
        body = sys.argv[2] if len(sys.argv) > 2 else "peoria-city-council"
        bodies = [body]
        meetings = search_peoria_meetings(body_slugs=bodies, date_range="lyr")
        print(f"\nFound {len(meetings)} Peoria {body} meeting(s):")
        for m in meetings:
            print(f"  {m['meeting_date']} - {m['meeting_type']} (ID={m['meeting_id']})")
            print(f"    View: {m['meeting_view_url']}")
            if m['agenda_pdf_url']:
                print(f"    PDF:  {m['agenda_pdf_url']}")
            print()

    elif len(sys.argv) > 1 and sys.argv[1] == "items":
        # Test: fetch agenda items for a specific meeting
        meeting_id = sys.argv[2]
        url = f"{MEETING_VIEW_URL}?MeetingID={meeting_id}&doctype=Agenda"
        raw_html = fetch_page(url)
        agenda_items = parse_agenda_items_from_html(raw_html, meeting_id)
        print(f"\nFound {len(agenda_items)} agenda items for meeting {meeting_id}:")
        for item in agenda_items:
            num = item['agenda_item_number'] or '(header)'
            cat = item['item_type_category']
            title = item['agenda_item_title'][:70]
            iid = item['item_id'] or '-'
            print(f"  [{cat:7s}] {num:6s} | {title:65s} | ItemID={iid}")

    elif len(sys.argv) > 1 and sys.argv[1] == "all":
        # Test: search all bodies
        all_body_slugs = list(BODY_CODE_MAP.keys())
        meetings = search_peoria_meetings(body_slugs=all_body_slugs, date_range="l6m")
        print(f"\nFound {len(meetings)} Peoria meeting(s) total:")
        for m in meetings:
            print(f"  {m['meeting_date']:12s} | {m['body_name']:35s} | ID={m['meeting_id']}")

    else:
        print("Usage:")
        print("  python -m scraper.peoria meetings [body_slug]")
        print("  python -m scraper.peoria items <meeting_id>")
        print("  python -m scraper.peoria all")
        print()
        print("Body slugs:")
        for slug in BODY_CODE_MAP:
            print(f"  {slug} (code={BODY_CODE_MAP[slug]})")


if __name__ == "__main__":
    main()
