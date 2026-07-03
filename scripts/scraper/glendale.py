"""
City of Glendale City Council meeting and agenda extraction via Legistar (Granicus).

Glendale uses the Legistar agenda management system at ``glendale-az.legistar.com``.

The Calendar page (Calendar.aspx) lists upcoming/recent meetings in a Telerik
RadGrid HTML table.  Each row includes:
  - Body name (link to DepartmentDetail.aspx)
  - Meeting date
  - Meeting time
  - Meeting location
  - Meeting Details (link to MeetingDetail.aspx)
  - Agenda (View.ashx?M=A)
  - Accessible Agenda (View.ashx?M=AADA)
  - Minutes (View.ashx?M=M)
  - Video link

Glendale's Legistar instance differs from Mesa's in a few ways:
  - The body dropdown ComboBox has id ``ctl00_ContentPlaceHolder1_lstBodies``
  - Only "City Council" and "City Council Workshop" body options
  - The Calendar page has tabs: "List View" (tab index 0) and "Calendar View" (tab index 1)
  - Single department detail: DepartmentDetail.aspx?ID=26355 for City Council
  - No year dropdown (uses a date range filter instead)

MeetingDetail.aspx shows agenda items in an HTML table with columns:
  File#, Agenda#, Type, Title, Action Result, Action Details.

LegislationDetail.aspx shows individual item details with attachments.
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.io_utils import normalize_meeting_date
from scraper.models import _HtmlNode

log = logging.getLogger(__name__)

# ── Jurisdiction / body constants ──

JURISDICTION_ID = 9          # City of Glendale
PUBLIC_BODY_CODE = "glendale-cc"
SOURCE_SYSTEM = "legistar"
SOURCE_INSTANCE_URL = "https://glendale-az.legistar.com"

# ── Public body slug mapping ──
# Matches body names in Legistar Calendar.aspx to our public body slugs.

BODY_SLUG_MAP = {
    "city council": "glendale-city-council",
    "city council workshop": "glendale-city-council-workshop",
    "city council study session": "glendale-city-council-workshop",
}

# Body code → code slug for the ``body`` column on meetings/agenda_items
BODY_CODE_MAP = {
    "glendale-city-council": "glendale-cc",
    "glendale-city-council-workshop": "glendale-cc-ws",
    "glendale-city-council-study-session": "glendale-cc-ws",
}

# Default: only sync City Council regular meetings initially
DEFAULT_BODY_SLUGS = ["glendale-city-council"]


# ── URL patterns ──

BASE_URL = "https://glendale-az.legistar.com"
CALENDAR_URL = f"{BASE_URL}/Calendar.aspx"
DEPARTMENT_DETAIL_URL = f"{BASE_URL}/DepartmentDetail.aspx?ID=26355"

# Glendale Legistar only has one department (City Council) with ID 26355


# ── Parsing helpers ──

def _attr(node: _HtmlNode, key: str) -> str:
    """Get an attribute value from an HTML node, or empty string."""
    return (node.attrs.get(key) or "").strip()


def _text(node: _HtmlNode | None) -> str:
    """Get cleaned text from an HTML node."""
    if node is None:
        return ""
    return _clean_html_text(_node_text(node))


def _find_link(node: _HtmlNode, id_contains: str = "") -> Optional[_HtmlNode]:
    """Find an anchor tag within *node* whose id contains *id_contains*."""
    for a in _find_all(node, "a"):
        aid = (a.attrs.get("id") or "")
        if id_contains in aid:
            return a
    return None


# ── Meeting list extraction ──

def parse_meetings_from_html(html: str) -> list[dict]:
    """Parse the RadGrid meeting table from Calendar.aspx or DepartmentDetail.aspx.

    Returns a list of meeting dicts with keys:
      - body_name            : full body name from the Name column
      - body_slug            : normalized body slug
      - body_code            : short body code
      - meeting_date         : MM/DD/YYYY
      - meeting_time         : HH:MM AM/PM
      - meeting_location     : venue name
      - meeting_id           : Legistar meeting ID
      - meeting_guid         : Legistar meeting GUID
      - meeting_detail_url   : MeetingDetail.aspx URL
      - agenda_url           : View.ashx?M=A URL
      - accessible_agenda_url: View.ashx?M=AADA URL
      - minutes_url          : View.ashx?M=M URL
      - agenda_packet_url    : View.ashx?M=AP URL
      - video_url            : video link URL
      - source_url           : URL of the page this was parsed from
    """
    root = _parse_html(html)
    meetings: list[dict] = []

    # Find the RadGrid master table
    table = _find_radgrid_table(root)
    if table is None:
        log.warning("No RadGrid table found in HTML")
        return meetings

    # Find data rows (skip header rows)
    rows = [tr for tr in _find_all(table, "tr")
            if "rgRow" in (tr.attrs.get("class") or "")
            or "rgAltRow" in (tr.attrs.get("class") or "")]

    log.debug("Found %d meeting rows in RadGrid", len(rows))

    for row in rows:
        cells = [td for td in _find_all(row, "td") if td.tag == "td"]
        if len(cells) < 6:
            continue

        # Column 0: Body Name (with link to DepartmentDetail)
        body_name = ""
        body_dept_id = ""
        body_dept_guid = ""
        body_link = _find_link(cells[0], "hypBody")
        if body_link:
            body_name = _text(body_link)
            href = _attr(body_link, "href")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            body_dept_id = params.get("ID", [""])[0]
            body_dept_guid = params.get("GUID", [""])[0]

        # Column 1: Meeting Date
        meeting_date = _text(cells[1])

        # Column 3: Meeting Time (column 2 is the iCal icon)
        meeting_time = _text(cells[3]) if len(cells) > 3 else ""

        # Column 4: Meeting Location
        meeting_location = _text(cells[4]) if len(cells) > 4 else ""

        # Column 5: Meeting Details link
        meeting_id = ""
        meeting_guid = ""
        meeting_detail_url = ""
        detail_link = _find_link(cells[5], "hypMeetingDetail")
        if detail_link:
            href = _attr(detail_link, "href")
            if href and "MeetingDetail.aspx" in href:
                meeting_detail_url = urllib.parse.urljoin(BASE_URL, href)
                params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                meeting_id = params.get("ID", [""])[0]
                meeting_guid = params.get("GUID", [""])[0]

        if not meeting_id:
            log.debug("Skipping row without meeting ID: %s", body_name)
            continue

        # Column 6: Agenda link
        agenda_url = ""
        agenda_link = _find_link(cells[6], "hypAgenda") if len(cells) > 6 else None
        if agenda_link:
            href = _attr(agenda_link, "href")
            if href:
                agenda_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 7: Accessible Agenda
        accessible_agenda_url = ""
        aa_link = _find_link(cells[7], "hypAccessibleAgendaHTML") if len(cells) > 7 else None
        if aa_link:
            href = _attr(aa_link, "href")
            if href:
                accessible_agenda_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 8: Agenda Packet
        agenda_packet_url = ""
        ap_link = _find_link(cells[8], "hypAgendaPacket") if len(cells) > 8 else None
        if ap_link:
            href = _attr(ap_link, "href")
            if href and "Not" not in href:
                agenda_packet_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 9: Minutes
        minutes_url = ""
        minutes_link = _find_link(cells[9], "hypMinutes") if len(cells) > 9 else None
        if minutes_link:
            href = _attr(minutes_link, "href")
            if href and "Not" not in href:
                minutes_url = urllib.parse.urljoin(BASE_URL, href)

        # Column 11: Video
        video_url = ""
        video_link = _find_link(cells[11], "hypVideo") if len(cells) > 11 else None
        if video_link:
            href = _attr(video_link, "href")
            if href and "Not" not in href and href != "#":
                video_url = urllib.parse.urljoin(BASE_URL, href)

        body_slug = _resolve_body_slug(body_name)
        body_code = BODY_CODE_MAP.get(body_slug, "glendale-cc")

        m = {
            "body_name": body_name,
            "body_slug": body_slug,
            "body_code": body_code,
            "body_dept_id": body_dept_id,
            "body_dept_guid": body_dept_guid,
            "meeting_date": normalize_meeting_date(meeting_date) or meeting_date,
            "meeting_time": meeting_time,
            "meeting_location": meeting_location,
            "meeting_id": meeting_id,
            "meeting_guid": meeting_guid,
            "meeting_detail_url": meeting_detail_url,
            "agenda_url": agenda_url,
            "accessible_agenda_url": accessible_agenda_url,
            "minutes_url": minutes_url,
            "agenda_packet_url": agenda_packet_url,
            "video_url": video_url,
        }
        meetings.append(m)

    return meetings


def _find_radgrid_table(root: _HtmlNode) -> Optional[_HtmlNode]:
    """Find the RadGrid master table in the parsed HTML tree.

    Legistar RadGrid has class ``rgMasterTable`` and id containing
    ``gridCalendar`` or ``gridMeetings``.
    """
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        tbl_id = table.attrs.get("id") or ""
        if "rgMasterTable" in cls and ("gridCalendar" in tbl_id or "gridMeetings" in tbl_id):
            return table
    # Fallback: find any table with rgMasterTable class
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        if "rgMasterTable" in cls:
            return table
    return None


def _resolve_body_slug(body_name: str) -> str:
    """Normalize a Legistar body name to our slug."""
    key = body_name.lower().strip()
    # Direct match
    if key in BODY_SLUG_MAP:
        return BODY_SLUG_MAP[key]
    # Partial match by prefix
    for pattern, slug in sorted(BODY_SLUG_MAP.items(), key=lambda x: -len(x[0])):
        if key.startswith(pattern):
            return slug
    log.debug("Unknown Glendale body name: '%s', defaulting to glendale-city-council", body_name)
    return "glendale-city-council"


def meetings_for_body(meetings: list[dict], body_slug: str) -> list[dict]:
    """Filter meeting list to only those belonging to a specific body."""
    return [m for m in meetings if m.get("body_slug") == body_slug]


# ── Agenda item extraction ──

def parse_agenda_items_from_html(html: str, meeting_id: str,
                                  body_code: str = "glendale-cc") -> list[dict]:
    """Parse agenda items from a MeetingDetail.aspx HTML page.

    The MeetingDetail page shows items in an HTML table with columns:
      File#, Agenda#, Type, Title, Action Result, Action Details.

    Returns a list of item dicts with keys:
      - meeting_id
      - agenda_item_number   : e.g. "2", "3-a", "4-a"
      - file_number          : e.g. "26-0034"
      - item_type            : e.g. "Minutes", "Contract", "Resolution", "Ordinance"
      - agenda_item_title
      - agenda_item_text     : description text
      - action_result        : e.g. "Not available", "Approved"
      - legislation_id       : Legistar legislation ID
      - legislation_guid     : Legistar legislation GUID
      - legislation_url      : LegislationDetail.aspx URL
      - item_type_category   : "section" for headers, "item" for action items
      - section_level        : nested depth
    """
    root = _parse_html(html)
    items: list[dict] = []

    # The MeetingDetail page renders agenda items in a RadGrid with id
    # containing "gridLegislation" or "gridMain"
    grid = _find_legislation_grid(root)
    if grid is None:
        log.debug("No legislation grid found for meeting %s", meeting_id)
        return items

    rows = [tr for tr in _find_all(grid, "tr")
            if "rgRow" in (tr.attrs.get("class") or "")
            or "rgAltRow" in (tr.attrs.get("class") or "")]

    log.debug("Found %d agenda item rows for meeting %s", len(rows), meeting_id)

    sort_order = 0
    for row in rows:
        cells = [td for td in _find_all(row, "td") if td.tag == "td"]
        if len(cells) < 4:
            continue

        # Column 0: File# (link to LegislationDetail)
        file_number = _text(cells[0])
        legislation_id = ""
        legislation_guid = ""
        legislation_url = ""
        file_link = _find_link(cells[0])
        if file_link:
            href = _attr(file_link, "href")
            if href and "LegislationDetail.aspx" in href:
                legislation_url = urllib.parse.urljoin(BASE_URL, href)
                params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                legislation_id = params.get("ID", [""])[0]
                legislation_guid = params.get("GUID", [""])[0]

        # Column 1: Agenda#
        agenda_number = _text(cells[1])

        # Column 2: Type (Minutes, Liquor License, Contract, Resolution, Ordinance, etc.)
        item_type = _text(cells[2])

        # Column 3: Title
        title = _text(cells[3])

        # Column 6 (last): Action Details / Result
        action_result = ""
        if len(cells) > 6:
            action_link = _find_link(cells[6])
            if action_link:
                action_result = _text(action_link)

        sort_order += 1

        item = {
            "meeting_id": meeting_id,
            "agenda_item_number": agenda_number,
            "file_number": file_number,
            "item_type": item_type,
            "agenda_item_title": title,
            "agenda_item_text": "",
            "action_result": action_result,
            "legislation_id": legislation_id,
            "legislation_guid": legislation_guid,
            "legislation_url": legislation_url,
            "item_type_category": "item",
            "section_level": 0,
            "sort_order": sort_order,
        }
        items.append(item)

    return items


def _find_legislation_grid(root: _HtmlNode) -> Optional[_HtmlNode]:
    """Find the legislation RadGrid table in the MeetingDetail HTML.

    The grid is typically named ``gridMain`` or ``gridLegislation``,
    both of which have the ``rgMasterTable`` class.
    """
    for table in _find_all(root, "table"):
        cls = table.attrs.get("class") or ""
        tbl_id = table.attrs.get("id") or ""
        if "rgMasterTable" in cls and ("gridMain" in tbl_id or "gridLegislation" in tbl_id):
            return table
    return None


def _split_title_and_action(text: str) -> tuple[str, str]:
    """Split agenda item cell text into title and action result.

    In the Legistar MeetingDetail table, the Title column may contain
    both the item title and the action result (on a new line).
    """
    text = text.strip()
    parts = re.split(r"\s{3,}|\n|\r", text, maxsplit=1)
    if len(parts) == 2:
        title = parts[0].strip()
        action = parts[1].strip()
        return title, action
    return text, ""


# ── Legislation detail extraction ──

def parse_legislation_detail_from_html(html: str) -> dict:
    """Parse a LegislationDetail.aspx page for item details and attachments.

    Returns a dict with keys:
      - file_number
      - item_type
      - status
      - title
      - attachments : list of dicts [{title, url}]
    """
    root = _parse_html(html)
    result: dict = {
        "file_number": "",
        "item_type": "",
        "status": "",
        "title": "",
        "attachments": [],
    }

    # Find the detail fields
    # Legistar uses label-value pairs like "File #:", "Type:", "Status:", "Title:"
    all_spans = _find_all(root, "span")
    for span in all_spans:
        span_text = _clean_html_text(_node_text(span)).strip()

        if span_text.startswith("File #") or span_text.startswith("File #:"):
            parent = span.parent
            if parent:
                full_text = _clean_html_text(_node_text(parent))
                m = re.search(r"File\s*#:\s*(.+)", full_text)
                if m:
                    result["file_number"] = m.group(1).strip()

        elif span_text.startswith("Type:"):
            parts = span_text.split(":", 1)
            if len(parts) > 1:
                result["item_type"] = parts[1].strip()
            parent = span.parent
            if parent:
                for child in parent.children:
                    if isinstance(child, _HtmlNode) and child.tag == "a":
                        result["item_type"] = _text(child)

        elif span_text.startswith("Status:"):
            result["status"] = span_text.split(":", 1)[1].strip() if ":" in span_text else ""

        elif span_text.startswith("Title:"):
            result["title"] = span_text.split(":", 1)[1].strip() if ":" in span_text else ""

    # Find attachments under "Attachments:" header
    # Attachments are <a> tags with href containing View.ashx?M=F
    for a in _find_all(root, "a"):
        href = _attr(a, "href")
        if "View.ashx?M=F" in href:
            title = _text(a)
            if title and title not in ("Not available", "Not available "):
                result["attachments"].append({
                    "title": title,
                    "url": urllib.parse.urljoin(BASE_URL, href),
                })

    return result


# ── Meeting title / type helpers ──

def normalize_meeting_title(raw_title: str) -> str:
    """Normalize a Glendale meeting title."""
    title = raw_title.replace("\u2013", "-").strip()
    title = re.sub(r"^CANCEL(?:LED|ED)?\s*-\s*", "", title, flags=re.IGNORECASE)
    return title.strip()


def extract_meeting_type(title: str) -> str:
    """Derive a meeting type from the meeting title or body name."""
    tl = title.lower()
    if "workshop" in tl:
        return "Workshop"
    if "study session" in tl:
        return "Study Session"
    if "strategic planning" in tl:
        return "Strategic Planning Session"
    if "executive session" in tl:
        return "Executive Session"
    if "special" in tl:
        return "Special Meeting"
    if "public hearing" in tl:
        return "Public Hearing"
    if "regular" in tl or tl in ("city council",):
        return "Regular Meeting"
    return "Regular Meeting"


# ── HTTP helpers ──

def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch an HTML page from Glendale Legistar.

    Uses urllib with a reasonable User-Agent header.
    """
    import urllib.request
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


