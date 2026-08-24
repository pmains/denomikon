"""
City of El Mirage agenda extraction via Destiny (AgendaQuick).

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
    parse_agenda_items as _parse_agenda_items,
    parse_meetings as _parse_meetings,
)

log = logging.getLogger(__name__)

# ── Constants ──

ORG_ID = "35647"
PUBLIC_BODY_CODE = "el-mirage-cc"
DEFAULT_BODY_SLUGS = [
    "el-mirage-city-council",
    "el-mirage-planning-zoning",
    "el-mirage-youth-advisory",
    "el-mirage-psprs",
    "el-mirage-parks-recreation",
    "el-mirage-employee-relations",
    "el-mirage-public-safety",
    "el-mirage-recreation",
    "el-mirage-dusd",
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
    "city council": ("el-mirage-city-council", "el-mirage-cc"),
    "council work session": ("el-mirage-city-council", "el-mirage-cc"),
    "youth advisory commission": ("el-mirage-youth-advisory", "el-mirage-yac"),
    "youth advisory": ("el-mirage-youth-advisory", "el-mirage-yac"),
    "public safety personnel retirement": ("el-mirage-psprs", "el-mirage-psprs"),
    "psprs": ("el-mirage-psprs", "el-mirage-psprs"),
    "planning and zoning": ("el-mirage-planning-zoning", "el-mirage-pz"),
    "planning &amp; zoning": ("el-mirage-planning-zoning", "el-mirage-pz"),
    "planning & zoning": ("el-mirage-planning-zoning", "el-mirage-pz"),
    "parks and rec task force": ("el-mirage-parks-recreation", "el-mirage-prtf"),
    "parks & rec task force": ("el-mirage-parks-recreation", "el-mirage-prtf"),
    "park and rec": ("el-mirage-parks-recreation", "el-mirage-prtf"),
    "employee relations": ("el-mirage-employee-relations", "el-mirage-er"),
    "public safety sub": ("el-mirage-public-safety", "el-mirage-pss"),
    "recreation sub": ("el-mirage-recreation", "el-mirage-rec"),
    "dusd": ("el-mirage-dusd", "el-mirage-dusd"),
    "meeting": ("el-mirage-city-council", "el-mirage-cc"),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    import re as _re
    key = _re.sub(r'\s+', ' ', body_name).lower().strip()
    """Resolve a Destiny body name to (slug, body_code)."""
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "el-mirage-city-council", "el-mirage-cc"


def meeting_id_from_url(url: str) -> str:
    """Extract meeting seq from a Destiny agenda URL."""
    import re
    m = re.search(r"seq=(\d+)", url)
    return m.group(1) if m else ""


# ── Meeting search ──


def search_el_mirage_meetings(
    year: int,
    body_slugs: Optional[list[str]] = None,
    start_month: int = 1,
    end_month: int = 12,
) -> list[dict]:
    """Search El Mirage meetings for a given year, month by month.

    Args:
        year: The year to search.
        body_slugs: Optional list of body slugs to filter by.
        start_month: First month to fetch (1-12, default 1).
        end_month: Last month to fetch (1-12, default 12).
    """
    all_m: list[dict] = []
    start_month = max(1, min(12, start_month))
    end_month = max(start_month, min(12, end_month))
    for m in range(start_month, end_month + 1):
        try:
            html = fetch_page(build_month_url(ORG_ID, year, m), timeout=15)
            month_meetings = _parse_meetings(html, BODY_MAP)
            all_m.extend(month_meetings)
        except Exception as e:
            log.warning("El Mirage %d-%02d failed: %s", year, m, e)
    if body_slugs:
        return [m for m in all_m if m["body_slug"] in body_slugs]
    return all_m


# ── Agenda item parsing (delegated to destiny_common) ──


def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse agenda items. Delegates to destiny_common.

    El Mirage uses the standard Destiny detail-page format.
    """
    return _parse_agenda_items(html, meeting_seq)


# ── Vote / Results PDF parsing (unchanged) ──


def fetch_results_pdf_bytes(results_url: str) -> Optional[bytes]:
    import urllib.request
    try:
        req = urllib.request.Request(results_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Results PDF not available: %s", e)
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


def parse_results_votes(text: str) -> dict:
    """Parse El Mirage Results PDF (same format as Chandler/Glendale)."""
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()
    lines = text.split("\n")
    i = 0
    vote_count_re = re.compile(r"(\d+)-(\d+)")
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        all_vc = list(vote_count_re.finditer(line))
        if not all_vc:
            i += 1
            continue
        vc = all_vc[-1]
        ayes_count = int(vc.group(1))
        nays_count = int(vc.group(2))
        result = "Carried Unanimously" if nays_count == 0 else "Carried"
        votes.append({
            "agenda_item_number": "",
            "motion_result": result,
            "supervisor_votes": [],
            "vote_text": line.strip(),
        })
        i += 1
    return {"supervisors": supervisors, "votes": votes}
