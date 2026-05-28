"""
City of Buckeye meeting extraction via Granicus ViewPublisher.

Buckeye uses the Granicus platform at ``buckeyeaz.granicus.com``.
Meetings are listed on the ViewPublisher page with PDF-based agenda packets.

Sources:
  https://buckeyeaz.granicus.com/ViewPublisher.php?view_id=1  (HTML meeting list)
  https://buckeyeaz.granicus.com/ViewPublisherRSS.php?view_id=1  (RSS feed)
  https://buckeyeaz.granicus.com/AgendaViewer.php?view_id=1&event_id=X  (PDF agenda)
"""

from __future__ import annotations
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──

PUBLIC_BODY_CODE = "buckeye-cc"
DEFAULT_BODY_SLUGS = ["buckeye-city-council"]

BASE_URL = "https://buckeyeaz.granicus.com"
SOURCE_INSTANCE_URL = BASE_URL
SOURCE_SYSTEM = "granicus"

VIEW_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Body name patterns from the RSS feed titles
# Each entry: pattern → (slug, code, display_name)
_BODY_PATTERNS: list[tuple[str, str, str, str]] = [
    (r"regular\s+council\s+meeting", "buckeye-city-council", "buckeye-cc", "City Council Regular"),
    (r"council\s+workshop", "buckeye-city-council", "buckeye-cc", "City Council Workshop"),
    (r"special\s+council\s+meeting", "buckeye-city-council", "buckeye-cc", "City Council Special"),
    (r"council\s+executive\s+session", "buckeye-city-council", "buckeye-cc", "City Council Executive"),
    (r"planning\s+(and|&)\s+zoning", "buckeye-planning-zoning", "buckeye-pz", "Planning & Zoning"),
    (r"board\s+of\s+adjustment", "buckeye-board-of-adjustment", "buckeye-boa", "Board of Adjustment"),
    (r"parks?\s+(and|&)\s+recre?c?r?a?t?i?o?n?", "buckeye-parks-rec", "buckeye-prc", "Parks & Recreation"),
    (r"historic\s+preservation", "buckeye-historic-preservation", "buckeye-hpc", "Historic Preservation"),
    (r"library\s+(advisory\s+)?board", "buckeye-library-board", "buckeye-library", "Library Board"),
    (r"public\s+safety\s+retirement", "buckeye-psprs", "buckeye-psprs", "PSPRS"),
    (r"airport\s+advisory", "buckeye-airport-advisory", "buckeye-airport", "Airport Advisory"),
    (r"youth\s+council", "buckeye-youth-council", "buckeye-youth", "Youth Council"),
    (r"community\s+facilities\s+district", "buckeye-cfd", "buckeye-cfd", "CFD"),
    (r"pollution\s+control", "buckeye-pollution-control", "buckeye-pollution", "Pollution Control"),
    (r"judicial\s+selection", "buckeye-judicial-selection", "buckeye-judicial", "Judicial Selection"),
]

# Default: match anything not caught above as generic city council
_DEFAULT_SLUG = "buckeye-city-council"
_DEFAULT_CODE = "buckeye-cc"


def _resolve_body(title: str) -> tuple[str, str, str]:
    """Resolve a meeting title to (slug, code, meeting_type)."""
    lower = title.lower()
    for pattern, slug, code, mtype in _BODY_PATTERNS:
        if re.search(pattern, lower):
            return slug, code, mtype
    return _DEFAULT_SLUG, _DEFAULT_CODE, title


# ── Meeting extraction ──

def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def parse_view_publisher(html: str) -> list[dict]:
    """Parse the ViewPublisher.php HTML meeting list.

    Returns list of meeting dicts with keys:
      meeting_id, meeting_date, meeting_type, meeting_title,
      body_slug, body_code, agenda_url, minutes_url, packet_url
    """
    meetings: list[dict] = []
    rows = re.findall(
        r'<tr\s+class="listingRow">(.*?)</tr>',
        html, re.DOTALL
    )
    for row in rows:
        # Meeting name
        name_match = re.search(
            r'class="listItem"[^>]*headers="Name"[^>]*>(.*?)</td>', row
        )
        if not name_match:
            continue
        name = re.sub(r"<[^>]+>", " ", name_match.group(1)).strip()
        if not name:
            continue

        # Date
        date_match = re.search(
            r'class="listItem"[^>]*headers="Date"[^>]*>(.*?)</td>', row
        )
        date_raw = re.sub(r"<[^>]+>", " ", date_match.group(1)).strip() if date_match else ""
        date_raw = date_raw.replace("&nbsp;", " ").strip()
        
        # Try to parse date from "July 21, 2026" format
        meeting_date = _parse_granicus_date(date_raw)

        # Event ID from agenda link
        event_id = ""
        agenda_link = re.search(r'href="([^"]*event_id=(\d+))"', row)
        if agenda_link:
            event_id = agenda_link.group(2)

        # Agenda packet PDF URL
        packet_url = ""
        packet_match = re.search(r'href="(https://[^"]*\.pdf)"[^>]*>Agenda\s*Packet', row)
        if packet_match:
            packet_url = packet_match.group(1)

        # Minutes link
        minutes_match = re.search(r'href="([^"]*event_id=(\d+))"[^>]*>Minutes', row)
        minutes_url = ""
        if minutes_match:
            minutes_url = urllib.parse.urljoin(BASE_URL, minutes_match.group(1))

        slug, code, mtype = _resolve_body(name)

        meetings.append({
            "meeting_id": event_id or f"buckeye-{date_raw}",
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": name,
            "body_slug": slug,
            "body_code": code,
            "event_id": event_id,
            "agenda_url": f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&event_id={event_id}" if event_id else "",
            "minutes_url": minutes_url,
            "packet_url": packet_url,
            "source_url": f"{BASE_URL}/ViewPublisher.php?view_id={VIEW_ID}",
        })

    return meetings


