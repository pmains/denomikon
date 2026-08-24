"""
City of Surprise, AZ meeting extraction via CivicClerk API.

Surprise uses the CivicClerk Public Portal platform at
``https://surpriseaz.portal.civicclerk.com/`` with a REST/OData API
at ``https://surpriseaz.api.civicclerk.com/v1``.

The API exposes:
- ``/Events`` — OData queryable list of events/meetings
- ``/Events/{id}`` — single event with publishedFiles (agenda/minutes PDFs)
- ``/EventCategories`` — committee/body lookup
- ``/Meetings/{id}`` — meeting detail with agenda items (legacy events only)
- ``/Meetings/GetMeetingFile(fileId={id},plainText=false)`` — file download (returns blobUri → Azure Storage)

Usage:
    ./scrape surprise --sync [--start-date=YYYY-MM-DD] [--end-date=YYYY-MM-DD]
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Optional

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text

log = logging.getLogger(__name__)

# ── Jurisdiction / body constants ──

SOURCE_SYSTEM = "civicclerk"
SOURCE_INSTANCE_URL = "https://surpriseaz.portal.civicclerk.com"
BASE_URL = "https://surpriseaz.portal.civicclerk.com"
API_BASE = "https://surpriseaz.api.civicclerk.com/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# ── Public body code mapping ──
# Maps CivicClerk EventCategory id → (slug, code) tuple.
#
# Many category IDs map to the same body slug (e.g. Regular Council Meeting,
# Council Work Session, Special Council Meeting all → "surprise-cc").
# The second tuple element is the short body code for use in tables.

CATEGORY_SLUG_MAP: dict[int, tuple[str, str]] = {
    # City Council meetings (multiple category types → same body)
    38: ("surprise-cc", "surprise-cc"),  # Regular City Council Meeting
    39: ("surprise-cc", "surprise-cc"),  # Regular City Council Work Session
    40: ("surprise-cc", "surprise-cc"),  # Special City Council Meeting
    41: ("surprise-cc", "surprise-cc"),  # Special City Council Work Session
    # Planning and Zoning
    34: ("surprise-pz", "surprise-pz"),      # P&Z Commission
    35: ("surprise-pz", "surprise-pz"),      # P&Z Commission Work Session
    # Boards and Commissions
    27: ("surprise-arts-culture", "surprise-arts"),       # Arts and Cultural Advisory Commission
    28: ("surprise-cfd", "surprise-cfd"),                 # CFD Commission
    29: ("surprise-parks-recreation", "surprise-parks"),  # Parks and Recreation Commission
    30: ("surprise-veterans-disability-hs", "surprise-vhs"), # Veterans, Disability and Human Services
    31: ("surprise-health-benefits", "surprise-hb"),      # Health Benefits Trust Fund Board
    32: ("surprise-judicial-selection", "surprise-jud"),  # Judicial Selection Advisory Commission
    33: ("surprise-personnel-appeals", "surprise-pa"),    # Personnel Appeals Commission
    36: ("surprise-psprs-fire", "surprise-psprs-f"),      # PSPRS – Fire
    37: ("surprise-psprs-police", "surprise-psprs-p"),    # PSPRS – Police
    42: ("surprise-special-meeting", "surprise-sm"),       # Special Meeting
    43: ("surprise-municipal-property-corp", "surprise-mpc"), # Municipal Property Corporation
    44: ("surprise-tourism", "surprise-tourism"),          # Tourism Advisory Commission
    45: ("surprise-youth-leadership", "surprise-youth"),   # Youth Leadership Commission
    46: ("surprise-boards-commissions-nominations", "surprise-nom"), # B&C Nominations
    47: ("surprise-construction-review", "surprise-bcr"),  # Board of Construction Review
    48: ("surprise-audit", "surprise-audit"),              # City Audit Committee
    49: ("surprise-exceptional-leader-task-force", "surprise-eltf"), # Exceptional Leader Task Force
    50: ("surprise-community-outreach", "surprise-outreach"), # Community Outreach Subcommittee
    51: ("surprise-tourism-fund", "surprise-tf-sub"),      # Tourism Fund Subcommittee
    52: ("surprise-fire-police-joint", "surprise-fp-joint"), # Fire and Police Boards Joint
    53: ("surprise-psprs-joint-board", "surprise-psprs-jb"), # PSPRS Joint Board
    54: ("surprise-psprs-police-disability", "surprise-psprs-pd"), # PSPRS Police Disability
    55: ("surprise-rules", "surprise-rules"),              # Rules Committee
    56: ("surprise-education", "surprise-ed"),             # Education Subcommittee
    57: ("surprise-redistricting", "surprise-redist"),     # Redistricting Advisory Committee
    58: ("surprise-recruitment", "surprise-recruit"),      # Recruitment Committee
    59: ("surprise-library", "surprise-library"),          # Library Commission
    60: ("surprise-import", "surprise-import"),            # Import (internal)
    61: ("surprise-public-safety-retirement-joint", "surprise-psr-joint"), # Public Safety Retirement Joint
    # General category (catch-all)
    24: ("surprise-general", "surprise-general"),          # General
}

# Default body slugs to sync when --bodies is not specified
DEFAULT_BODY_SLUGS = [
    "surprise-cc",
    "surprise-pz",
]

# File type mapping
FILE_TYPE_NAMES = {
    0: "agenda",
    1: "agenda_packet",
    2: "additional",
    3: "supplemental",
    4: "minutes",
    5: "video",
}


# ── Helpers ──

def _build_url(base: str, path: str, params: Optional[dict[str, str]] = None) -> str:
    """Build URL with properly encoded query parameters.

    CivicClerk's OData API uses ``$filter``, ``$orderby``, ``$top``, ``$skip``
    which must have their ``$`` signs URL-encoded.
    """
    if not params:
        return f"{base}{path}"
    encoded_parts = []
    for k, v in params.items():
        encoded_k = urllib.parse.quote(k, safe="")
        encoded_v = urllib.parse.quote(v, safe="")
        encoded_parts.append(f"{encoded_k}={encoded_v}")
    qs = "&".join(encoded_parts)
    return f"{base}{path}?{qs}"


def _fetch_json(url: str, timeout: int = 15) -> dict:
    """Fetch a URL as JSON with standard headers."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_raw(url: str, timeout: int = 15) -> bytes:
    """Fetch a URL and return raw bytes."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _resolve_body_from_category(category_id: int) -> tuple[str, str]:
    """Map a CivicClerk category ID to a (slug, code) tuple."""
    return CATEGORY_SLUG_MAP.get(category_id, ("surprise-general", "surprise-general"))


def _resolve_body_from_name(category_name: str) -> tuple[str, str]:
    """Map a CivicClerk category name to a (slug, code) tuple by matching against known names."""
    name_lower = category_name.lower().strip()
    # Direct name lookups
    mapping = {
        "regular city council meeting": ("surprise-cc", "surprise-cc"),
        "regular city council work session": ("surprise-cc", "surprise-cc"),
        "special city council meeting": ("surprise-cc", "surprise-cc"),
        "special city council work session": ("surprise-cc", "surprise-cc"),
        "planning and zoning commission": ("surprise-pz", "surprise-pz"),
        "planning and zoning commission work session": ("surprise-pz", "surprise-pz"),
        "arts and cultural advisory commission": ("surprise-arts-culture", "surprise-arts"),
        "cfd commission meeting": ("surprise-cfd", "surprise-cfd"),
        "parks and recreation commission": ("surprise-parks-recreation", "surprise-parks"),
        "veterans, disability and human services commission": ("surprise-veterans-disability-hs", "surprise-vhs"),
        "health benefits trust fund board": ("surprise-health-benefits", "surprise-hb"),
        "judicial selection advisory commission": ("surprise-judicial-selection", "surprise-jud"),
        "personnel appeals commission": ("surprise-personnel-appeals", "surprise-pa"),
        "public safety personnel retirement system commission – fire": ("surprise-psprs-fire", "surprise-psprs-f"),
        "public safety personnel retirement system commission – police": ("surprise-psprs-police", "surprise-psprs-p"),
        "special meeting": ("surprise-special-meeting", "surprise-sm"),
        "surprise municipal property corporation": ("surprise-municipal-property-corp", "surprise-mpc"),
        "tourism advisory commission": ("surprise-tourism", "surprise-tourism"),
        "youth leadership commission": ("surprise-youth-leadership", "surprise-youth"),
        "boards and commissions nominations committee": ("surprise-boards-commissions-nominations", "surprise-nom"),
        "board of construction review": ("surprise-construction-review", "surprise-bcr"),
        "city audit committee": ("surprise-audit", "surprise-audit"),
        "honoring an exceptional leader task force": ("surprise-exceptional-leader-task-force", "surprise-eltf"),
        "council subcommittee on community outreach, partnerships & grants meeting": ("surprise-community-outreach", "surprise-outreach"),
        "tourism fund subcommittee": ("surprise-tourism-fund", "surprise-tf-sub"),
        "local fire and police boards joint meeting": ("surprise-fire-police-joint", "surprise-fp-joint"),
        "psprs joint board position process subcommittee": ("surprise-psprs-joint-board", "surprise-psprs-jb"),
        "psprs police disability review subcommittee": ("surprise-psprs-police-disability", "surprise-psprs-pd"),
        "rules committee": ("surprise-rules", "surprise-rules"),
        "education subcommittee": ("surprise-education", "surprise-ed"),
        "redistricting advisory committee": ("surprise-redistricting", "surprise-redist"),
        "recruitment committee": ("surprise-recruitment", "surprise-recruit"),
        "library commission": ("surprise-library", "surprise-library"),
        "public safety retirement commission joint meeting": ("surprise-public-safety-retirement-joint", "surprise-psr-joint"),
        "general": ("surprise-general", "surprise-general"),
    }
    for key, value in mapping.items():
        if key in name_lower or name_lower in key:
            return value
    return ("surprise-general", "surprise-general")


def _format_date_for_api(d: str) -> str:
    """Convert YYYY-MM-DD to ISO 8601 OData format (YYYY-MM-DDT00:00:00Z)."""
    return f"{d.strip()}T00:00:00Z"


def _parse_date_from_event(event_date: str) -> str:
    """Parse eventDate string (ISO 8601) to YYYY-MM-DD."""
    if not event_date:
        return ""
    return event_date[:10]


def _parse_time_from_event(event_date: str) -> str:
    """Extract time portion from eventDate ISO string."""
    if not event_date or "T" not in event_date:
        return ""
    time_part = event_date.split("T")[1]  # e.g. "18:00:00Z"
    if time_part.endswith("Z"):
        time_part = time_part[:-1]
    return time_part


# ── Category / Body helpers ──

def fetch_categories() -> list[dict]:
    """Fetch all event categories (bodies/committees) from the CivicClerk API.

    Returns a list of category dicts with keys: id, categoryDesc, sortOrder.
    """
    url = f"{API_BASE}/EventCategories"
    data = _fetch_json(url, timeout=10)
    return data.get("value", [])


def build_body_slug_map() -> dict[str, int]:
    """Build a mapping of body slug → CivicClerk category ID.

    Returns dict like {"surprise-cc": 38, ...}.
    Note: some body slugs map to multiple category IDs. This returns
    the first match for each slug.
    """
    cats = fetch_categories()
    slug_map: dict[str, int] = {}
    for cat in cats:
        cat_id = cat.get("id")
        cat_name = cat.get("categoryDesc", "")
        slug, _ = _resolve_body_from_name(cat_name)
        if slug not in slug_map and cat_id:
            slug_map[slug] = cat_id
    # Also add entries from the hardcoded map for any missing
    for cat_id, (slug, _) in CATEGORY_SLUG_MAP.items():
        if slug not in slug_map:
            slug_map[slug] = cat_id
    return slug_map


def get_category_ids_for_body_slugs(body_slugs: list[str]) -> list[int]:
    """Resolve body slugs to CivicClerk category IDs for filtering.

    Handles the one-to-many mapping (e.g. "surprise-cc" maps
    to category IDs 38, 39, 40, 41).
    """
    ids: list[int] = []
    for slug in body_slugs:
        for cat_id, (s, _) in CATEGORY_SLUG_MAP.items():
            if s == slug:
                ids.append(cat_id)
    return ids


# ── Meeting search ──

def search_surprise_meetings(
    start_date: str = "",
    end_date: str = "",
    body_slugs: Optional[list[str]] = None,
    limit: int = 500,
) -> list[dict]:
    """Search for Surprise meetings via the CivicClerk Events OData API.

    Parameters
    ----------
    start_date : str, optional
        Start date in YYYY-MM-DD format.  If empty, defaults to 90 days ago.
    end_date : str, optional
        End date in YYYY-MM-DD format.  If empty, defaults to today.
    body_slugs : list of str, optional
        List of body slugs to filter by.  If empty or None, returns all.
    limit : int
        Maximum number of events to return (passed as $top).

    Returns
    -------
    list of dict
        Each dict represents one meeting/event.
    """
    import datetime

    # Default date range: last 90 days to today
    if not end_date:
        end_date = datetime.date.today().isoformat()
    if not start_date:
        start_date = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()

    # Build OData $filter
    filters: list[str] = []
    filters.append(f"isPublished eq 'Published'")

    # Date range filter
    date_start = _format_date_for_api(start_date)
    date_end = _format_date_for_api(end_date)
    filters.append(f"eventDate ge {date_start}")
    filters.append(f"eventDate le {date_end}")

    # Category (body) filter
    if body_slugs:
        cat_ids = get_category_ids_for_body_slugs(body_slugs)
        if len(cat_ids) == 1:
            filters.append(f"categoryId eq {cat_ids[0]}")
        elif len(cat_ids) > 1:
            # Build OR filter: categoryId eq X or categoryId eq Y ...
            or_parts = [f"categoryId eq {cid}" for cid in cat_ids]
            filters.append(f"({' or '.join(or_parts)})")

    filter_str = " and ".join(filters)

    params: dict[str, str] = {
        "$filter": filter_str,
        "$orderby": "eventDate desc",
        "$top": str(limit),
    }

    url = _build_url(API_BASE, "/Events", params)
    log.debug("Searching Surprise meetings: %s", url)

    try:
        data = _fetch_json(url, timeout=20)
    except urllib.error.HTTPError as e:
        log.warning("Surprise meeting search failed with HTTP %s", e.code)
        return []

    events = data.get("value", [])
    log.debug("Found %d Surprise events", len(events))
    # Parse events into standard meeting format
    meetings: list[dict] = []
    for ev in events:
        meeting = _parse_event_to_meeting(ev)
        if meeting:
            meetings.append(meeting)

    return meetings


def _parse_event_to_meeting(ev: dict) -> Optional[dict]:
    """Parse a CivicClerk event dict into our standard meeting format."""
    event_id = ev.get("id")
    if not event_id:
        return None

    event_name = ev.get("eventName", "")
    event_date = ev.get("eventDate", "")
    category_name = ev.get("categoryName") or ev.get("eventCategoryName") or ""
    category_id = ev.get("categoryId") or ev.get("eventCategoryId") or 24

    # Resolve body slug/code
    body_slug, body_code = _resolve_body_from_category(category_id)
    if body_code == "surprise-general" and category_name:
        # Try name-based resolution as fallback
        body_slug, body_code = _resolve_body_from_name(category_name)

    # Parse date/time
    date_str = _parse_date_from_event(event_date)
    time_str = _parse_time_from_event(event_date)

    # Get published files from the event detail
    published_files = ev.get("publishedFiles", [])
    # If not included in listing, we'll fetch detail later
    needs_detail = not published_files and ev.get("hasAgenda", False)

    # Build file URLs
    # The filtered listing returns relative blob paths, while the detail
    # endpoint returns full API URLs. We always prefer the full API URL.
    agenda_url = None
    minutes_url = None
    packet_url = None
    meeting_results_url = None
    for pf in published_files:
        file_type = pf.get("type", "").lower()
        file_url = pf.get("url", "")
        file_id = pf.get("fileId")

        # Ensure we have a proper API URL, not just a blob relative path
        if file_id and (not file_url or not file_url.startswith("http")):
            file_url = f"{API_BASE}/Meetings/GetMeetingFile(fileId={file_id},plainText=false)"

        if "agenda packet" in file_type or "packet" in file_type:
            packet_url = file_url
        elif "agenda" in file_type:
            agenda_url = file_url
        elif "minutes" in file_type:
            minutes_url = file_url
        elif "meeting results" in file_type:
            meeting_results_url = file_url

    # Determine meeting type from category name
    meeting_type = ""
    if category_name:
        meeting_type = category_name

    meeting = {
        "meeting_id": str(event_id),
        "meeting_date": date_str,
        "meeting_time": time_str,
        "meeting_title": event_name,
        "meeting_type": meeting_type,
        "body": body_code,
        "body_slug": body_slug,
        "body_name": category_name or event_name,
        # jurisdiction_id resolved from public_bodies table in create_or_get_meeting
        "source_system": SOURCE_SYSTEM,
        "source_instance_url": SOURCE_INSTANCE_URL,
        "agenda_url": agenda_url or "",
        "minutes_url": minutes_url or "",
        "agenda_packet_url": packet_url or "",
        "needs_detail": needs_detail,
        "_raw_event_id": event_id,
        "_raw_published_files": published_files,
        "agenda_id": ev.get("agendaId"),
    }
    return meeting


# ── Event detail ──

def fetch_event_detail(event_id: int) -> dict:
    """Fetch full event details including publishedFiles from the CivicClerk API.

    Parameters
    ----------
    event_id : int
        CivicClerk event ID.

    Returns
    -------
    dict
        Full event data including publishedFiles.
    """
    url = f"{API_BASE}/Events/{event_id}"
    try:
        return _fetch_json(url, timeout=15)
    except urllib.error.HTTPError as e:
        log.warning("Failed to fetch event %s detail: HTTP %s", event_id, e.code)
        return {}
    except Exception as e:
        log.warning("Failed to fetch event %s detail: %s", event_id, e)
        return {}


# ── Meeting detail / agenda items ──

def fetch_meeting_detail(event_id: int) -> dict:
    """Fetch meeting detail including agenda items from CivicClerk.

    NOTE: This endpoint only returns items for legacy events (pre-2020).
    Newer events store agenda items differently and this endpoint returns
    only metadata.

    Parameters
    ----------
    event_id : int
        CivicClerk event ID (same as event ID in /Events).

    Returns
    -------
    dict
        Meeting data including ``items`` and ``publishedFiles`` arrays.
    """
    url = f"{API_BASE}/Meetings/{event_id}"
    try:
        return _fetch_json(url, timeout=15)
    except urllib.error.HTTPError as e:
        log.debug("Meeting detail %s: HTTP %s", event_id, e.code)
        return {}
    except Exception as e:
        log.debug("Meeting detail %s: %s", event_id, e)
        return {}


def parse_agenda_items(meeting_data: dict, event_id: str) -> list[dict]:
    """Parse agenda items from a meeting detail response.

    Parameters
    ----------
    meeting_data : dict
        Response from ``fetch_meeting_detail()``.
    event_id : str
        The event/meeting ID for cross-reference.

    Returns
    -------
    list of dict
        List of agenda item dicts with standardized fields.
    """
    items: list[dict] = []
    raw_items = meeting_data.get("items", [])
    if not raw_items:
        return []

    sort_order = 0
    for item in raw_items:
        if not item or item.get("isDeleted", False):
            continue

        sort_order += 1
        is_section = item.get("isSection", 0) == 1
        outline = item.get("agendaObjectItemOutlineNumber", "") or ""
        name = item.get("agendaObjectItemName", "")
        description = item.get("agendaObjectItemDescription", "") or ""
        html_content = item.get("agendaObjectItemHtmlContent", "") or ""
        parent_id = item.get("parentId", -1)

        # Determine type
        item_type = "section" if is_section else "item"

        # Clean up outline number
        outline = outline.strip()
        if outline.endswith("."):
            outline = outline[:-1]

        agenda_item = {
            "meeting_id": event_id,
            "agenda_item_number": outline,
            "agenda_item_title": name or "",
            "agenda_item_text": description or html_content or "",
            "item_type": item_type,
            "agenda_category": "",
            "sort_order": sort_order,
            "parent_id": parent_id if parent_id > 0 else None,
            "has_motion": item.get("hasMotion", False),
            "has_vote": item.get("hasVote", False),
            "motion_text": "",
            "vote_data": None,
            "attachments": item.get("attachmentsList", []),
            "reports": item.get("reportsList", []),
        }
        items.append(agenda_item)

        # Check for child items
        child_items_list = item.get("childItems", [])
        if child_items_list:
            for child in child_items_list:
                sort_order += 1
                child_outline = child.get("agendaObjectItemOutlineNumber", "") or ""
                child_outline = child_outline.strip().rstrip(".")
                child_name = child.get("agendaObjectItemName", "")
                child_desc = child.get("agendaObjectItemDescription", "") or ""
                child_html = child.get("agendaObjectItemHtmlContent", "") or ""

                agenda_item = {
                    "meeting_id": event_id,
                    "agenda_item_number": f"{outline}.{child_outline}" if outline else child_outline,
                    "agenda_item_title": child_name or "",
                    "agenda_item_text": child_desc or child_html or "",
                    "item_type": "item",
                    "agenda_category": "",
                    "sort_order": sort_order,
                    "parent_id": item.get("id"),
                    "has_motion": child.get("hasMotion", False),
                    "has_vote": child.get("hasVote", False),
                    "motion_text": "",
                    "vote_data": None,
                    "attachments": child.get("attachmentsList", []),
                    "reports": child.get("reportsList", []),
                }
                items.append(agenda_item)

    return items


# ── File download ──

def download_meeting_file(file_id: int, timeout: int = 30) -> Optional[bytes]:
    """Download a meeting file (agenda PDF, minutes, etc.) from CivicClerk.

    The API returns a JSON with a ``blobUri`` pointing to Azure blob storage.
    This function resolves the blob URI and returns the raw file bytes.

    Parameters
    ----------
    file_id : int
        CivicClerk file ID from the event's publishedFiles array.
    timeout : int
        Download timeout in seconds.

    Returns
    -------
    bytes or None
        Raw file bytes, or None on failure.
    """
    url = f"{API_BASE}/Meetings/GetMeetingFile(fileId={file_id},plainText=false)"
    try:
        data = _fetch_json(url, timeout=timeout)
    except Exception as e:
        log.warning("Failed to get file metadata for fileId=%s: %s", file_id, e)
        return None

    blob_uri = data.get("blobUri")
    if not blob_uri:
        log.warning("No blobUri in GetMeetingFile response for fileId=%s", file_id)
        return None

    try:
        return _fetch_raw(blob_uri, timeout=timeout)
    except Exception as e:
        log.warning("Failed to download blob for fileId=%s: %s", file_id, e)
        # Try alternate format
        alt_blob = data.get("alternateBlobUri") or data.get("downloadUri")
        if alt_blob:
            try:
                return _fetch_raw(alt_blob, timeout=timeout)
            except Exception as e2:
                log.warning("Also failed on alternate URI: %s", e2)
        return None


# ── Direct file URL resolution ──

def resolve_file_download_url(file_id: int) -> Optional[str]:
    """Resolve a CivicClerk file ID to a direct Azure blob download URL.

    The API returns a signed SAS URL to Azure blob storage.

    Parameters
    ----------
    file_id : int
        File ID from the event's publishedFiles array.

    Returns
    -------
    str or None
        Direct download URL, or None on failure.
    """
    url = f"{API_BASE}/Meetings/GetMeetingFile(fileId={file_id},plainText=false)"
    try:
        data = _fetch_json(url, timeout=15)
        return data.get("blobUri")
    except Exception as e:
        log.warning("Failed to resolve file URL for fileId=%s: %s", file_id, e)
        return None


# ── Main discovery function ──

def discover_meetings(
    start_date: str = "",
    end_date: str = "",
    body_slugs: Optional[list[str]] = None,
    fetch_detail: bool = False,
) -> list[dict]:
    """Discover Surprise meetings matching the given criteria.

    Parameters
    ----------
    start_date : str, optional
        Start date in YYYY-MM-DD format.
    end_date : str, optional
        End date in YYYY-MM-DD format.
    body_slugs : list of str, optional
        Body slugs to filter by (e.g. ["surprise-cc"]).
        Defaults to ``DEFAULT_BODY_SLUGS``.
    fetch_detail : bool
        Whether to fetch full event detail for each meeting to get
        published files and agenda items.  When False, only listing
        data is returned (which may include publishedFiles if the
        listing endpoint includes them).

    Returns
    -------
    list of dict
        Meeting dicts as returned by ``_parse_event_to_meeting()``.
    """
    if body_slugs is None:
        body_slugs = DEFAULT_BODY_SLUGS

    meetings = search_surprise_meetings(
        start_date=start_date,
        end_date=end_date,
        body_slugs=body_slugs,
    )

    if fetch_detail:
        for meeting in meetings:
            event_id = meeting.get("_raw_event_id")
            if not event_id:
                continue
            detail = fetch_event_detail(int(event_id))
            if detail:
                meeting["_raw_published_files"] = detail.get("publishedFiles", [])
                # Re-process files
                for pf in detail.get("publishedFiles", []):
                    file_type = pf.get("type", "").lower()
                    file_url = pf.get("url", "")
                    if "agenda packet" in file_type or "packet" in file_type:
                        meeting["agenda_packet_url"] = file_url
                    elif "agenda" in file_type:
                        meeting["agenda_url"] = file_url
                    elif "minutes" in file_type:
                        meeting["minutes_url"] = file_url

            # Try to get agenda items from meeting detail
            m_detail = fetch_meeting_detail(int(event_id))
            if m_detail and m_detail.get("items"):
                meeting["agenda_items"] = parse_agenda_items(m_detail, str(event_id))

    return meetings


# ── Agenda items extraction from event detail ──

def extract_agenda_items_from_event(event_id: int) -> list[dict]:
    """Extract agenda items for a given event.

    For legacy events, agenda items are available via the /Meetings endpoint.
    For newer events, this returns an empty list (PDF extraction needed).

    Parameters
    ----------
    event_id : int
        CivicClerk event ID.

    Returns
    -------
    list of dict
        Agenda items as parsed by ``parse_agenda_items()``.
    """
    meeting_data = fetch_meeting_detail(event_id)
    if meeting_data and meeting_data.get("items"):
        return parse_agenda_items(meeting_data, str(event_id))
    return []


# ── PDF text extraction ──

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pypdf's PdfReader.

    Parameters
    ----------
    pdf_bytes : bytes
        Raw PDF file bytes.

    Returns
    -------
    str
        Concatenated text from all pages, joined by newlines.
    """
    from io import BytesIO
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    all_text: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            all_text.append(page_text)
    return "\n".join(all_text)


