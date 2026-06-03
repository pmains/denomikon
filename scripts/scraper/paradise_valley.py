"""
Town of Paradise Valley meeting extraction via Granicus RSS.

Granicus RSS: https://paradisevalleyaz.granicus.com/ViewPublisherRSS.php?view_id=2&mode=agendas
"""

from __future__ import annotations
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import urllib.request
import urllib.parse

log = logging.getLogger(__name__)

BASE_URL = "https://paradisevalleyaz.granicus.com"
VIEW_ID = 2
SOURCE_SYSTEM = "granicus"
JURISDICTION_ID = 15  # Paradise Valley

RSS_URL = f"{BASE_URL}/ViewPublisherRSS.php?view_id={VIEW_ID}&mode=agendas"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

BODY_MAP: dict[str, tuple[str, str, str]] = {
    "Town Council": ("paradise-valley-cc", "paradise-valley-cc", "Town Council"),
    "Planning Commission": ("paradise-valley-pc", "paradise-valley-pc", "Planning Commission"),
    "Board of Adjustment": ("paradise-valley-boa", "paradise-valley-boa", "Board of Adjustment"),
}

DEFAULT_BODY_SLUGS = ["paradise-valley-cc", "paradise-valley-pc"]


def _resolve_body(title: str) -> tuple[str, str, str]:
    for key, (slug, code, display) in BODY_MAP.items():
        if key.lower() in title.lower():
            return slug, code, display
    return "paradise-valley-cc", "paradise-valley-cc", title


def fetch_rss() -> Optional[str]:
    try:
        req = urllib.request.Request(RSS_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch Paradise Valley RSS: %s", e)
        return None


def search_meetings() -> list[dict]:
    """Search Paradise Valley meetings via Granicus RSS feed."""
    rss_xml = fetch_rss()
    if not rss_xml:
        return []

    meetings: list[dict] = []
    try:
        root = ET.fromstring(rss_xml)
    except ET.ParseError:
        return []

    ns = {"": "http://www.w3.org/2005/Atom"}
    for item in root.iter("item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        if title_el is None:
            continue

        title = title_el.text or ""
        desc = desc_el.text or "" if desc_el is not None else ""

        # Extract event_id from description (CDATA contains full URL)
        event_match = re.search(r"event_id=(\d+)", desc)
        if not event_match:
            continue
        event_id = int(event_match.group(1))

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        meeting_date = date_match.group(1) if date_match else ""

        body_name = title.split(" on ")[0].split(" - ")[0].strip() if " on " in title else "Town Council"
        slug, code, display = _resolve_body(body_name)
        agenda_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&event_id={event_id}"

        meetings.append({
            "meeting_id": str(event_id),
            "meeting_date": meeting_date,
            "meeting_type": display,
            "meeting_title": title.split(" - ")[0].strip() if " - " in title else body_name,
            "body_slug": slug,
            "body_code": code,
            "body_name": body_name,
            "agenda_url": agenda_url,
            "source_url": agenda_url,
            "source_system": SOURCE_SYSTEM,
        })

    return meetings
