"""
Reusable CivicClerk platform scraper — used by Surprise, Avondale, and any
other city that uses CivicClerk for meeting management.

API: https://{city}.api.civicclerk.com/v1
Portal: https://{city}.portal.civicclerk.com
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
from typing import Optional

log = logging.getLogger(__name__)


class CivicClerkConfig:
    """Per-jurisdiction CivicClerk configuration."""

    def __init__(
        self,
        subdomain: str,
        body_map: dict[str, tuple[str, str, str]],
        default_body: str = "city-council",
    ):
        self.subdomain = subdomain
        self.api_base = f"https://{subdomain}.api.civicclerk.com/v1"
        self.portal_base = f"https://{subdomain}.portal.civicclerk.com"
        self.body_map = body_map
        self.default_body = default_body


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def resolve_body(config: CivicClerkConfig, category_name: str) -> tuple[str, str, str]:
    """Map CivicClerk category name to (slug, body_code, display_name)."""
    key = category_name.strip()
    if key in config.body_map:
        return config.body_map[key]
    for pattern, (slug, code, name) in config.body_map.items():
        if pattern.lower() in key.lower() or key.lower() in pattern.lower():
            return slug, code, name
    return config.default_body, config.default_body, key


def fetch_events(config: CivicClerkConfig, start_date: str = "2026-01-01") -> list[dict]:
    """Fetch all CivicClerk events from start_date onward, paginating."""
    all_events: list[dict] = []
    params = urllib.parse.urlencode({
        "$filter": f"eventDate ge {start_date}",
        "$orderby": "eventDate desc",
        "$top": 100,
    })
    url = f"{config.api_base}/Events?{params}"

    while url:
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log.warning("Failed to fetch events from %s: %s", config.subdomain, e)
            break
        all_events.extend(data.get("value", []))
        url = data.get("@odata.nextLink", "")

    return all_events


def parse_events_to_meetings(config: CivicClerkConfig, events: list[dict]) -> list[dict]:
    """Parse CivicClerk events into meeting dicts."""
    meetings: list[dict] = []
    for e in events:
        cat = e.get("categoryName", "") or e.get("eventName", "")
        slug, code, display = resolve_body(config, cat)
        date_raw = (e.get("eventDate") or "")[:10]
        event_id = e.get("id")
        portal_url = f"{config.portal_base}/event/{event_id}/overview" if event_id else ""

        agenda_url = ""
        minutes_url = ""
        for pf in e.get("publishedFiles", []):
            ftype = pf.get("type", "")
            fid = pf.get("fileId")
            if ftype == "Agenda" and not agenda_url and fid:
                agenda_url = f"{config.api_base}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
            elif ftype == "Minutes" and not minutes_url and fid:
                minutes_url = f"{config.api_base}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"

        meetings.append({
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
        })
    return meetings


def search_meetings(
    config: CivicClerkConfig,
    start_date: str = "2026-01-01",
    body_slugs: Optional[list[str]] = None,
) -> list[dict]:
    """Search meetings via CivicClerk API."""
    events = fetch_events(config, start_date)
    meetings = parse_events_to_meetings(config, events)

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
    """Parse numbered agenda items from PDF text."""
    items: list[dict] = []
    sort_order = 0
    lines = text.split("\n") if text else []
    seen: set[str] = set()

    for line in lines:
        s = line.strip()
        if not s:
            continue
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


def fetch_and_parse_agenda(agenda_url: str, meeting_id: str, body_code: str = "avondale-cc") -> list[dict]:
    """Download and parse agenda items from a CivicClerk agenda PDF."""
    pdf_bytes = fetch_pdf_bytes(agenda_url)
    if not pdf_bytes:
        return []
    text = extract_pdf_text(pdf_bytes)
    if not text or len(text) < 100:
        return []
    items = parse_agenda_items(text, meeting_id)
    for item in items:
        an = item.get("agenda_item_number", "") or ""
        item["agenda_item_id"] = f"{body_code}-{meeting_id}_{an}"
        item["source_body"] = body_code
        item["source_url"] = agenda_url
    return items