def parse_agenda_items_from_pdf_text(text: str) -> list[dict]:
    """Parse Surprise agenda PDF text into structured agenda items.

    Surprise agendas have a consistent format with:
    - Letter-based top-level sections: "A. Call To Order", "B. Roll Call", etc.
    - Section headers like "CONSENT AGENDA:", "REGULAR AGENDA ITEM - PUBLIC HEARING:"
    - Numbered items (1., 2., ...) under each section

    The parser scans the text for:
    - Section headers (capitalised keywords, with or without trailing colon)
    - Letter-based sections (single capital letter followed by period and title)
    - Numbered items (e.g. ``1.``, ``12.``)
    - Lettered sub-items (e.g. ``a)``, ``b.`` — only after a numbered parent)
    - Continuation lines that belong to the previous item

    Parameters
    ----------
    text : str
        Extracted PDF text (raw output from ``extract_pdf_text()``).

    Returns
    -------
    list of dict
        Each dict has keys: ``agenda_item_number``, ``agenda_item_title``,
        ``agenda_item_text``, ``item_type`` ("section" | "item"),
        ``agenda_category``, ``sort_order``.
    """
    import re

    items: list[dict] = []
    current_category = ""
    sort_order = 0
    seen_numbered_item = False  # track so lettered sub-items only match after numbers
    _pending_letter = None  # "A." on its own line, title on next line

    # Filter patterns for non-agenda content (defined before pre-clean)
    _legal_citation = re.compile(r'^[\s\(]*\d+\.\s+(Discussion|Consideration|Executive|Receive|Consult)[\s,]|A\.R\.S\.|§', re.IGNORECASE)
    _attachment_label = re.compile(r'^0\d\s+[A-Z]{2}\d+[-]')
    _minutes_text = re.compile(r'^[A-Z]\s*[-–]\s*(?:In attendance|Commissioner|Chair|Vice Chair|Councilmember|Mayor|Present were|Absent)', re.IGNORECASE)

    # Pre-clean: normalise whitespace and strip blank/trivial lines
    lines_raw = text.split("\n")
    lines: list[str] = []
    for line in lines_raw:
        s = line.strip()
        if not s:
            continue
        # Skip page-only numbers and common PDF artefacts
        if re.match(r'^\d+$', s):
            continue
        if re.match(r'^Page \d+', s, re.IGNORECASE):
            continue
        if re.match(r'^City of Surprise,? (Arizona|AZ)', s, re.IGNORECASE):
            continue
        if s.lower().startswith("city of surprise"):
            continue
        if s.lower().startswith("printed on") or s.lower().startswith("printed:"):
            continue
        # Filter legal boilerplate (A.R.S. citations, executive session procedures)
        if _legal_citation.search(s):
            continue
        # Filter attachment labels like "01 FS25-0733 Zaxby's..."
        if _attachment_label.match(s):
            continue
        # Filter minutes-inspired text ("Commissioner X", "Chair Y", etc.)
        if _minutes_text.match(s):
            continue
        lines.append(s)

    # ── Pattern definitions ──

    # Section header markers (with optional trailing colon, hyphen-delimited variants)
    section_re = re.compile(
        r'^(CONSENT\s+AGENDA|REGULAR\s+AGENDA'
        r'|REGULAR\s+AGENDA\s+ITEM\s*[-–]\s*(?:PUBLIC\s+HEARING|NON[-–]PUBLIC\s+HEARING)'
        r'|PUBLIC\s+HEARINGS?'
        r'|NEW\s+BUSINESS|UNFINISHED\s+BUSINESS|DISCUSSION\s+ITEMS?'
        r'|COUNCIL\s+(?:MEMBER\s+)?COMMENTS'
        r'|MAYOR(?:' + "'" + r')?S?\s+REPORT'
        r'|CITIZEN(?:\s+PARTICIPATION|\s+COMMENTS?|\s+INPUT)'
        r'|CALL\s+TO\s+ORDER|ROLL\s+CALL|PLEDGE\s+OF\s+ALLEGIANCE'
        r'|PRESENTATIONS?|PROCLAMATIONS?|ADJOURNMENT'
        r'|EXECUTIVE\s+SESSION|WORK\s+SESSION|STUDY\s+SESSION'
        r'|COUNCIL\s+REPORTS'
        r'|MAYOR(?:\s+AND)?\s+COUNCIL\s+REPORTS)'
        r'[\s:]*$', re.IGNORECASE)

    # Capitalised multi-word section headers ending in AGENDA/HEARINGS/etc
    big_section_re = re.compile(
        r'^[A-Z][A-Z\s\-]+(?:AGENDA|HEARINGS|ITEMS|SESSION|COMMISSION'
        r'|BOARD|AUTHORITY|COMMITTEE)'
        r'\s*:?\s*$')

    # Numbered item: "1.", "12." — limit to ≤200 to avoid addresses
    numbered_re = re.compile(r'^(\d+)\.?\s*(.*)')

    # Lettered section header: "A. Call To Order", "B. Roll Call"
    # Matches single capital letter followed by period and text
    letter_section_re = re.compile(r'^([A-Z])\.\s+(.*)')
    # Single letter on its own line: "A." — title on next line
    solo_letter_re = re.compile(r'^([A-Z])\.\s*$')

    # Lettered sub-item: "a)", "b.", "(c)", "a" — only matched after numbered items
    lettered_re = re.compile(r'^[\s\(]*([a-zA-Z])\)?\.?[\s\):]+(.*)')

    i = 0
    while i < len(lines):
        s = lines[i]

        # ── Check letter-section patterns before section keywords ──
        # ("B. Roll Call" should be letter item, not "Roll Call" section)
        letter_sec_match = None
        if not _pending_letter:
            letter_sec_match = letter_section_re.match(s)
        
        if letter_sec_match:
            letter = letter_sec_match.group(1)
            title = letter_sec_match.group(2).strip()
        elif _pending_letter:
            pass  # handled in letter block below
        elif section_re.match(s) or big_section_re.match(s):
            current_category = s.upper()
            sort_order += 1
            items.append({
                "agenda_item_number": "",
                "agenda_item_title": s,
                "agenda_item_text": s,
                "item_type": "section",
                "agenda_category": current_category,
                "sort_order": sort_order,
            })
            i += 1
            # Skip content under Executive Session — all boilerplate legal text
            if "EXECUTIVE SESSION" in current_category:
                # Skip lines until the next section header (not lettered sub-items)
                while i < len(lines):
                    if section_re.match(lines[i]) or big_section_re.match(lines[i]):
                        break
                    i += 1
                continue
            # Stop parsing after adjournment — everything after is staff reports / attachments
            if "ADJOURN" in current_category:
                break
            continue

        # ── Single letter on its own line ("A." — title on next line) ──
        solo_match = solo_letter_re.match(s)
        if solo_match:
            _pending_letter = solo_match.group(1)
            i += 1
            continue

        # ── Letter-based section (A. Call To Order, B. Roll Call) ──
        letter = None
        title = None
        if _pending_letter:
            letter = _pending_letter
            title = s
            _pending_letter = None
        elif letter_sec_match is not None:
            letter = letter_sec_match.group(1)
            title = letter_sec_match.group(2).strip()

        if letter is not None:
            # Skip content under Executive Session — all boilerplate legal text
            if "EXECUTIVE SESSION" in (title or "").upper():
                sort_order += 1
                items.append({
                    "agenda_item_number": letter,
                    "agenda_item_title": title,
                    "agenda_item_text": title,
                    "item_type": "item",
                    "agenda_category": "EXECUTIVE SESSION",
                    "sort_order": sort_order,
                })
                # Consume all lines until next section header (skip lettered items — they're legal boilerplate)
                j = i + 1
                while j < len(lines):
                    if section_re.match(lines[j]) or big_section_re.match(lines[j]):
                        break
                    j += 1
                i = j
                continue

            # Peek ahead for continuation lines
            body_lines: list[str] = [f"{letter}. {title}"]
            j = i + 1
            while j < len(lines):
                next_s = lines[j]
                # Stop at next section header, letter section, solo letter, numbered item, or sub-item
                if section_re.match(next_s) or big_section_re.match(next_s):
                    break
                if letter_section_re.match(next_s) or solo_letter_re.match(next_s):
                    break
                next_num = numbered_re.match(next_s)
                if next_num:
                    num_val = int(next_num.group(1))
                    if num_val <= 200:
                        break
                next_letter = lettered_re.match(next_s)
                if next_letter and seen_numbered_item:
                    l = next_letter.group(1)
                    if l.islower() or len(lines[j].strip()) < 50:
                        break
                body_lines.append(next_s)
                j += 1

            sort_order += 1
            items.append({
                "agenda_item_number": letter,
                "agenda_item_title": title,
                "agenda_item_text": "\n".join(body_lines),
                "item_type": "item",
                "agenda_category": current_category,
                "sort_order": sort_order,
            })
            i = j
            continue

        # ── Numbered item ──
        num_match = numbered_re.match(s)
        if num_match:
            num_str = num_match.group(1)
            num_val = int(num_str)
            title_line = num_match.group(2).strip()

            # Reject numbers > 200 (likely address numbers or page counts)
            if num_val > 200:
                i += 1
                continue

            seen_numbered_item = True

            # Collect continuation lines for the title
            body_lines: list[str] = []
            if title_line:
                body_lines.append(title_line)

            # Peek ahead for continuation lines that are NOT new items
            j = i + 1
            while j < len(lines):
                next_s = lines[j]
                next_num = numbered_re.match(next_s)
                if next_num:
                    nv = int(next_num.group(1))
                    if nv <= 200:
                        break
                next_letter = lettered_re.match(next_s)
                if next_letter and seen_numbered_item:
                    l = next_letter.group(1)
                    if l.islower():
                        break
                if section_re.match(next_s) or big_section_re.match(next_s):
                    break
                if letter_section_re.match(next_s):
                    break
                body_lines.append(next_s)
                j += 1

            full_text = " ".join(body_lines) if body_lines else ""
            sort_order += 1
            items.append({
                "agenda_item_number": num_str,
                "agenda_item_title": body_lines[0] if body_lines else full_text,
                "agenda_item_text": "\n".join(body_lines) if body_lines else full_text,
                "item_type": "item",
                "agenda_category": current_category,
                "sort_order": sort_order,
            })
            i = j  # skip past consumed lines
            continue

        # ── Lettered sub-item (a, b, c, etc. — only after numbered items) ──
        if seen_numbered_item:
            letter_match = lettered_re.match(s)
            if letter_match and items:
                letter = letter_match.group(1)
                letter_title = letter_match.group(2).strip()
                # Only lowercase letters qualify as sub-items
                if letter.islower() and len(letter) == 1 and letter_title:
                    # Determine parent number from last numbered item
                    parent_num = ""
                    for p in reversed(items):
                        pn = p.get("agenda_item_number", "")
                        if pn and "." not in pn and pn.isdigit():
                            parent_num = pn
                            break

                    combined_num = f"{parent_num}.{letter}" if parent_num else letter
                    full_title = f"({letter}) {letter_title}"

                    # Peek ahead for continuation lines
                    sub_body: list[str] = [f"{letter}) {letter_title}"]
                    j = i + 1
                    while j < len(lines):
                        next_s = lines[j]
                        next_num = numbered_re.match(next_s)
                        if next_num and int(next_num.group(1)) <= 200:
                            break
                        next_letter = lettered_re.match(next_s)
                        if next_letter:
                            nl = next_letter.group(1)
                            if nl.islower():
                                break
                        if section_re.match(next_s) or big_section_re.match(next_s):
                            break
                        if letter_section_re.match(next_s):
                            break
                        sub_body.append(next_s)
                        j += 1

                    sort_order += 1
                    items.append({
                        "agenda_item_number": combined_num,
                        "agenda_item_title": full_title,
                        "agenda_item_text": "\n".join(sub_body),
                        "item_type": "item",
                        "agenda_category": current_category,
                        "sort_order": sort_order,
                    })
                    i = j
                    continue

        # ── Continuation line — append to last item's text ──
        if items and items[-1]["item_type"] == "item":
            last = items[-1]
            last["agenda_item_text"] = last["agenda_item_text"] + "\n" + s
            if not last["agenda_item_title"]:
                last["agenda_item_title"] = s

        i += 1

    return items


