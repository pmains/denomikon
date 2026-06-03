"""
Town of Queen Creek meeting extraction via Granicus RSS.

Granicus RSS: https://queencreekaz.granicus.com/ViewPublisherRSS.php?view_id=3&mode=agendas
"""

from __future__ import annotations
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

import urllib.request

log = logging.getLogger(__name__)

BASE_URL = "https://queencreekaz.granicus.com"
VIEW_ID = 3
SOURCE_SYSTEM = "granicus"
JURISDICTION_ID = 16  # Queen Creek

RSS_URL = f"{BASE_URL}/ViewPublisherRSS.php?view_id={VIEW_ID}&mode=agendas"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

BODY_MAP: dict[str, tuple[str, str, str]] = {
    "Town Council": ("queen-creek-cc", "queen-creek-cc", "Town Council"),
    "Planning and Zoning Commission": ("queen-creek-pz", "queen-creek-pz", "Planning & Zoning Commission"),
    "Parks and Recreation": ("queen-creek-parks", "queen-creek-parks", "Parks & Recreation"),
    "Economic Development": ("queen-creek-ed", "queen-creek-ed", "Economic Development"),
    "Board of Adjustment": ("queen-creek-boa", "queen-creek-boa", "Board of Adjustment"),
}

DEFAULT_BODY_SLUGS = ["queen-creek-cc", "queen-creek-pz"]


def _resolve_body(title: str) -> tuple[str, str, str]:
    for key, (slug, code, display) in BODY_MAP.items():
        if key.lower() in title.lower():
            return slug, code, display
    return "queen-creek-cc", "queen-creek-cc", title


def fetch_rss() -> Optional[str]:
    try:
        req = urllib.request.Request(RSS_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch Queen Creek RSS: %s", e)
        return None


def search_meetings() -> list[dict]:
    """Search Queen Creek meetings via Granicus RSS feed."""
    rss_xml = fetch_rss()
    if not rss_xml:
        return []

    meetings: list[dict] = []
    try:
        root = ET.fromstring(rss_xml)
    except ET.ParseError:
        return []

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        if title_el is None or link_el is None:
            continue

        title = title_el.text or ""
        link = link_el.text or ""

        event_match = re.search(r"event_id=(\d+)", link)
        if not event_match:
            continue
        event_id = int(event_match.group(1))
        # Try YYYY-MM-DD format first, then "Mon DD, YYYY" format
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        if not date_match:
            date_match = re.search(r"(\w{3,9}\s+\d{1,2},\s+\d{4})", title)
        if date_match:
            meeting_date = date_match.group(1)
            # Normalize "Jun 03, 2026" → "2026-06-03"
            if not re.match(r"\d{4}-\d{2}-\d{2}", meeting_date):
                from datetime import datetime
                try:
                    meeting_date = datetime.strptime(meeting_date, "%b %d, %Y").strftime("%Y-%m-%d")
                except ValueError:
                    meeting_date = ""
        else:
            meeting_date = ""
        body_name = title.split(" - ")[0].strip() if " - " in title else title
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
            "source_url": link,
            "source_system": SOURCE_SYSTEM,
        })

    return meetings