# ── Playwright-based search ──

async def search_glendale_meetings(
    page,
    body_slugs: Optional[list[str]] = None,
) -> list[dict]:
    """Search for Glendale meetings on the Legistar Calendar page via Playwright.

    Glendale's Calendar.aspx requires JavaScript for the RadComboBox body filter
    and tab switching.  This function:

      1. Navigates to Calendar.aspx
      2. Switches to the "List View" tab if needed
      3. Optionally selects a body from the body dropdown (lstBodies)
      4. Parses the resulting RadGrid HTML table

    Parameters
    ----------
    page : playwright.async_api.Page
        Playwright page instance.
    body_slugs : list[str], optional
        Only return meetings for these body slugs.  Defaults to
        ``["glendale-city-council"]``.  Pass an empty list or None to
        return all bodies.

    Returns
    -------
    list[dict]
        Meeting dicts as returned by ``parse_meetings_from_html()``.
    """
    if body_slugs is None:
        body_slugs = DEFAULT_BODY_SLUGS

    await page.goto(CALENDAR_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(1)

    # ── Switch to List View tab ──
    # The Calendar page has tabs: "List View" (tab index 0) and
    # "Calendar View" (tab index 1).  We need the List View.
    try:
        # The RadTab strip has id containing "tabCalendar"
        # Tab items are <li> elements inside a <ul> with class "rtsLI"
        # List View is the first tab (index 0)
        tabs = await page.query_selector_all("#tabCalendar ul.rtsTabs li.rtsLI")
        if tabs and len(tabs) >= 1:
            # Check if we're already on List View by looking at selected class
            is_selected = await tabs[0].get_attribute("class")
            if is_selected and "rtsSelected" not in (is_selected or ""):
                await tabs[0].click()
                await asyncio.sleep(1.5)
                await page.wait_for_load_state("networkidle", timeout=15000)
                log.info("Switched to List View tab")
            else:
                log.debug("Already on List View tab")
    except Exception as e:
        log.warning("Could not switch to List View tab: %s", e)

    # ── Select body from dropdown ──
    # Glendale's body dropdown has id ctl00_ContentPlaceHolder1_lstBodies.
    # The options are typically: "All Bodies", "City Council", "City Council Workshop"
    # We need to map our body_slugs to the Legistar body names.
    if body_slugs:
        try:
            # Try to select the first matching body from the RadComboBox
            # First click the dropdown arrow to open it
            arrow = await page.query_selector("#ctl00_ContentPlaceHolder1_lstBodies_Arrow")
            if arrow:
                await arrow.click()
                await asyncio.sleep(0.5)

                # The dropdown items are <li> elements with class "rcbItem"
                items = await page.query_selector_all(
                    "#ctl00_ContentPlaceHolder1_lstBodies_DropDown li.rcbItem, "
                    "#ctl00_ContentPlaceHolder1_lstBodies_DropDown div.rcbItem"
                )
                for item in items:
                    item_text = await item.inner_text()
                    item_text = item_text.strip().lower()

                    # Determine which body name to look for
                    for slug in body_slugs:
                        if slug == "glendale-city-council" and "city council" in item_text and "workshop" not in item_text:
                            await item.click()
                            await asyncio.sleep(2)
                            await page.wait_for_load_state("networkidle", timeout=15000)
                            log.info("Selected body: %s", item_text)
                            break
                        elif slug == "glendale-city-council-workshop" and "workshop" in item_text:
                            await item.click()
                            await asyncio.sleep(2)
                            await page.wait_for_load_state("networkidle", timeout=15000)
                            log.info("Selected body: %s", item_text)
                            break
        except Exception as e:
            log.warning("Could not select body from dropdown: %s", e)

    await asyncio.sleep(1)
    html = await page.content()
    all_meetings = parse_meetings_from_html(html)

    # Filter to requested bodies (by slug, since the dropdown should have
    # narrowed it down server-side, but we also filter locally just in case)
    filtered = [m for m in all_meetings if m.get("body_slug") in body_slugs]

    log.info(
        "Found %d Glendale meeting(s) via Playwright (%d total)",
        len(filtered), len(all_meetings),
    )

    # Enrich with meeting_type and meeting_title
    for m in filtered:
        m["meeting_type"] = extract_meeting_type(m.get("body_name", ""))
        m["meeting_title"] = normalize_meeting_title(m.get("body_name", ""))

    return filtered


async def search_glendale_meetings_for_body(
    page, body_slug: str = "glendale-city-council"
) -> list[dict]:
    """Search for Glendale meetings for a specific body via Playwright.

    Convenience wrapper around ``search_glendale_meetings()``.
    """
    return await search_glendale_meetings(page, body_slugs=[body_slug])


# ── Agenda item fetching ──

async def fetch_agenda_items_with_playwright(
    page, meeting_detail_url: str, meeting_id: str, body_code: str = "glendale-cc"
) -> list[dict]:
    """Fetch and parse agenda items from a MeetingDetail page via Playwright.

    Parameters
    ----------
    page : playwright.async_api.Page
    meeting_detail_url : str
        Full URL to the MeetingDetail.aspx page.
    meeting_id : str
        Legistar meeting ID.
    body_code : str
        Body code for the meeting.

    Returns
    -------
    list[dict]
        Agenda item dicts.
    """
    await page.goto(meeting_detail_url, wait_until="networkidle", timeout=30000)
    html = await page.content()
    return parse_agenda_items_from_html(html, meeting_id, body_code)


async def fetch_agenda_items_async(
    meeting_detail_url: str, meeting_id: str, body_code: str = "glendale-cc"
) -> list[dict]:
    """Fetch and parse agenda items from a MeetingDetail page via plain HTTP.

    Returns an empty list on 410/404 (meeting not available on Legistar anymore).

    Parameters
    ----------
    meeting_detail_url : str
        Full URL to the MeetingDetail.aspx page.
    meeting_id : str
        Legistar meeting ID.
    body_code : str
        Body code for the meeting.

    Returns
    -------
    list[dict]
        Agenda item dicts.
    """
    try:
        html = fetch_page(meeting_detail_url)
        return parse_agenda_items_from_html(html, meeting_id, body_code)
    except Exception as e:
        err_str = str(e)
        if "410" in err_str or "404" in err_str or "Gone" in err_str:
            log.warning("Meeting detail page not available (410/404) for %s: %s", meeting_id, meeting_detail_url)
            return []
        raise


# ── Minutes PDF vote extraction ──

def fetch_minutes_pdf_bytes(minutes_url: str) -> Optional[bytes]:
    """Download a Glendale Minutes PDF from a View.ashx?M=M URL.

    Returns raw PDF bytes, or None on failure (including 410 Gone).
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            minutes_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Minutes PDF not available for %s: %s", minutes_url, e)
        return None


def extract_minutes_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a Glendale Minutes PDF using pdftotext."""
    import subprocess
    import tempfile
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
        log.warning("pdftotext failed for Glendale minutes: %s", e)
        return None
    finally:
        import os
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


# ── Year-based search via ASP.NET POST ──

def _extract_aspnet_form_fields(html: str) -> dict[str, str]:
    """Extract ASP.NET WebForms hidden fields from a page."""
    fields = {}
    for name in (
        "__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR",
        "__PREVIOUSFOCUSED", "__CT100", "__LASTFOCUS",
    ):
        m = re.search(
            rf'(?:id|name)="{name}"[^>]*value="([^"]*)"', html
        )
        if m:
            fields[name] = m.group(1)
        else:
            fields[name] = ""
    return fields


def _build_body_client_state(body_label: str) -> str:
    """Build a Telerik RadComboBox ClientState JSON string for a body selection."""
    import json
    state = {
        "logEntries": [],
        "value": body_label,
        "text": body_label,
        "enabled": True,
        "checkedIndices": [],
        "checkedItemsTextOverflows": False,
    }
    return json.dumps(state)


def search_glendale_meetings_by_body(body_label: str) -> list[dict]:
    """Search Glendale Legistar for meetings matching a body using ASP.NET POST.

    Fetches the Calendar.aspx page, extracts the ASP.NET form fields, then
    POSTs back with the body RadComboBox set to the requested body.

    Parameters
    ----------
    body_label : str
        The label text to select in the body dropdown (e.g. "City Council").

    Returns
    -------
    list[dict]
        Meeting dicts as returned by ``parse_meetings_from_html()``.
    """
    import urllib.request

    # Step 1: GET the Calendar page
    html = fetch_page(CALENDAR_URL)
    fields = _extract_aspnet_form_fields(html)

    client_state = _build_body_client_state(body_label)

    # Step 2: POST with the body selection
    form_data = [
        ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
        ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
        ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
        ("ctl00_ContentPlaceHolder1_lstBodies_ClientState", client_state),
        ("ctl00_ContentPlaceHolder1_lstBodies_Input", body_label),
        ("ctl00_ContentPlaceHolder1_txtSearch", ""),
        ("__EVENTTARGET", "ctl00$ContentPlaceHolder1$lstBodies"),
        ("__EVENTARGUMENT", ""),
        ("__PREVIOUSFOCUSED", fields.get("__PREVIOUSFOCUSED", "")),
        ("__CT100", fields.get("__CT100", "")),
    ]

    data = urllib.parse.urlencode(form_data).encode("utf-8")

    req_fetch = urllib.request.Request(
        CALENDAR_URL,
        data=data,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urllib.request.urlopen(req_fetch, timeout=30) as resp:
            result_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to POST body search for '%s': %s", body_label, e)
        raise

    meetings = parse_meetings_from_html(result_html)
    log.info(
        "Found %d Glendale meeting(s) for body '%s'",
        len(meetings), body_label,
    )
    return meetings


# ── Top-level search ──

def search_glendale_meetings_sync(
    body_slugs: Optional[list[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Search for Glendale meetings using synchronous HTTP.

    Uses ASP.NET POST to filter by the first body slug in *body_slugs*.
    Falls back to a simple GET if POST filtering fails.

    If *start_date* and *end_date* are provided (ISO-8601), results are
    post-filtered to that date range since Legistar doesn't expose a
    server-side date filter.

    Parameters
    ----------
    body_slugs : list[str], optional
        Body slugs to include.  Defaults to ``["glendale-city-council"]``.
    start_date : str, optional
        ISO-8601 start date (e.g. "2026-06-30").  Requires *end_date*.
    end_date : str, optional
        ISO-8601 end date (e.g. "2026-07-03").  Requires *start_date*.

    Returns
    -------
    list[dict]
        Meeting dicts.
    """
    if body_slugs is None:
        body_slugs = DEFAULT_BODY_SLUGS

    # Map the first body slug to a Legistar body label
    slug_to_label = {
        "glendale-city-council": "City Council",
        "glendale-city-council-workshop": "City Council Workshop",
    }

    body_label = slug_to_label.get(body_slugs[0]) if body_slugs else None

    try:
        if body_label:
            meetings = search_glendale_meetings_by_body(body_label)
        else:
            html = fetch_page(CALENDAR_URL)
            meetings = parse_meetings_from_html(html)
    except Exception:
        log.warning("POST body search failed, falling back to simple GET")
        html = fetch_page(CALENDAR_URL)
        meetings = parse_meetings_from_html(html)

    # Filter to requested body slugs
    filtered = [m for m in meetings if m.get("body_slug") in body_slugs]

    # Post-filter by date range if provided
    if start_date and end_date:
        sd_str = start_date.replace("-", "")
        ed_str = end_date.replace("-", "")

        def _meeting_key(md: str) -> str:
            """Turn a Legistar meeting date (MM/DD/YYYY) into YYYYMMDD."""
            parts = md.split("/")
            if len(parts) == 3:
                return f"{parts[2]}{int(parts[0]):02d}{int(parts[1]):02d}"
            return md.replace("-", "")

        filtered = [
            m for m in filtered
            if sd_str <= _meeting_key(m.get("meeting_date", "")) <= ed_str
        ]

    log.info(
        "Found %d Glendale meeting(s) via sync HTTP (%d total, %s)",
        len(filtered), len(meetings),
        f"date range {start_date}–{end_date}" if start_date else "no date filter",
    )
    return filtered


# ── Main / CLI entry point ──

def main():
    """CLI entry point for testing the Glendale Legistar scraper.

    Usage:  python scripts/scraper/glendale.py [--year=YYYY] [--body=BODY_SLUG]

    Examples:
        python scripts/scraper/glendale.py
        python scripts/scraper/glendale.py --year=2025
        python scripts/scraper/glendale.py --body=glendale-city-council
        python scripts/scraper/glendale.py --body=glendale-city-council-workshop
        python scripts/scraper/glendale.py --year=2025 --detail
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Test the Glendale Legistar scraper",
    )
    parser.add_argument("--year", type=int, default=None, help="Year to search (e.g. 2025)")
    parser.add_argument("--body", default="glendale-city-council",
                        help="Body slug (default: glendale-city-council)")
    parser.add_argument("--detail", action="store_true",
                        help="Also fetch agenda item details")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max meetings to process")
    parser.add_argument("--headed", action="store_true",
                        help="Run Playwright in headed mode (requires browser)")
    parser.add_argument("--http", action="store_true",
                        help="Use synchronous HTTP instead of Playwright")

    args = parser.parse_args()

    body_slugs = [args.body]

    if args.http:
        # Use synchronous HTTP mode
        meetings = search_glendale_meetings_sync(body_slugs=body_slugs)
    else:
        # Use Playwright (async)
        import asyncio
        from playwright.async_api import async_playwright

        async def _run():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=not args.headed)
                ctx = await browser.new_context()
                page = await ctx.new_page()
                try:
                    meetings = await search_glendale_meetings(page, body_slugs=body_slugs)
                finally:
                    await browser.close()
                return meetings

        meetings = asyncio.run(_run())

    if args.limit and len(meetings) > args.limit:
        meetings = meetings[:args.limit]

    if not meetings:
        print("No meetings found.")
        return

    print(f"\nFound {len(meetings)} Glendale meeting(s):")
    print("=" * 100)

    for m in meetings:
        print(f"\n  Body:        {m.get('body_name', '')}")
        print(f"  Slug:        {m.get('body_slug', '')}")
        print(f"  Code:        {m.get('body_code', '')}")
        print(f"  Date:        {m.get('meeting_date', '')}")
        print(f"  Time:        {m.get('meeting_time', '')}")
        print(f"  Location:    {m.get('meeting_location', '')}")
        print(f"  Meeting ID:  {m.get('meeting_id', '')}")
        print(f"  Detail URL:  {m.get('meeting_detail_url', '')}")
        print(f"  Agenda URL:  {m.get('agenda_url', '')}")
        print(f"  Minutes URL: {m.get('minutes_url', '')}")
        print(f"  Packet URL:  {m.get('agenda_packet_url', '')}")
        print(f"  Video URL:   {m.get('video_url', '')}")

        # Show meeting type from enriched data
        if m.get("meeting_type"):
            print(f"  Type:        {m.get('meeting_type', '')}")
        if m.get("meeting_title"):
            print(f"  Title:       {m.get('meeting_title', '')}")

    # Fetch detail for first meeting if requested
    if args.detail and meetings:
        first = meetings[0]
        detail_url = first.get("meeting_detail_url")
        meeting_id = first.get("meeting_id")
        if detail_url and meeting_id:
            print(f"\n\nFetching agenda items for meeting {meeting_id}...")
            body_code = first.get("body_code", "glendale-cc")

            try:
                items = asyncio.run(fetch_agenda_items_async(
                    detail_url, meeting_id, body_code
                ))
                print(f"\n  Found {len(items)} agenda item(s):")
                print("-" * 80)
                for item in items:
                    print(f"    #{item.get('agenda_item_number', '')}: "
                          f"{item.get('item_type', '')} - "
                          f"{item.get('agenda_item_title', '')[:80]}")
                    if item.get("file_number"):
                        print(f"       File: {item.get('file_number', '')}")
                    if item.get("legislation_url"):
                        print(f"       Leg:  {item.get('legislation_url', '')}")
            except Exception as e:
                print(f"  Error fetching agenda items: {e}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    main()
