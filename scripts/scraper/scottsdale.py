"""
City of Scottsdale meeting and agenda extraction.

Scottsdale posts City Council agendas as PDF files in organized folders:
  /Assets/ScottsdaleAZ/Council/current-agendas-minutes/{year}-agendas/*.pdf

File naming convention:
  {MM}-{DD}-{YY}-{type}[-{subtype}]-agenda.pdf
  {MM}-{DD}-{YY}-approved-{type}[-{subtype}]-minutes.pdf

Usage:
    ./scrape scottsdale --sync [--year=2026]
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PUBLIC_BODY_CODE = "scottsdale-cc"
JURISDICTION_ID = 7

BASE_URL = "https://ww2.scottsdaleaz.gov"
CURRENT_PAGE = f"{BASE_URL}/council/meeting-information/agendas-minutes"
ARCHIVE_PAGE = CURRENT_PAGE + "/archived-agendas-minutes"
BASE_ASSETS = f"{BASE_URL}/Assets/ScottsdaleAZ/Council"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Mapping file type patterns to meeting type
TYPE_MAP = {
    "regular": "Regular Meeting",
    "regular and work study": "Regular Meeting",
    "special": "Special Meeting",
    "work study": "Work Study Session",
}

# Council member names for vote attribution
COUNCIL_NAMES = {
    "littlefield": "Mayor Littlefield",
    "kwasman": "Vice Mayor Kwasman",
    "mcallen": "Councilmember McAllen",
    "milhaven": "Councilmember Milhaven",
    "mesnard": "Councilmember Mesnard",
    "schwartz": "Councilmember Schwartz",
    "tabor": "Councilmember Tabor",
    "whitehead": "Councilmember Whitehead",
    "guy": "Councilmember Guy",
}


def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def download_pdf(url: str) -> Optional[bytes]:
    """Download a PDF file."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Failed to download %s: %s", url, e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a PDF using pdftotext."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        Path(pdf_path).unlink(missing_ok=True)
        return result.stdout.strip() if result.stdout.strip() else None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None


def _parse_meeting_type(filename: str) -> str:
    """Derive meeting type from filename."""
    lower = filename.lower()
    for pattern, mtype in TYPE_MAP.items():
        if pattern in lower:
            return mtype
    return "Regular Meeting"


def _parse_meeting_date(filename: str) -> str:
    """Extract date from filename like 05-19-26-regular-agenda.pdf → 5/19/2026."""
    m = re.match(r"(\d{2})-(\d{2})-(\d{2})", filename)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3))
        return f"{month}/{day}/{year}"
    return ""


def search_meetings(year: int) -> list[dict]:
    """Find all Council meeting PDFs for a given year.

    Returns list of dicts with keys:
      meeting_id (filename base), meeting_date, meeting_type,
      agenda_url, minutes_url
    """
    meetings_by_key: dict[str, dict] = {}
    assets_paths = ["current-agendas-minutes", "archive-agendas-minutes"]
    base_re = re.escape("/Assets/ScottsdaleAZ/Council/")

    for page_url in [CURRENT_PAGE, ARCHIVE_PAGE]:
        try:
            html = fetch_page(page_url, timeout=15)
        except Exception:
            continue

        # Match href="/Assets/ScottsdaleAZ/Council/*/{year}-agendas-or-minutes/*.pdf"
        pat = r'href="(' + base_re + r'[^"]*?/(' + str(year) + r')-(?:agendas|minutes)/([^"]*\.pdf))"'
        for m in re.finditer(pat, html):
            pdf_url = urllib.parse.urljoin(BASE_URL, m.group(1))
            filename = m.group(3).lower()

            # Only process agenda and minutes PDFs
            if not ("agenda" in filename or "minutes" in filename):
                continue
            if "cancellation" in filename or "community" in filename:
                continue
            if "approved" in filename and "minutes" not in filename:
                continue

            # Extract meeting key: date + type (e.g. "01-07-26-special")
            key = re.sub(r"-(?:approved|marked)-", "-", filename)  # strip -approved-/ -marked-
            key = re.sub(r"-(?:agenda|minutes)\.pdf$", "", key)

            meeting_date = _parse_meeting_date(filename)
            meeting_type = _parse_meeting_type(filename)

            if key not in meetings_by_key:
                meetings_by_key[key] = {
                    "meeting_id": key,
                    "meeting_date": meeting_date,
                    "meeting_type": meeting_type,
                    "body_name": "Scottsdale City Council " + meeting_type,
                    "body_code": PUBLIC_BODY_CODE,
                    "body_slug": "scottsdale-city-council",
                    "agenda_url": "",
                    "minutes_url": "",
                }

            if "minutes" in filename:
                meetings_by_key[key]["minutes_url"] = pdf_url
            else:
                meetings_by_key[key]["agenda_url"] = pdf_url

    return list(meetings_by_key.values())


