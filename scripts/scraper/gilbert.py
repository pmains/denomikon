"""
Town of Gilbert meeting extraction via OnBase Agenda Online.

Gilbert uses OnBase at ``gilbertaz.databankcloud.com/GilbertAgendaOnline``.
Unlike Tempe/Maricopa, search results are embedded as JSON in the page
inside a ``showSearchResults(new SearchResults({...}))`` script call.

Usage:
    ./scrape gilbert --sync [--start-date=MM/DD/YYYY] [--end-date=MM/DD/YYYY]
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Optional

log = logging.getLogger(__name__)

PUBLIC_BODY_CODE = "gilbert-tc"

BASE_URL = "https://gilbertaz.databankcloud.com/GilbertAgendaOnline"
SEARCH_URL = f"{BASE_URL}/Meetings/Search"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# OnBase meeting type IDs for Gilbert
# 101=Regular/Special, 102=Study Session, 103=Subcommittee, 105=Water Resources MPC,
# 106=Public Facilities MPC, 107=Redevelopment Commission
ALL_TYPE_IDS = [101, 102, 103, 105, 106, 107]

# Map meeting type names to body slugs/codes
TYPE_NAME_MAP = {
    "Council Regular/Special Meeting": ("gilbert-city-council", "gilbert-tc", "Regular Meeting"),
    "Council Study Session": ("gilbert-city-council", "gilbert-tc", "Study Session"),
    "Council Subcommittee": ("gilbert-city-council", "gilbert-tc", "Subcommittee"),
    "Water Resources Municipal Property Corporation": ("gilbert-water-resources", "gilbert-water", "Water Resources MPC"),
    "Public Facilities Municipal Property Corporation": ("gilbert-public-facilities", "gilbert-pf", "Public Facilities MPC"),
    "Redevelopment Commission": ("gilbert-redevelopment", "gilbert-red", "Redevelopment Commission"),
}

COUNCIL_TYPE_IDS = [101, 102, 103]

BODY_MAP: dict[str, str] = {
    "Council Regular/Special Meeting": "gilbert-tc",
    "Council Study Session": "gilbert-tc",
    "Council Subcommittee": "gilbert-tc",
}


def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def _gilbert_type_to_body(type_name: str) -> tuple[str, str, str]:
    """Map Gilbert meeting type name to (slug, code, meeting_type)."""
    entry = TYPE_NAME_MAP.get(type_name)
    if entry:
        return entry
    return "gilbert-city-council", "gilbert-tc", type_name


def search_gilbert_meetings(start_date: str, end_date: str) -> list[dict]:
    """Search for Gilbert meetings via embedded JSON.

    Searches all known meeting type IDs (Council, MPC, Redevelopment).
    Maps meeting type names to the correct body slug and code.

    Parameters
    ----------
    start_date : str in MM/DD/YYYY format
    end_date : str in MM/DD/YYYY format

    Returns list of meeting dicts.
    """
    url = (
        f"{SEARCH_URL}?dropid=11"
        f"&mtids={urllib.parse.quote(','.join(str(t) for t in ALL_TYPE_IDS))}"
        f"&dropsv={urllib.parse.quote(start_date)}"
        f"&dropev={urllib.parse.quote(end_date)}"
    )

    html = fetch_page(url, timeout=20)
    meetings_json = _extract_meetings_json(html)
    if not meetings_json:
        log.warning("No meetings JSON found in Gilbert Search page")
        return []

    return _parse_meetings(meetings_json)


def _extract_meetings_json(html: str) -> Optional[list]:
    """Extract the meetings array from the embedded JS data."""
    # Find showSearchResults(new SearchResults({...}))
    idx = html.find("showSearchResults(new SearchResults(")
    if idx < 0:
        return None
    # Find the JSON object argument
    start = html.find("{", idx)
    if start < 0:
        return None
    # Match balanced braces
    depth = 0
    end = start
    for i, c in enumerate(html[start:]):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    json_str = html[start:end]
    try:
        data = json.loads(json_str)
        return data.get("Meetings", [])
    except json.JSONDecodeError as e:
        log.warning("Failed to parse Gilbert meeting JSON: %s", e)
        return None


def _parse_meetings(raw_meetings: list) -> list[dict]:
    """Parse raw meeting JSON objects into our standard format."""
    meetings: list[dict] = []
    for mt in raw_meetings:
        mid = str(mt.get("ID", ""))
        if not mid:
            continue
        name = mt.get("Name", "")
        type_name = mt.get("MeetingTypeName", "")
        time_str = mt.get("TimeString", "")
        date_part = time_str.split()[0] if time_str else ""

        slug, code, meeting_type = _gilbert_type_to_body(type_name)

        meetings.append({
            "meeting_id": mid,
            "meeting_date": date_part,
            "meeting_time": " ".join(time_str.split()[1:]) if time_str else "",
            "meeting_title": name,
            "meeting_type": meeting_type,
            "body": code,
            "body_code": code,
            "body_slug": slug,
            "body_name": type_name,
            "agenda_url": f"{BASE_URL}/Meetings/ViewMeetingAgenda?meetingId={mid}&type=agenda",
        })
    return meetings


# Recreate GILBERT_CONFIG here (avoids circular import in main.py)


def fetch_agenda_html(meeting_id: int) -> str:
    """Fetch the agenda HTML for a Gilbert meeting via the ViewAgenda API."""
    import urllib.request
    url = (
        f"https://gilbertaz.databankcloud.com/GilbertAgendaOnline"
        f"/Documents/ViewAgenda?meetingId={meeting_id}&type=agenda&doctype=1"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch Gilbert agenda %s: %s", meeting_id, e)
        raise


def parse_gilbert_agenda(html: str, meeting_id: str) -> list[dict]:
    """Parse Gilbert agenda items from Aspose.Words HTML output.

    Gilbert's agenda is rendered as an HTML document with <p> tags.
    Section headers like CONSENT CALENDAR, CALL TO ORDER, etc. appear
    as bolded text. Numbered items appear as lettered sub-items (a), b)).
    """
    import re

    items: list[dict] = []
    sort_order = 0

    # Remove scripts and styles
    clean = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL)

    # Extract all paragraph text
    paragraphs = []
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", clean, re.DOTALL):
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = text.replace("\u00a0", " ").replace("&nbsp;", " ").strip()
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            paragraphs.append(text)

    # Identify sections and items
    current_section = ""
    for text in paragraphs:
        tl = text.upper().strip()

        # Section headers
        if tl in ("CONSENT CALENDAR",):
            current_section = "Consent"
            continue
        elif tl == "CALL TO ORDER":
            current_section = "Call to Order"
            continue
        elif "AGENDA ITEM" in tl:
            continue
        elif tl.startswith("ADJOURN"):
            current_section = "Adjournment"
            continue
        elif tl == "PUBLIC HEARING":
            current_section = "Public Hearing"
            continue

        # Skip boilerplate
        if any(kw in tl for kw in ("COUNCIL MEETING AGENDA", "REGULAR MEETING",
                                   "MAYOR", "MUNICIPAL BUILDING", "NOTICE:",
                                   "MEETING PROTOCOL", "PUBLIC SPEAKING",
                                   "FOR MORE INFORMATION", "ADDENDUM",
                                   "INVOCATION", "PLEDGE")):
            continue
        if len(text) < 10:
            continue

        # Check for numbered sub-items like "a)", "b)", "1.", "2."
        item_match = re.match(r"^\s*([a-z0-9])\)\s*", text)
        if item_match:
            sort_order += 1
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": item_match.group(1),
                "agenda_item_title": text,
                "agenda_item_text": "",
                "item_type": "",
                "agenda_category": current_section,
                "sort_order": sort_order,
            })
            continue

        # Full items like "CONSIDER: adoption of Resolution..."
        full_match = re.match(r"^([A-Z][A-Z\s]+\s*-\s*consider:?)\s*(.*)", text, re.IGNORECASE)
        if full_match:
            sort_order += 1
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": str(sort_order),
                "agenda_item_title": text,
                "agenda_item_text": "",
                "item_type": "",
                "agenda_category": current_section,
                "sort_order": sort_order,
            })
            continue

        # Any reasonably long paragraph could be an item
        if len(text) > 30 and not any(kw in tl for kw in (
            "COUNCIL MEETING", "\u00a0", "MAYOR ", "MUNICIPAL",
            "GILBERT COUNCIL", "RECORDED", "PARTICIPATE", "EXECUTIVE SESSION",
        )):
            sort_order += 1
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": str(sort_order),
                "agenda_item_title": text[:200],
                "agenda_item_text": "",
                "item_type": "",
                "agenda_category": current_section,
                "sort_order": sort_order,
            })

    return items

def _make_config():
    """Create an OnBaseConfig for the Gilbert instance."""
    from scraper.onbase import OnBaseConfig
    return OnBaseConfig(
        name="Gilbert",
        host="gilbertaz.databankcloud.com",
        base_path="/GilbertAgendaOnline",
        search_path="/Meetings",
        search_method="POST",
        csrf_required=True,
        date_start_field="DateRangeCustomStartDate",
        date_end_field="DateRangeCustomEndDate",
        meeting_type_field="MeetingTypeIDs",
        source_system="hyland_onbase_agenda_online",
        source_instance_url="https://gilbertaz.databankcloud.com/GilbertAgendaOnline",
    )

def get_config():
    """Get or create Gilbert OnBaseConfig."""
    return _make_config()
