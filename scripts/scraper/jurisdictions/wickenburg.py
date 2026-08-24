"""
Town of Wickenburg agenda extraction via Destiny (AgendaQuick).

Uses ``scraper.destiny_common`` (HTML-parser-based) for all parsing.
"""

from __future__ import annotations
import logging
from typing import Optional

from scraper.platforms.destiny_common import (
    BASE_URL,
    build_month_url,
    extract_meeting_type,
    fetch_page,
    fetch_agenda_memo_docs,
    parse_agenda_items as _parse_agenda_items,
    parse_meetings as _parse_meetings,
)

log = logging.getLogger(__name__)

# ── Constants ──

ORG_ID = "94253"
DEFAULT_BODY_SLUGS = [
    "wickenburg-common-council",
    "wickenburg-planning-zoning",
    "wickenburg-airport",
    "wickenburg-community-programming",
    "wickenburg-economic-development",
    "wickenburg-finance",
    "wickenburg-parks-recreation",
    "wickenburg-parks-trails",
]

SOURCE_INSTANCE_URL = BASE_URL
SOURCE_SYSTEM = "agendaquick"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ── Body map ──

BODY_MAP: dict[str, tuple[str, str]] = {
    "common council regular meeting": ("wickenburg-common-council", "wickenburg-cc"),
    "common council special meeting": ("wickenburg-common-council", "wickenburg-cc"),
    "common council study session": ("wickenburg-common-council", "wickenburg-cc"),
    "planning and zoning advisory commission": ("wickenburg-planning-zoning", "wickenburg-pz"),
    "planning & zoning advisory commission": ("wickenburg-planning-zoning", "wickenburg-pz"),
    "airport advisory commission": ("wickenburg-airport", "wickenburg-airport"),
    "community programming advisory committee": ("wickenburg-community-programming", "wickenburg-cpac"),
    "economic development and transportation advisory": ("wickenburg-economic-development", "wickenburg-edt"),
    "econ. dvlp. and transportation": ("wickenburg-economic-development", "wickenburg-edt"),
    "finance advisory commission": ("wickenburg-finance", "wickenburg-finance"),
    "parks and recreation advisory commission": ("wickenburg-parks-recreation", "wickenburg-parks"),
    "parks & recreation advisory commission": ("wickenburg-parks-recreation", "wickenburg-parks"),
    "parks and trails advisory committee": ("wickenburg-parks-trails", "wickenburg-trails"),
    "parks & trails advisory committee": ("wickenburg-parks-trails", "wickenburg-trails"),
    "meeting": ("wickenburg-common-council", "wickenburg-cc"),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    import re as _re
    key = _re.sub(r'\s+', ' ', body_name).lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "wickenburg-common-council", "wickenburg-cc"


def meeting_id_from_url(url: str) -> str:
    """Extract meeting seq from a Destiny agenda URL."""
    import re
    m = re.search(r"seq=(\d+)", url)
    return m.group(1) if m else ""


# ── Meeting search ──


def search_wickenburg_meetings(
    year: int,
    body_slugs: Optional[list[str]] = None,
    start_month: int = 1,
    end_month: int = 12,
) -> list[dict]:
    """Search Wickenburg meetings for a given year, month by month."""
    all_m: list[dict] = []
    start_month = max(1, min(12, start_month))
    end_month = max(start_month, min(12, end_month))
    for m in range(start_month, end_month + 1):
        try:
            url = build_month_url(ORG_ID, year, m)
            html = fetch_page(url, timeout=15)
            month_meetings = _parse_meetings(html, BODY_MAP)
            all_m.extend(month_meetings)
        except Exception as e:
            log.warning("Wickenburg %d-%02d failed: %s", year, m, e)
    if body_slugs:
        return [m for m in all_m if m["body_slug"] in body_slugs]
    return all_m


# ── Agenda item parsing (delegated to destiny_common) ──


def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse agenda items. Delegates to destiny_common."""
    return _parse_agenda_items(html, meeting_seq)