def parse_agenda_items(pdf_bytes: bytes, meeting_id: str) -> list[dict]:
    """Parse agenda items from a Scottsdale Council meeting PDF.

    The PDF format has:
      N.    Item Name
      Request: [motion text]
      – Action Result

    Returns list of item dicts.
    """
    text = extract_pdf_text(pdf_bytes)
    if not text:
        return []

    items: list[dict] = []
    sort_order = 0
    lines = text.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Detect numbered items: "N.    Item Name"
        item_match = re.match(r"^(\d+)\.\s+(.*)", line)
        if not item_match:
            i += 1
            continue

        item_number = item_match.group(1)
        title = item_match.group(2).strip()

        # Collect the full title (next lines until blank or next item)
        j = i + 1
        while j < len(lines):
            next_line = lines[j].strip()
            if not next_line or re.match(r"^\d+\.", next_line):
                break
            if not next_line.startswith("Request:") and not next_line.startswith("–"):
                title += " " + next_line
            j += 1

        # Find motion text
        motion_text = ""
        action_result = ""
        for k in range(i + 1, min(i + 20, len(lines))):
            lk = lines[k].strip()
            if lk.startswith("Request:"):
                motion_text = lk
            elif lk.startswith("–"):
                action_result = lk

        sort_order += 1
        items.append({
            "meeting_id": meeting_id,
            "agenda_item_number": item_number,
            "agenda_item_title": title,
            "agenda_item_text": motion_text,
            "vote_or_action": action_result,
            "item_type": "",
            "agenda_category": "",
            "sort_order": sort_order,
        })

        i += 1

    return items


