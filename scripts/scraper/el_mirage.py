"""
City of El Mirage agenda extraction via Destiny (AgendaQuick).

El Mirage uses the Destiny/AgendaQuick platform at
``public.destinyhosted.com`` with organization ID 35647.

Same scraper pattern as Chandler, Glendale, and Goodyear.
"""

from __future__ import annotations
import logging
import re
import urllib.parse
from typing import Optional

from scraper.io_utils import _normalize_text_date

log = logging.getLogger(__name__)

# ── Constants ──

ORG_ID = "35647"
PUBLIC_BODY_CODE = "el-mirage-cc"
DEFAULT_BODY_SLUGS = ["el-mirage-city-council"]

BASE_URL = "https://public.destinyhosted.com"
SOURCE_INSTANCE_URL = "https://public.destinyhosted.com"
SOURCE_SYSTEM = "agendaquick"

# Body map: display name → (slug, body_code)
# Committee IDs from the Destiny portal (id=35647)
BODY_MAP: dict[str, tuple[str, str]] = {
    "city council": ("el-mirage-city-council", "el-mirage-cc"),
    "youth advisory commission": ("el-mirage-youth-advisory", "el-mirage-yac"),
    "public safety personnel retirement": ("el-mirage-psprs", "el-mirage-psprs"),
    "planning and zoning": ("el-mirage-planning-zoning", "el-mirage-pz"),
    "meeting": ("el-mirage-city-council", "el-mirage-cc"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    """Resolve a body name to (slug, code)."""
    key = body_name.lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "el-mirage-city-council", "el-mirage-cc"


def extract_meeting_type(body_name: str) -> str:
    """Extract meeting type from body name."""
    tl = body_name.lower()
    if "cancellation" in tl or "canceled" in tl or "cancelled" in tl:
        return "Cancelled"
    if "quorum notice" in tl or "quorum notices" in tl:
        return "Quorum Notice"
    if "study session" in tl:
        return "Study Session"
    if "work session" in tl:
        return "Work Session"
    if "special meeting" in tl or tl.endswith("special"):
        return "Special"
    if "regular meeting" in tl:
        return "Regular Meeting"
    return "Regular Meeting"


def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def build_month_url(year: int, month: int) -> str:
    return (
        f"{BASE_URL}/agenda_publish.cfm?id={ORG_ID}"
        f"&mt=ALL&get_month={month}&get_year={year}"
    )


def parse_meetings(html: str) -> list[dict]:
    """Parse meeting list from the Destiny portal page."""
    meetings: list[dict] = []
    # Each meeting is in a table row with columns: date, name, minutes, links
    row_re = re.compile(
        r'<tr[^>]*>\s*<td[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>'
        r'\s*([A-Z][a-z]+ \d+, \d{4})\s*</a>'
    )
    for m in row_re.finditer(html):
        href = m.group(1)
        date_raw = m.group(2)
        date = _normalize_text_date(date_raw) or date_raw

        # Find the full row to extract body name
        row_start = html.rfind("<tr", 0, m.start())
        row_end = html.find("</tr>", m.end())
        if row_end > 0 and row_start >= 0:
            row = html[row_start:row_end]
        else:
            continue

        # Extract body name from the second column
        cols = re.findall(r'<td[^>]*class="mediumText"[^>]*>(.*?)</td>', row, re.DOTALL)
        body_name = ""
        if len(cols) >= 2:
            body_name = re.sub(r"<[^>]+>", " ", cols[1]).strip()

        if not body_name:
            continue

        # Skip "Meeting Results" and "Minutes" rows (these are detail links)
        if body_name.startswith("Meeting Results") or body_name.startswith("Minutes"):
            continue

        slug, code = _resolve_body(body_name)
        meeting_type = extract_meeting_type(body_name)

        # Get agenda URL (decode HTML entities like &amp;)
        agenda_url = urllib.parse.urljoin(BASE_URL, href)
        agenda_url = agenda_url.replace("&amp;", "&")

        meetings.append({
            "meeting_date": date,
            "body_name": body_name,
            "body_slug": slug,
            "body_code": code,
            "meeting_type": meeting_type,
            "meeting_id": meeting_id_from_url(agenda_url),
            "agenda_url": agenda_url,
        })
    return meetings


def meeting_id_from_url(url: str) -> str:
    """Extract meeting seq from a Destiny agenda URL."""
    m = re.search(r"seq=(\d+)", url)
    return m.group(1) if m else ""


def search_el_mirage_meetings(year: int, body_slugs: Optional[list[str]] = None) -> list[dict]:
    """Search El Mirage meetings for a given year, month by month."""
    all_m: list[dict] = []
    for m in range(1, 13):
        try:
            html = fetch_page(build_month_url(year, m), timeout=15)
            month_meetings = parse_meetings(html)
            all_m.extend(month_meetings)
        except Exception as e:
            log.warning("El Mirage %d-%02d failed: %s", year, m, e)
    if body_slugs:
        return [m for m in all_m if m["body_slug"] in body_slugs]
    return all_m


# ── Agenda item parsing ──

def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse agenda items from an El Mirage agenda detail page.

    El Mirage uses a Destiny format with:
      <td>1.</td>
      <td>...</td><td>...</td><td>...</td>
      <td colspan="2"><strong>ITEM TITLE</strong></td>

    Motion text follows in the row after the title cell.
    """
    items: list[dict] = []
    sort_order = 0

    # Look for item rows: <td>N.</td> followed by title in colspan td
    item_re = re.compile(
        r'<td[^>]*>\s*(\d[\w.-]*)\s*\.\s*</td>'
        r'(?:.*?<td[^>]*colspan="?\d+"?[^>]*>)?\s*'
        r'<strong[^>]*>(.*?)</strong>',
        re.DOTALL,
    )

    for m in item_re.finditer(html):
        item_number = m.group(1)
        title_html = m.group(2)
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = re.sub(r"\s+", " ", title).replace("&nbsp;", " ").strip()
        if not title or len(title) < 3:
            continue
        sort_order += 1

        # Find motion text after this item
        motion_text = ""
        after = html[m.end():m.end() + 4000]
        motion_m = re.search(
            r"Move\s+(?:City Council|Commission|Board|the Council)\s+", after,
        )
        if motion_m:
            ms = motion_m.start()
            me = after.find("</td>", ms)
            if me > 0:
                motion_text = re.sub(r"<[^>]+>", " ", after[ms:me])
            else:
                me2 = after.find("<tr", ms)
                if me2 > 0:
                    motion_text = re.sub(r"<[^>]+>", " ", after[ms:me2])
            motion_text = re.sub(r"\s+", " ", motion_text).strip()

        items.append({
            "meeting_id": meeting_seq,
            "agenda_item_number": item_number,
            "agenda_item_title": title,
            "agenda_item_text": motion_text,
            "item_type": "",
            "sort_order": sort_order,
        })
    return items


# ── Vote / Results PDF parsing ──

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
        # Look for vote lines with counts
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