def fetch_and_parse_agenda_pdf(agenda_url: str) -> list[dict]:
    """Download a Surprise agenda PDF and parse it into structured agenda items.

    Handles two types of URLs:
    - API download URLs (containing ``fileId=``) → uses ``download_meeting_file()``
    - Direct Azure blob URLs (signed SAS) → fetches directly with ``_fetch_raw()``
    - Any other HTTP(S) URL → fetches directly

    Parameters
    ----------
    agenda_url : str
        URL to the agenda PDF (CivicClerk API or Azure blob URL).

    Returns
    -------
    list of dict
        Parsed agenda items, or empty list on failure.
    """
    if not agenda_url:
        log.warning("fetch_and_parse_agenda_pdf: empty agenda_url")
        return []

    pdf_bytes: Optional[bytes] = None

    # Detect API URL with fileId in path-based params
    # CivicClerk uses path params like /Meetings/GetMeetingFile(fileId=8329,plainText=false)
    import re
    file_id_match = re.search(r'fileId=(\d+)', agenda_url)
    if file_id_match:
        # Use existing file downloader which handles the blob URI resolution
        file_id = int(file_id_match.group(1))
        pdf_bytes = download_meeting_file(file_id, timeout=30)

    if pdf_bytes is None:
        # Try direct fetch (either Azure blob URL or any other URL)
        try:
            pdf_bytes = _fetch_raw(agenda_url, timeout=30)
        except Exception as e:
            log.warning("fetch_and_parse_agenda_pdf: failed to fetch %s: %s", agenda_url, e)
            return []

    if not pdf_bytes:
        log.warning("fetch_and_parse_agenda_pdf: no data from %s", agenda_url)
        return []

    # Verify it's a PDF
    if not pdf_bytes.startswith(b"%PDF"):
        log.warning("fetch_and_parse_agenda_pdf: %s is not a PDF (starts with %r)", agenda_url, pdf_bytes[:10])
        return []

    # Extract text and parse
    text = extract_pdf_text(pdf_bytes)
    if not text.strip():
        log.warning("fetch_and_parse_agenda_pdf: no text extracted from %s", agenda_url)
        return []

    items = parse_agenda_items_from_pdf_text(text)
    log.info("Parsed %d agenda items from PDF (%d bytes): %s", len(items), len(pdf_bytes), agenda_url)
    return items