def parse_minutes_votes(pdf_bytes: bytes, meeting_id: str) -> dict:
    """Parse vote data from Scottsdale minutes PDFs.

    Format:
      Councilmember X made a motion to ... which carried X/Y,
      with [members] voting in the affirmative [and [members] dissenting].

    Returns dict with supervisors and votes lists.
    """
    text = extract_pdf_text(pdf_bytes)
    if not text:
        return {"supervisors": [], "votes": []}

    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()
    lines = text.splitlines()

    # Hardcoded Scottsdale council member names for 2026
    _KNOWN_MEMBERS = {
        "borowsky", "kwasman", "dubauskas", "graham", "littlefield", "mcallen", "whitehead",
    }
    _FULL_NAMES = {
        "borowsky": "Lisa Borowsky", "kwasman": "Adam Kwasman", "dubauskas": "Jan Dubauskas",
        "graham": "Barry Graham", "littlefield": "Kathy Littlefield", "mcallen": "Maryann McAllen",
        "whitehead": "Solange Whitehead",
    }

    # Parse roll call from the PRESENT line
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("Present:") and "Mayor" in s:
            # Extract names: extract last names from "Mayor Lisa Borowsky; Vice Mayor Adam Kwasman; ..."
            names_found = re.findall(r"([A-Z][a-zA-Z]+)\s*[;,]", s)
            for n in names_found:
                nkey = n.lower()
                if nkey in _KNOWN_MEMBERS and nkey not in seen_sup:
                    seen_sup.add(nkey)
                    full_name = _FULL_NAMES.get(nkey, n)
                    supervisors.append({
                        "name": full_name,
                        "normalized_name": full_name.lower(),
                        "present": True,
                    })
            # Also check next line for wrapped names
            for j in range(i + 1, min(i + 3, len(lines))):
                ns = lines[j].strip()
                if ns and "Council" not in ns and "Also" not in ns and "Staff" not in ns:
                    names_found = re.findall(r"([A-Z][a-zA-Z]+)", ns)
                    for n in names_found:
                        nkey = n.lower()
                        if nkey in _KNOWN_MEMBERS and nkey not in seen_sup:
                            seen_sup.add(nkey)
                            full_name = _FULL_NAMES.get(nkey, n)
                            supervisors.append({
                                "name": full_name,
                                "normalized_name": full_name.lower(),
                                "present": True,
                            })
            break

    # Find vote blocks: "which carried X/Y"
    for i, line in enumerate(lines):
        s = line.strip()
        carry_match = re.search(r"carried\s+(\d+)/(\d+)", s, re.IGNORECASE)
        if not carry_match:
            continue

        ayes_count = int(carry_match.group(1))
        nays_count = int(carry_match.group(2))

        # Find who voted how by looking for names in the affirmative/dissenting list
        # Pattern: "with [names] voting in the affirmative and [names] dissenting"
        ayes_names: list[str] = []
        nays_names: list[str] = []

        # Collect all names in this line and next few lines
        context = s
        for j in range(i + 1, min(i + 3, len(lines))):
            ns = lines[j].strip()
            if ns and "motion" not in ns.lower() and "vote" not in ns.lower() and len(ns) > 10:
                context += " " + ns

        # Extract names voting in the affirmative
        aff_section = ""
        diss_section = ""
        if "voting in the affirmative" in context:
            after_aff = context.split("voting in the affirmative", 1)[1]
            if "dissenting" in after_aff:
                aff_section = after_aff.split("dissenting", 1)[0]
                diss_section = "dissenting" + after_aff.split("dissenting", 1)[1]
            else:
                aff_section = after_aff

        if diss_section:
            # Extract dissenting names
            for m in re.finditer(r"([A-Z][a-zA-Z]+)\s+dissenting", diss_section):
                n = m.group(1).lower()
                if n in _KNOWN_MEMBERS:
                    nays_names.append(_FULL_NAMES.get(n, m.group(1)))

        if not ayes_names and not nays_names:
            # All members voted the same way (unanimous)
            for sup in supervisors:
                ayes_names.append(sup["name"])

        # Build supervisor votes
        svotes = []
        for name in ayes_names:
            nkey = name.lower().split()[-1] if " " in name else name.lower()
            svotes.append({"name": name, "vote": "yes", "raw_vote_text": nkey})
        for name in nays_names:
            nkey = name.lower().split()[-1] if " " in name else name.lower()
            svotes.append({"name": name, "vote": "no", "raw_vote_text": nkey})

        # Find item number
        item_num = ""
        for k in range(max(0, i - 5), i):
            im = re.search(r"ITEM\s+(\d+)", lines[k], re.IGNORECASE)
            if im:
                item_num = im.group(1)
                break
        if not item_num:
            for k in range(max(0, i - 3), i):
                im = re.search(r"(\d+)\.", lines[k])
                if im:
                    item_num = im.group(1)
                    break

        votes.append({
            "agenda_item_number": item_num,
            "ayes": ayes_names,
            "nays": nays_names,
            "motion_result": "Carried",
            "supervisor_votes": svotes,
            "vote_text": context[:300],
        })

    return {"supervisors": supervisors, "votes": votes}