def _parse_granicus_date(date_str: str) -> str:
    """Parse Granicus date format to YYYY-MM-DD."""
    from datetime import datetime
    date_str = date_str.strip()
    # "July 21, 2026"
    for fmt in ["%B %d, %Y", "%B %d %Y", "%b %d, %Y"]:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def fetch_rss_meetings() -> list[dict]:
    """Fetch meetings from the Granicus RSS feed (includes all bodies)."""
    url = f"{BASE_URL}/ViewPublisherRSS.php?view_id={VIEW_ID}&mode=minutes"
    rss_xml = fetch_page(url)
    meetings: list[dict] = []

    # RSS 2.0 format: <rss><channel><item>...</item></channel></rss>
    items = re.findall(
        r"<item>(.*?)</item>",
        rss_xml, re.DOTALL
    )

    for item_xml in items:
        # Title
        title_m = re.search(r"<title>(.*?)</title>", item_xml)
        if not title_m:
            continue
        title = title_m.group(1).strip()

        # Parse title: "Regular Council Meeting - May 20, 2026"
        meeting_name = title
        meeting_date = ""
        if " - " in title:
            parts = title.rsplit(" - ", 1)
            meeting_name = parts[0].strip()
            try:
                dt = datetime.strptime(parts[1].strip(), "%B %d, %Y")
                meeting_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                meeting_date = parts[1].strip()

        # Link (MinutesViewer)
        link_m = re.search(r"<link>(.*?)</link>", item_xml)
        link_url = link_m.group(1).strip() if link_m else ""

        # Extract event/clip ID from link
        event_id = ""
        agenda_url = ""
        minutes_url = link_url
        m = re.search(r"clip_id=(\d+)", link_url)
        if m:
            event_id = m.group(1)
            agenda_url = f"{BASE_URL}/AgendaViewer.php?view_id={VIEW_ID}&event_id={event_id}"

        slug, code, mtype = _resolve_body(meeting_name)

        meetings.append({
            "meeting_id": event_id or f"rss-{len(meetings)}",
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": meeting_name,
            "body_slug": slug,
            "body_code": code,
            "event_id": event_id,
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "packet_url": "",
            "source_url": url,
        })

    return meetings


def search_buckeye_meetings(
    year: int,
    body_slugs: Optional[list[str]] = None,
) -> list[dict]:
    """Search Buckeye meetings for a given year.

    Uses the RSS feed which includes all bodies and goes back ~100 meetings.
    """
    meetings = fetch_rss_meetings()

    # Filter by year
    year_str = str(year)
    filtered = [m for m in meetings if m["meeting_date"].startswith(year_str)]

    # Filter by body slugs
    if body_slugs:
        filtered = [m for m in filtered if m["body_slug"] in body_slugs]

    return filtered


# ── Agenda item extraction from PDF ──

def fetch_agenda_pdf_bytes(pdf_url: str) -> Optional[bytes]:
    import urllib.request
    try:
        req = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Agenda PDF not available: %s", e)
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


def parse_agenda_pdf_items(text: str) -> list[dict]:
    """Parse agenda items from a Buckeye agenda packet PDF text.

    Buckeye agendas follow a standard format with numbered items.
    """
    items: list[dict] = []
    sort_order = 0
    lines = text.split("\n") if text else []

    # Look for numbered items
    item_re = re.compile(r"^\s*(\d+)\.\s+(.+?)(?:\s*\([^)]*\))?$")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = item_re.match(line)
        if m:
            sort_order += 1
            items.append({
                "agenda_item_number": m.group(1),
                "agenda_item_title": m.group(2).strip(),
                "agenda_item_text": line,
            })

    return items
