"""
City of Phoenix meeting extraction via Legistar.

Phoenix uses the Legistar agenda management system at ``phoenix.legistar.com``.
Same platform as Mesa (``mesa.legistar.com``).

Council meetings, Planning Commission, and subcommittees are all available
through the Calendar.aspx page. Agenda items are on MeetingDetail.aspx.
"""

from __future__ import annotations
import asyncio
import logging
import re
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──

PUBLIC_BODY_CODE = "phoenix-cc"
DEFAULT_BODY_SLUGS = ["phoenix-city-council"]

BASE_URL = "https://phoenix.legistar.com"
CALENDAR_URL = f"{BASE_URL}/Calendar.aspx"
SOURCE_INSTANCE_URL = BASE_URL
SOURCE_SYSTEM = "legistar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Body name → (slug, code)
BODY_SLUG_MAP: dict[str, tuple[str, str]] = {
    "city council formal meeting": ("phoenix-city-council", "phoenix-cc"),
    "city council policy session": ("phoenix-city-council", "phoenix-cc"),
    "city council special meeting": ("phoenix-city-council", "phoenix-cc"),
    "city council work study session": ("phoenix-city-council", "phoenix-cc"),
    "planning commission": ("phoenix-planning-commission", "phoenix-pc"),
    "community services and education subcommittee": ("phoenix-community-services-sub", "phoenix-cs"),
    "economic development and the arts subcommittee": ("phoenix-economic-dev-sub", "phoenix-ed"),
    "public safety and justice subcommittee": ("phoenix-public-safety-sub", "phoenix-ps"),
    "transportation, infrastructure, and planning subcommittee": ("phoenix-transportation-sub", "phoenix-ti"),
    "general information packet": ("phoenix-general-packet", "phoenix-gp"),
    "subcommittee general information packet": ("phoenix-sub-packet", "phoenix-sp"),
    "virtual community budget hearing": ("phoenix-budget-hearing", "phoenix-bh"),
}

DEFAULT_BODY_SLUGS = ["phoenix-city-council"]


def _resolve_body(body_name: str) -> tuple[str, str, str]:
    lower = body_name.lower().strip()
    for pattern, (slug, code) in BODY_SLUG_MAP.items():
        if lower == pattern or lower.startswith(pattern):
            return slug, code, body_name.strip()
    return "phoenix-city-council", "phoenix-cc", body_name.strip()


# ── HTTP helpers ──

def fetch_page(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def fetch_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Failed to download %s: %s", url, e)
        return None


def _extract_aspnet_form_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                  "__PREVIOUSFOCUSED", "__CT100"]:
        m = re.search(r'id=["\']' + name + r'["\'][^>]*value=["\']([^"\']*)["\']', html)
        if m:
            fields[name] = m.group(1)
        m2 = re.search(r'name=["\']' + name + r'["\'][^>]*value=["\']([^"\']*)["\']', html)
        if m2 and name not in fields:
            fields[name] = m2.group(1)
    return fields


def _build_year_client_state(year: str) -> str:
    return (
        '{"logEntries":[],"value":"","text":"' + year + '",'
        '"enabled":true,"checkedIndices":[],"checkedItemsTextOverflows":false}'
    )


# ── Parsing helpers ──

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text


def _text(node) -> str:
    if node is None:
        return ""
    return _clean_html_text(_node_text(node))


def _attr(node, key: str) -> str:
    return (node.attrs.get(key) or "").strip()


def _find_link(cell, id_contains: str = "") -> Optional[object]:
    for a in _find_all(cell, "a"):
        href = _attr(a, "href")
        if href and (not id_contains or id_contains in href):
            return a
    return None


# ── Meeting discovery ──

