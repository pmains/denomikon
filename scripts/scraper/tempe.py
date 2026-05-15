"""City of Tempe City Council meeting and agenda extraction.

Uses the OnBase Agenda Online adapter with the TEMPE_CONFIG to search for
meetings, parse agendas, and persist results to the database.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from scraper.onbase import (
    OnBaseAgendaClient,
    TEMPE_CONFIG,
    search_meetings as onbase_search,
    fetch_agenda_html as onbase_fetch_agenda,
    parse_agenda_html,
    parse_meetings_from_html,
)

log = logging.getLogger(__name__)

# ── Client instance ──

CLIENT = OnBaseAgendaClient(TEMPE_CONFIG)

# Public body slug for this source
SOURCE_BODY = "tempe-cc"
JURISDICTION_ID = 2  # City of Tempe
PUBLIC_BODY_CODE = "tempe-cc"

# Mapping from OnBase meeting type IDs to our public body slugs
MEETING_TYPE_BODY_MAP = {
    109: "tempe-cc",    # Regular City Council
    101: "tempe-cc",    # Work Study Session
    106: "tempe-cc",    # Executive Session
    102: "tempe-cc",    # Special Meeting
    104: "tempe-drc",   # Development Review Commission Regular
    105: "tempe-drc",   # Development Review Commission Study
    110: "tempe-boa",   # Board of Adjustment Regular
    111: "tempe-boa",   # Board of Adjustment Study
    112: "tempe-hpc",   # Historic Preservation Commission
    107: "tempe-ha",    # Tempe Housing Authority
    108: "tempe-rio",   # Rio Salado Community Facilities District Board
    113: "tempe-rmt",   # Risk Management Trust Board
    114: "tempe-jrc",   # Joint Review Committee Regular
    115: "tempe-jrc",   # Joint Review Committee Study
}

# Mapping from meeting type ID to our body code
ONBASE_TYPE_TO_BODY = {
    109: "tempe-cc",
    101: "tempe-cc",
    106: "tempe-cc",
    102: "tempe-cc",
}


# Default set of OnBase meeting type IDs to search for all Tempe bodies
DEFAULT_TYPE_IDS = [109, 101, 106, 102, 104, 105, 110, 111, 112, 107, 108, 113, 114, 115]

# Shortcut names for commonly-used body groups
BODY_GROUPS = {
    "council": [109, 101, 106, 102],   # City Council: Regular, Work Study, Exec Session, Special
    "drc": [104, 105],                   # Development Review Commission: Regular, Study
    "boa": [110, 111],                   # Board of Adjustment: Regular, Study
    "hpc": [112],                        # Historic Preservation Commission
    "all": DEFAULT_TYPE_IDS,             # All Tempe bodies
}


def get_type_ids_for_meeting_type(meeting_type_ids: Optional[list[int]] = None,
                                    body_group: Optional[str] = None) -> list[int]:
    """Return meeting type IDs to search for.

    If *body_group* is given, uses the pre-defined group.  Otherwise uses
    *meeting_type_ids* if provided.  Defaults to all Tempe bodies.
    """
    if body_group and body_group in BODY_GROUPS:
        return BODY_GROUPS[body_group]
    if meeting_type_ids is not None:
        return meeting_type_ids
    return DEFAULT_TYPE_IDS


def resolve_body_code(meeting_type_ids: Optional[list[int]] = None) -> str:
    """Resolve the public body code for the requested meeting type IDs."""
    if meeting_type_ids is None:
        return PUBLIC_BODY_CODE
    # If all type IDs map to the same body, use that; otherwise default
    bodies = {MEETING_TYPE_BODY_MAP.get(mtid, PUBLIC_BODY_CODE) for mtid in meeting_type_ids}
    if len(bodies) == 1:
        return bodies.pop()
    return PUBLIC_BODY_CODE


def _strip_cancel_prefix(text: str) -> str:
    """Strip cancel/reschedule prefixes from a meeting title or type.

    Removes leading "CANCELED – ", "CANCELLED – ", "CANCEL – "
    and the trailing " - " residue that some OnBase meetings leave
    after the prefix is removed.
    """
    import re
    t = text.replace("\u2013", "-").strip()
    t = re.sub(r"^CANCEL(?:LED|ED)?\s*-\s*", "", t, count=1, flags=re.IGNORECASE)
    t = re.sub(r"^RESCHEDULED TO \d{1,2}/\d{1,2}/\d{4}\s*-\s*", "", t, count=1, flags=re.IGNORECASE)
    # Strip leading " - " residue (space-hyphen-space) left behind by some systems
    t = re.sub(r"^\s*-\s+", "", t)
    return t.strip()


async def search_tempe_meetings(page, start_date: str, end_date: str,
                                 meeting_type_ids: Optional[list[int]] = None,
                                 body_group: Optional[str] = None) -> list[dict]:
    """Search for City of Tempe meetings and return parsed results.

    Parameters
    ----------
    page : playwright Page (may be None; search uses urllib)
    start_date : str
        Start date in MM/DD/YYYY format.
    end_date : str
        End date in MM/DD/YYYY format.
    meeting_type_ids : list[int], optional
        OnBase meeting type IDs to search for.
    body_group : str, optional
        Shortcut name of a pre-defined body group ("council", "drc", "boa",
        "hpc", "all").  Overrides *meeting_type_ids* if given.

    Returns
    -------
    list[dict]
        Meeting dicts with keys: meeting_id, meeting_date, meeting_time,
        meeting_title, meeting_type, body, agenda_url, etc.
    """
    type_ids = get_type_ids_for_meeting_type(meeting_type_ids, body_group)
    body_code = resolve_body_code(meeting_type_ids)

    meetings = await onbase_search(
        page, TEMPE_CONFIG, start_date, end_date,
        meeting_type_ids=type_ids,
        public_body_code=body_code,
    )

    # Derive the per-meeting body code from the meeting title,
    # since different OnBase meeting type IDs map to different
    # public bodies (City Council, DRC, BOA, HPC, etc.)
    for m in meetings:
        title = m.get("meeting_title", "")
        raw_type = m.get("meeting_type", "")
        # Detect cancelation before normalization strips the prefix
        m["canceled"] = bool(re.search(r"CANCEL(?:LED|ED|LED)", title + " " + raw_type, re.IGNORECASE))
        m["meeting_type"] = normalize_meeting_type(raw_type)
        # Strip cancel/reschedule prefixes from the meeting title too,
        # but DON'T normalize it as a meeting type (that was a bug:
        # normalize_meeting_type on a title would mangle full names).
        m["meeting_title"] = _strip_cancel_prefix(title)
        title_body = extract_body_code_from_title(title)
        if title_body != "tempe-cc":
            m["body"] = title_body

    return meetings


async def extract_tempe_agenda_items(page, agenda_url: str) -> list[dict]:
    """Extract agenda items from a Tempe meeting's agenda page.

    Parameters
    ----------
    page : playwright Page
    agenda_url : str
        URL to the meeting agenda view (ViewMeetingAgenda or ViewMeeting).

    Returns
    -------
    list[dict]
        Agenda item dicts with keys: meeting_id, agenda_item_number,
        agenda_item_id, agenda_item_title, agenda_item_text, item_type, etc.
    """
    # Parse meeting ID from the URL
    import re
    mid_match = re.search(r'[?&]meetingId=(\d+)', agenda_url)
    if not mid_match:
        mid_match = re.search(r'[?&]id=(\d+)', agenda_url)
    if not mid_match:
        log.warning("Could not extract meeting ID from agenda URL: %s", agenda_url)
        return []
    meeting_id = mid_match.group(1)

    html = await onbase_fetch_agenda(page, TEMPE_CONFIG, int(meeting_id))
    items = parse_agenda_html(html, meeting_id, PUBLIC_BODY_CODE)

    # Attach source info
    for item in items:
        item["source_url"] = agenda_url
        item["body"] = PUBLIC_BODY_CODE

    # Propagate consent / non-consent / other category labels down from
    # level-1 section headings to all child items.
    _assign_tempe_categories(items)

    return items


def _assign_tempe_categories(items: list[dict]) -> None:
    """Walk items and set ``agenda_category`` on each based on the
    enclosing level-1 section (CONSENT AGENDA, NON-CONSENT AGENDA, etc.).
    """
    current_category = ""
    for item in items:
        level = item.get("section_level", 0) or 0
        title = (item.get("agenda_item_title") or "").strip().upper()

        if level == 1:
            # Level-1 sections define the category for all sub-items until
            # the next level-1 section.
            if title == "CONSENT AGENDA":
                current_category = "Consent"
            elif title == "NON-CONSENT AGENDA":
                current_category = "Non-Consent"
            elif title.startswith("CALL TO ORDER"):
                current_category = "Call to Order"
            elif title.startswith("PUBLIC APPEARANCES"):
                current_category = "Public Appearances"
            elif title.startswith("REPORTS AND ANNOUNCEMENTS"):
                current_category = "Reports & Announcements"
            elif "MEETING MINUTES" in title or title.startswith("MEETING MINUTES"):
                current_category = "Meeting Minutes"
            elif title.startswith("CURRENT EVENTS"):
                current_category = "Current Events"
            elif title.startswith("ADJOURNMENT"):
                current_category = "Adjournment"
            else:
                current_category = title  # use raw title

        item["agenda_category"] = current_category


def extract_meeting_type_from_title(title: str) -> str:
    """Derive a human-readable meeting type from the meeting title.

    Tempe meeting titles include the meeting type, e.g.
    'Regular City Council Meeting', 'City Council Executive Session'.
    """
    title_lower = title.lower()
    if "executive" in title_lower:
        return "Executive Session"
    if "work study" in title_lower or "study session" in title_lower:
        return "Work Study"
    if "special" in title_lower:
        return "Special Meeting"
    if "regular" in title_lower or "regular meeting" in title_lower:
        return "Regular Meeting"
    if "calendaring" in title_lower:
        return "Calendaring"
    if "budget" in title_lower:
        return "Budget Meeting"
    return "Regular Meeting"  # default


def extract_body_code_from_title(title: str) -> str:
    """Derive the public body code from the meeting title."""
    title_lower = title.lower()
    if "city council" in title_lower:
        return "tempe-cc"
    if "development review" in title_lower:
        return "tempe-drc"
    if "board of adjustment" in title_lower:
        return "tempe-boa"
    if "historic preservation" in title_lower:
        return "tempe-hpc"
    if "housing authority" in title_lower:
        return "tempe-ha"
    if "rio salado" in title_lower:
        return "tempe-rio"
    if "risk management" in title_lower:
        return "tempe-rmt"
    if "joint review" in title_lower:
        return "tempe-jrc"
    return "tempe-cc"


def normalize_tempe_meeting_title(raw_title: str) -> tuple[str, str]:
    """Clean up a meeting title and extract body code.

    Returns (cleaned_title, body_code).
    """
    title = raw_title.replace("–", "-").strip()
    title = title.replace("CANCELED – ", "", 1).replace("CANCELLED – ", "", 1)
    body_code = extract_body_code_from_title(title)
    return title, body_code

def normalize_meeting_type(raw_type: str) -> str:
    """Clean up a meeting type by stripping scheduling prefixes."""
    return _strip_cancel_prefix(raw_type)


def download_tempe_documents(meeting_id: str, meeting_date: str,
                              doc_dir: str = "data") -> dict[str, str]:
    """Download agenda PDF and packet PDF for a Tempe meeting.

    Returns dict with keys: agenda_pdf_path, packet_pdf_path (or empty strings on failure).
    """
    import os
    from scraper.onbase import download_document, TEMPE_CONFIG

    # Determine the document name from the meeting date
    # OnBase uses: Regular_City_Council_Meeting_{ID}_Agenda.pdf
    # We need to figure out the meeting name pattern from the ID
    # For now, construct from meeting_id
    name_base = f"Regular_City_Council_Meeting_{meeting_id}_Agenda"
    packet_name_base = f"{name_base}_Packet"

    # Ensure storage directory
    store_dir = os.path.join(doc_dir, "agendas", "tempe")
    os.makedirs(store_dir, exist_ok=True)

    results = {"agenda_pdf_path": "", "packet_pdf_path": ""}

    # Download agenda PDF
    agenda_path = os.path.join(store_dir, f"{meeting_id}_agenda.pdf")
    if not os.path.exists(agenda_path):
        try:
            log.info("Downloading agenda PDF for meeting %s", meeting_id)
            pdf_bytes = download_document(
                TEMPE_CONFIG, int(meeting_id), f"{name_base}.pdf", doc_type=1
            )
            with open(agenda_path, "wb") as f:
                f.write(pdf_bytes)
            log.info("Agenda PDF saved: %s (%d bytes)", agenda_path, len(pdf_bytes))
            results["agenda_pdf_path"] = agenda_path
        except Exception as e:
            log.warning("Failed to download agenda PDF for %s: %s", meeting_id, e)
    else:
        results["agenda_pdf_path"] = agenda_path

    # Download packet PDF
    packet_path = os.path.join(store_dir, f"{meeting_id}_packet.pdf")
    if not os.path.exists(packet_path):
        try:
            log.info("Downloading packet PDF for meeting %s", meeting_id)
            pdf_bytes = download_document(
                TEMPE_CONFIG, int(meeting_id),
                f"{packet_name_base}.pdf", doc_type=5,
            )
            with open(packet_path, "wb") as f:
                f.write(pdf_bytes)
            log.info("Packet PDF saved: %s (%d bytes)", packet_path, len(pdf_bytes))
            results["packet_pdf_path"] = packet_path
        except Exception as e:
            log.warning("Failed to download packet PDF for %s: %s", meeting_id, e)
    else:
        results["packet_pdf_path"] = packet_path

    return results