# ── Test / Main ──

def main():
    """Test the Surprise scraper by fetching recent meetings."""
    import datetime

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    log.info("=" * 60)
    log.info("Surprise CivicClerk Scraper Test")
    log.info("=" * 60)

    # 1. Fetch categories
    log.info("\n1. Fetching categories...")
    cats = fetch_categories()
    log.info("   Found %d categories", len(cats))
    for cat in cats[:10]:
        log.info("     [%3d] %s", cat.get("id"), cat.get("categoryDesc"))

    # 2. Build body slug map
    log.info("\n2. Building body slug map...")
    slug_map = build_body_slug_map()
    for slug, cat_id in sorted(slug_map.items())[:10]:
        log.info("     %s → category %d", slug, cat_id)

    # 3. Discover recent City Council meetings
    end = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

    log.info("\n3. Searching for City Council meetings (%s to %s)...", start, end)
    meetings = search_surprise_meetings(
        start_date=start,
        end_date=end,
        body_slugs=["surprise-cc"],
    )
    log.info("   Found %d meetings", len(meetings))
    for m in meetings[:5]:
        log.info(
            "     [%s] %s - %s (%s)",
            m["meeting_date"],
            m["meeting_title"],
            m["meeting_type"],
            m["body"],
        )
        if m.get("agenda_url"):
            log.info("       Agenda: %s", m["agenda_url"])
        if m.get("minutes_url"):
            log.info("       Minutes: %s", m["minutes_url"])
        if m.get("agenda_packet_url"):
            log.info("       Packet: %s", m["agenda_packet_url"])

    # 4. Try fetching detail for a meeting (first one)
    if meetings:
        log.info("\n4. Fetching detail for first meeting...")
        detail = fetch_event_detail(int(meetings[0]["meeting_id"]))
        log.info("   Event detail keys: %s", list(detail.keys()) if detail else "empty")
        pf = detail.get("publishedFiles", [])
        log.info("   Published files: %d", len(pf))
        for f in pf:
            log.info("     [%s] %s (fileId=%s)", f.get("type"), f.get("name"), f.get("fileId"))
        if pf:
            log.info("\n5. Downloading first file to verify...")
            blob = download_meeting_file(pf[0]["fileId"])
            if blob:
                log.info("   Downloaded %d bytes (%s)", len(blob), "PDF" if blob.startswith(b"%PDF") else "other")

        # Try getting agenda items from API
        log.info("\n6. Checking for agenda items via API...")
        items = extract_agenda_items_from_event(int(meetings[0]["meeting_id"]))
        log.info("   Found %d agenda items", len(items))
        for item in items[:5]:
            log.info("     [%s] %s", item["agenda_item_number"], item["agenda_item_title"][:80])

        # Try PDF extraction from the first meeting that has an agenda_url
        log.info("\n7. Testing PDF agenda extraction...")
        pdf_tested = False
        for m in meetings[:5]:
            agenda_url = m.get("agenda_url") or m.get("agenda_packet_url")
            if agenda_url:
                log.info("   Attempting PDF parse from meeting %s (%s)...", m["meeting_date"], agenda_url[:100])
                pdf_items = fetch_and_parse_agenda_pdf(agenda_url)
                log.info("   PDF extracted %d agenda items", len(pdf_items))
                for item in pdf_items[:10]:
                    num = item["agenda_item_number"]
                    title = item["agenda_item_title"][:80]
                    itype = item["item_type"]
                    log.info("     [%s] %s (%s)", num, title, itype)
                pdf_tested = True
                break
        if not pdf_tested:
            log.info("   No meetings with agenda_url found to test PDF extraction")

    # 5. List all available body slugs
    log.info("\n8. All body slug → category ID mappings:")
    for cat_id in sorted(CATEGORY_SLUG_MAP.keys()):
        slug, code = CATEGORY_SLUG_MAP[cat_id]
        log.info("     [%3d] %s (code=%s)", cat_id, slug, code)

    log.info("\n" + "=" * 60)
    log.info("Test complete")


if __name__ == "__main__":
    main()