def _parse_meetings_from_html(html: str) -> list[dict]:
    meetings: list[dict] = []
    rows = re.findall(
        r'<tr[^>]*class="rgRow[^"]*"[^>]*>(.*?)</tr>',
        html, re.DOTALL
    )
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 5:
            continue

        body_name = re.sub(r"<[^>]+>", " ", cells[0]).strip()
        date_raw = re.sub(r"<[^>]+>", " ", cells[1]).strip()
        details_html = cells[4] if len(cells) > 4 else ""
        ical_html = cells[2] if len(cells) > 2 else ""

        if not body_name or not date_raw:
            continue

        slug, code, mtype = _resolve_body(body_name)

        meeting_date = ""
        for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
            try:
                from datetime import datetime
                meeting_date = datetime.strptime(date_raw, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if not meeting_date:
            meeting_date = date_raw

        # Extract meeting ID and GUID from iCal link (available even when
        # the Details link is marked "Not viewable by the public")
        meeting_id = ""
        meeting_guid = ""
        ical_link = re.search(r'href="([^"]*)"[^>]*id="[^"]*hypiCal', ical_html, re.I)
        if ical_link:
            qs = ical_link.group(1)
            # Handle HTML-encoded ampersands (&amp;)
            qs = qs.replace("&amp;", "&")
            m_id = re.search(r'[?&]ID=(\d+)', qs)
            if m_id:
                meeting_id = m_id.group(1)
            m_guid = re.search(r'[?&]GUID=([^&]+)', qs)
            if m_guid:
                meeting_guid = m_guid.group(1)

        # Build MeetingDetail URL from ID and GUID
        meeting_detail_url = ""
        if meeting_id and meeting_guid:
            meeting_detail_url = (
                f"{BASE_URL}/MeetingDetail.aspx?ID={meeting_id}"
                f"&GUID={meeting_guid}&Options=info|"
            )
        # Fall back to the Details hypMeetingDetail link if viewable
        if not meeting_detail_url:
            details_link = re.search(r'href="([^"]*MeetingDetail\.aspx[^"]*)"', details_html)
            if details_link:
                meeting_detail_url = urllib.parse.urljoin(BASE_URL, details_link.group(1))

        meetings.append({
            "meeting_id": meeting_id or f"{body_name}-{date_raw}",
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": body_name,
            "body_slug": slug,
            "body_code": code,
            "meeting_guid": meeting_guid,
            "meeting_detail_url": meeting_detail_url,
            "source_url": meeting_detail_url or f"{BASE_URL}/Calendar.aspx",
        })

    return meetings


def search_phoenix_meetings(year: int, body_slugs: Optional[list[str]] = None) -> list[dict]:
    """Search Phoenix Legistar for meetings in a given year."""
    year_label = str(year)
    html = fetch_page(CALENDAR_URL)
    fields = _extract_aspnet_form_fields(html)
    client_state = _build_year_client_state(year_label)

    form_data = [
        ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
        ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
        ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
        ("ctl00_ContentPlaceHolder1_lstYears_ClientState", client_state),
        ("ctl00_ContentPlaceHolder1_lstYears_Input", year_label),
        ("ctl00_ContentPlaceHolder1_txtSearch", ""),
        ("__EVENTTARGET", "ctl00$ContentPlaceHolder1$lstYears"),
        ("__EVENTARGUMENT", ""),
    ]

    data = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(
        CALENDAR_URL, data=data,
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to POST year search for %s: %s", year_label, e)
        raise

    meetings = _parse_meetings_from_html(result_html)

    if body_slugs:
        meetings = [m for m in meetings if m["body_slug"] in body_slugs]

    return meetings


# ── Agenda item extraction ──

def _find_legislation_grid(root) -> Optional[object]:
    """Find the legislation RadGrid table in the MeetingDetail HTML."""
    # Look for tables whose id contains "gridLegislation"
    for table in _find_all(root, "table"):
        tid = table.attrs.get("id", "")
        if "gridLegislation" in tid:
            return table
    return None


def parse_agenda_items_from_html(html: str, meeting_id: str,
                                  body_code: str = "phoenix-cc") -> list[dict]:
    """Parse agenda items from a MeetingDetail.aspx HTML page.

    Phoenix MeetingDetail pages list items in a legislation RadGrid.
    """
    root = _parse_html(html)
    items: list[dict] = []

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

        # Column 2: Type (Resolution, Ordinance, Contract, etc.)
        item_type = _text(cells[2]) if len(cells) > 2 else ""

        # Column 3: Title
        title = _text(cells[3]) if len(cells) > 3 else ""

        # Column 4: Action Result (may be empty or "Not available")
        action_result = _text(cells[4]) if len(cells) > 4 else ""

        if not title and not file_number:
            continue

        sort_order += 1
        items.append({
            "meeting_id": meeting_id,
            "agenda_item_number": agenda_number,
            "file_number": file_number,
            "item_type": item_type,
            "agenda_item_title": title,
            "agenda_item_text": f"{item_type}: {title}" if item_type else title,
            "action_result": action_result,
            "legislation_id": legislation_id,
            "legislation_guid": legislation_guid,
            "legislation_url": legislation_url,
            "item_type_category": "item",
            "section_level": 0,
            "sort_order": sort_order,
        })

    return items


async def fetch_agenda_items_async(
    detail_url: str, meeting_id: str, body_code: str = "phoenix-cc"
) -> list[dict]:
    """Fetch and parse agenda items from a MeetingDetail page via plain HTTP."""
    try:
        html = fetch_page(detail_url, timeout=30)
        return parse_agenda_items_from_html(html, meeting_id, body_code)
    except Exception as e:
        log.warning("Failed to fetch agenda items for %s: %s", meeting_id, e)
        return []
