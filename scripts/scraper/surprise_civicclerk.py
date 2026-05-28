"""
City of Surprise meeting extraction via CivicClerk API.

Surprise uses CivicClerk for Planning & Zoning Commission and other boards.
City Council data is synced separately via Granicus (surprise.py).

API: https://surpriseaz.api.civicclerk.com/v1
Portal: https://surpriseaz.portal.civicclerk.com/event/{eventId}/overview
"""

from __future__ import annotations
import json
import logging
import re
import subprocess
import tempfile
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

API_BASE = "https://surpriseaz.api.civicclerk.com/v1"
PORTAL_BASE = "https://surpriseaz.portal.civicclerk.com"
SOURCE_SYSTEM = "civicclerk"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

# Map CivicClerk categoryName → (slug, body_code, display_name)
BODY_MAP: dict[str, tuple[str, str, str]] = {
    "Planning and Zoning Commission": ("surprise-pz", "surprise-pz", "Planning & Zoning Commission"),
    "Regular City Council Meeting": ("surprise-cc", "surprise-cc", "City Council"),
    "Regular City Council Work Session": ("surprise-cc", "surprise-cc", "City Council Work Session"),
    "Special City Council Meeting": ("surprise-cc", "surprise-cc", "Special City Council Meeting"),
    "Arts and Cultural Advisory Commission": ("surprise-arts", "surprise-arts", "Arts & Cultural Advisory Commission"),
    "Arts & Cultural Advisory Commission": ("surprise-arts", "surprise-arts", "Arts & Cultural Advisory Commission"),
    "Veteran, Disability and Human Service Commission": ("surprise-veterans", "surprise-veterans", "Veterans, Disability & Human Services"),
    "Library Commission": ("surprise-library", "surprise-library", "Library Commission"),
    "Library Advisory Commission": ("surprise-library", "surprise-library", "Library Advisory Commission"),
    "Parks and Recreation Commission": ("surprise-parks", "surprise-parks", "Parks & Recreation Commission"),
    "Public Safety Personnel Retirement System Commission \u2013 Fire": ("surprise-psprs-fire", "surprise-psprs-fire", "PSPRS Fire"),
    "Public Safety Personnel Retirement System Commission \u2013 Police": ("surprise-psprs-police", "surprise-psprs-police", "PSPRS Police"),
    "Health Benefits Trust Fund Board": ("surprise-health-benefits", "surprise-health-benefits", "Health Benefits Trust Fund Board"),
    "Boards and Commissions Nominations Committee": ("surprise-nominations", "surprise-nominations", "Boards & Commissions Nominations"),
    "City Audit Committee": ("surprise-audit", "surprise-audit", "City Audit Committee"),
    "Tourism Fund Subcommittee": ("surprise-tourism", "surprise-tourism", "Tourism Fund Subcommittee"),
    "Judicial Selection Advisory Commission": ("surprise-judicial-selection", "surprise-judicial-selection", "Judicial Selection Advisory Commission"),
}

DEFAULT_BODY_SLUGS = ["surprise-pz"]


def _resolve_body(category_name: str) -> tuple[str, str, str]:
    """Map CivicClerk category name to (slug, body_code, display_name)."""
    key = category_name.strip()
    if key in BODY_MAP:
        return BODY_MAP[key]
    # Partial match
    for pattern, (slug, code, name) in BODY_MAP.items():
        if pattern.lower() in key.lower() or key.lower() in pattern.lower():
            return slug, code, name
    return "surprise-pz", "surprise-pz", key


def fetch_events(start_date: str = "2026-01-01") -> list[dict]:
    """Fetch all CivicClerk events from start_date onward, paginating."""
    all_events: list[dict] = []
    params = urllib.parse.urlencode({
        "$filter": f"eventDate ge {start_date}",
        "$orderby": "eventDate desc",
        "$top": 100,
    })
    url = f"{API_BASE}/Events?{params}"

    while url:
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log.warning("Failed to fetch events: %s", e)
            break
        events = data.get("value", [])
        all_events.extend(events)
        url = data.get("@odata.nextLink", "")

    return all_events


def parse_events_to_meetings(events: list[dict]) -> list[dict]:
    """Parse CivicClerk events into our meeting dict format."""
    meetings: list[dict] = []
    for e in events:
        cat = e.get("categoryName", "") or e.get("eventName", "")
        slug, code, display = _resolve_body(cat)
        date_raw = (e.get("eventDate") or "")[:10]
        event_id = e.get("id")

        # Build portal URL
        portal_url = f"{PORTAL_BASE}/event/{event_id}/overview" if event_id else ""

        # Find agenda file from publishedFiles
        agenda_url = ""
        minutes_url = ""
        for pf in e.get("publishedFiles", []):
            ftype = pf.get("type", "")
            if ftype == "Agenda" and not agenda_url:
                agenda_url = f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={pf['fileId']},plainText=false)"
            elif ftype == "Minutes" and not minutes_url:
                minutes_url = f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={pf['fileId']},plainText=false)"

        meeting = {
            "meeting_id": str(event_id) if event_id else f"cc-{date_raw}",
            "meeting_date": date_raw,
            "meeting_type": display,
            "meeting_title": e.get("eventName", ""),
            "body_slug": slug,
            "body_code": code,
            "event_id": event_id,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "source_url": portal_url,
            "source_system": SOURCE_SYSTEM,
        }
        meetings.append(meeting)
    return meetings


def search_meetings(
    start_date: str = "2026-01-01",
    body_slugs: Optional[list[str]] = None,
) -> list[dict]:
    """Search Surprise CivicClerk meetings."""
    events = fetch_events(start_date)
    meetings = parse_events_to_meetings(events)

    if body_slugs:
        meetings = [m for m in meetings if m["body_slug"] in body_slugs]

    return meetings


# ── PDF extraction ──

def fetch_pdf_bytes(url: str) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def parse_agenda_items(text: str, meeting_id: str) -> list[dict]:
    """Parse agenda items from a Surprise agenda PDF.

    Surprise agendas typically have numbered items:
      1. Call to Order
      2. Pledge of Allegiance
      3. Approval of Minutes
      ...
    """
    items: list[dict] = []
    sort_order = 0
    lines = text.split("\n") if text else []
    seen: set[str] = set()

    for line in lines:
        s = line.strip()
        if not s:
            continue

        # "1.    Call to Order" or "1  Call to Order"
        m = re.match(r"^\s*(\d+)\.?\s+(.+?)$", s)
        if m:
            num = m.group(1)
            title = m.group(2).strip()
            key = f"{num}:{title[:40]}"
            if key in seen:
                continue
            seen.add(key)

            sort_order += 1
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": num,
                "item_type_category": "item",
                "agenda_item_title": title,
                "agenda_item_text": s,
                "sort_order": sort_order,
            })

    return items


def fetch_and_parse_agenda(agenda_url: str, meeting_id: str) -> list[dict]:
    """Download and parse agenda items from a Surprise CivicClerk agenda PDF."""
    pdf_bytes = fetch_pdf_bytes(agenda_url)
    if not pdf_bytes:
        return []
    text = extract_pdf_text(pdf_bytes)
    if not text or len(text) < 100:
        return []
    items = parse_agenda_items(text, meeting_id)
    for item in items:
        an = item.get("agenda_item_number", "") or ""
        item["agenda_item_id"] = f"surprise-cc-{meeting_id}_{an}"
        item["source_body"] = "surprise-cc"
        item["source_url"] = agenda_url
    return items
