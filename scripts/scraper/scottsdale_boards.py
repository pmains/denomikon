"""
Scottsdale Boards & Commissions scraper.

Each board page has a year-by-year accordion with links to agenda PDFs:
  /Assets/ScottsdaleAZ/Boards/{BoardName}/agendas-minutes/{year}-agendas/*.pdf

Reuses the PDF parsing logic from scottsdale.py (parse_agenda_items).
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

from scraper.scottsdale import (
    download_pdf, extract_pdf_text, parse_agenda_items,
    HEADERS, BASE_URL,
)

log = logging.getLogger(__name__)

# ── Target boards for housing/construction ──

BOARDS = {
    "scottsdale-planning-commission": {
        "name": "Scottsdale Planning Commission",
        "slug": "scottsdale-planning-commission",
        "code": "scottsdale-pc",
        "page": "/boards/planning-commission",
        "folder": "Planning",
    },
    "scottsdale-board-of-adjustment": {
        "name": "Scottsdale Board of Adjustment",
        "slug": "scottsdale-board-of-adjustment",
        "code": "scottsdale-boa",
        "page": "/boards/board-of-adjustment",
        "folder": "Adjustment",
    },
    "scottsdale-development-review-board": {
        "name": "Scottsdale Development Review Board",
        "slug": "scottsdale-development-review-board",
        "code": "scottsdale-drb",
        "page": "/boards/development-review-board",
        "folder": "Development",
    },
    "scottsdale-historic-preservation-commission": {
        "name": "Scottsdale Historic Preservation Commission",
        "slug": "scottsdale-historic-preservation-commission",
        "code": "scottsdale-hpc",
        "page": "/boards/historic-preservation-commission",
        "folder": "Historic",
    },
    "scottsdale-building-advisory-board-of-appeals": {
        "name": "Scottsdale Building Advisory Board of Appeals",
        "slug": "scottsdale-building-advisory-board-of-appeals",
        "code": "scottsdale-baba",
        "page": "/boards/building-advisory-board-of-appeals",
        "folder": "Building",
    },
}

JURISDICTION_ID = 7


def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def parse_meetings_from_board_page(html: str, board_cfg: dict) -> list[dict]:
    """Parse year-by-year meeting PDF links from a board page."""
    meetings: list[dict] = []
    seen: set[str] = set()
    folder = board_cfg["folder"]
    code = board_cfg["code"]

    for m in re.finditer(
        r'href="([^"]*Boards/' + re.escape(folder) + r'/agendas-minutes/(\d{4})-agendas/([^"]+\.pdf))"',
        html,
    ):
        pdf_url = urllib.parse.urljoin(BASE_URL, m.group(1))
        year = m.group(2)
        filename = m.group(3).lower()

        # Extract date from filename (handles MM-DD-YY and MM-DD-YYYY)
        date_match = re.match(r"(\d{1,2})-(\d{1,2})-(\d{2,4})", filename)
        if not date_match:
            continue
        month, day, yr_str = int(date_match.group(1)), int(date_match.group(2)), date_match.group(3)
        if len(yr_str) == 2:
            full_year = 2000 + int(yr_str)
        else:
            full_year = int(yr_str)
        meeting_date = f"{month}/{day}/{full_year}"

        # Determine meeting type from filename
        meeting_type = "Regular Meeting"
        if "special" in filename or "study" in filename:
            meeting_type = "Special Meeting"
        if "cancellation" in filename or "public-notice" in filename:
            continue  # Skip non-meeting PDFs

        meeting_key = f"{year}-{folder}-{filename}"
        if meeting_key in seen:
            continue
        seen.add(meeting_key)

        meetings.append({
            "meeting_id": f"{folder}-{filename.replace('.pdf', '')}",
            "meeting_date": meeting_date,
            "meeting_type": meeting_type,
            "body_name": board_cfg["name"],
            "body_code": code,
            "body_slug": board_cfg["slug"],
            "agenda_url": pdf_url,
            "year": year,
        })

    return meetings


def _to_iso(date_str: str) -> str:
    """Normalize MM/DD/YYYY → YYYY-MM-DD for comparison."""
    parts = date_str.split("/")
    return f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"


def search_board_meetings(
    board_slug: str,
    year: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Search for a board's meetings, optionally filtered by year or date range.

    ``start_date`` and ``end_date`` are YYYY-MM-DD strings.  When both are
    provided ``year`` is ignored.
    """
    cfg = BOARDS.get(board_slug)
    if not cfg:
        log.warning("Unknown board: %s", board_slug)
        return []

    url = urllib.parse.urljoin(BASE_URL, cfg["page"])
    try:
        html = fetch_page(url, timeout=15)
    except Exception:
        return []

    all_meetings = parse_meetings_from_board_page(html, cfg)

    # Date-range filter takes precedence over year
    if start_date and end_date:
        all_meetings = [
            m for m in all_meetings
            if start_date <= _to_iso(m["meeting_date"]) <= end_date
        ]
    elif year:
        all_meetings = [m for m in all_meetings if m["year"] == str(year)]

    log.info("Found %d meetings for %s", len(all_meetings), cfg["name"])
    return all_meetings
