"""
City of Avondale meeting extraction via CivicClerk.

Avondale uses the CivicClerk platform at ``avondaleaz.portal.civicclerk.com``
with a REST/OData API at ``avondaleaz.api.civicclerk.com/v1``.

Same platform/API pattern as Surprise (``surpriseaz``).
"""

from __future__ import annotations
import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──

PUBLIC_BODY_CODE = "avondale-cc"
DEFAULT_BODY_SLUGS = ["avondale-city-council"]

BASE_URL = "https://avondaleaz.portal.civicclerk.com"
API_BASE = "https://avondaleaz.api.civicclerk.com/v1"
SOURCE_INSTANCE_URL = BASE_URL
SOURCE_SYSTEM = "civicclerk"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Body name → (slug, code)
_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    "city council": ("avondale-city-council", "avondale-cc"),
    "planning commission": ("avondale-planning-zoning", "avondale-pz"),
    "board of adjustment": ("avondale-board-of-adjustment", "avondale-boa"),
    "possible quorum": ("avondale-quorum", "avondale-quorum"),
    "parks and recreation": ("avondale-parks-rec", "avondale-prc"),
    "library board": ("avondale-library-board", "avondale-library"),
    "historic preservation": ("avondale-historic-preservation", "avondale-hpc"),
}


def _resolve_body(category_name: str) -> tuple[str, str, str]:
    """Resolve a category name to (slug, code, meeting_type)."""
    lower = category_name.lower().strip()
    for pattern, (slug, code) in _CATEGORY_MAP.items():
        if pattern in lower:
            return slug, code, category_name.strip()
    return "avondale-city-council", "avondale-cc", category_name.strip()


# ── API helpers ──

def _fetch_json(url: str, timeout: int = 15) -> dict | list:
    import urllib.request, json
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("API fetch failed: %s %s", url[:80], e)
        raise


def _build_url(base: str, path: str, params: Optional[dict] = None) -> str:
    if params:
        return f"{base}{path}?{urllib.parse.urlencode(params)}"
    return f"{base}{path}"


# ── Meeting extraction ──

def search_avondale_meetings(
    year: int,
    body_slugs: Optional[list[str]] = None,
) -> list[dict]:
    """Search Avondale meetings via CivicClerk API for a given year."""
    from urllib.parse import urlencode

    meetings: list[dict] = []
    year_start = f"{year}-01-01"
    year_end = f"{year}-12-31"

    # Fetch events via OData with year filter
    params = {
        "$filter": f"startDateTime ge {year_start} and startDateTime le {year_end}",
        "$orderby": "startDateTime desc",
        "$top": 200,
    }

    next_url = _build_url(API_BASE, "/Events", params)
    seen_ids: set[int] = set()

    while next_url and len(meetings) < 500:
        try:
            data = _fetch_json(next_url)
        except Exception as e:
            log.warning("Failed to fetch Avondale events page: %s", e)
            break

        events = data.get("value", []) if isinstance(data, dict) else []
        for event in events:
            eid = event.get("id")
            if eid in seen_ids:
                continue
            seen_ids.add(eid)

            event_name = event.get("eventName", "").strip()
            category_name = event.get("categoryName", "").strip()
            start = event.get("startDateTime", "")
            meeting_date = start[:10] if start else ""

            slug, code, mtype = _resolve_body(category_name or event_name)

            if body_slugs and slug not in body_slugs:
                continue

            # Build URLs
            portal_url = f"{BASE_URL}/event/{eid}/overview"
            agenda_id = event.get("agendaId")

            meetings.append({
                "meeting_id": str(eid),
                "meeting_date": meeting_date,
                "meeting_type": mtype,
                "meeting_title": event_name,
                "body_slug": slug,
                "body_code": code,
                "portal_url": portal_url,
                "agenda_id": agenda_id,
                "source_url": f"{API_BASE}/Events/{eid}",
            })

        # Paginate
        next_url = data.get("@odata.nextLink", "") if isinstance(data, dict) else ""

    return meetings


# ── Agenda item extraction ──

def fetch_agenda_items(event_id: str, agenda_id: int) -> list[dict]:
    """Fetch agenda items for an event from the Avondale CivicClerk API."""
    items: list[dict] = []

    try:
        # Try to get the meeting/agenda details
        meeting_url = f"{API_BASE}/Meetings/{agenda_id}"
        meeting_data = _fetch_json(meeting_url)
        if isinstance(meeting_data, dict):
            items.append({
                "agenda_item_number": "1",
                "agenda_item_title": meeting_data.get("name", "Agenda"),
                "agenda_item_text": meeting_data.get("description", ""),
            })
    except Exception as e:
        log.debug("Avondale agenda fetch failed for event %s: %s", event_id, e)

    return items


# ── Document download ──

def fetch_document_bytes(file_url: str) -> Optional[bytes]:
    import urllib.request
    try:
        req = urllib.request.Request(file_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Document fetch failed: %s", e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    import subprocess, tempfile, os
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass
