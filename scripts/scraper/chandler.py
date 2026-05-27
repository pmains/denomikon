"""
City of Chandler meeting and agenda extraction via AgendaQuick (Destiny Software).

Chandler uses the AgendaQuick platform at ``public.destinyhosted.com``.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

from scraper.io_utils import _normalize_text_date

log = logging.getLogger(__name__)

JURISDICTION_ID = 3
PUBLIC_BODY_CODE = "chandler-cc"
SOURCE_SYSTEM = "agendaquick"
SOURCE_INSTANCE_URL = "https://public.destinyhosted.com"

BASE_URL = "https://public.destinyhosted.com"
CHANDLER_ID = "24263"

BODY_MAP: dict[str, tuple[str, str]] = {
    "city council": ("chandler-city-council", "chandler-cc"),
    "planning and zoning commission": ("chandler-planning-zoning-commission", "chandler-pz"),
    "development review commission": ("chandler-development-review-commission", "chandler-drc"),
    "development review": ("chandler-development-review-commission", "chandler-drc"),
    "board of adjustment": ("chandler-board-of-adjustment", "chandler-boa"),
    "historic preservation commission": ("chandler-historic-preservation-commission", "chandler-hpc"),
    "industrial development authority": ("chandler-ida", "chandler-ida"),
    "parks and recreation board": ("chandler-parks-recreation-board", "chandler-prb"),
    "parks and recreation": ("chandler-parks-recreation-board", "chandler-prb"),
    "library board": ("chandler-library-board", "chandler-lb"),
    "museum foundation": ("chandler-museum-foundation", "chandler-mf"),
    "cultural foundation": ("chandler-cultural-foundation", "chandler-cf"),
    "arts commission": ("chandler-arts-commission", "chandler-arts"),
    "transportation commission": ("chandler-transportation-commission", "chandler-tc"),
    "military and veterans affairs": ("chandler-military-veterans-commission", "chandler-mvc"),
    "housing and human services commission": ("chandler-housing-human-services-commission", "chandler-hhsc"),
    "human relations commission": ("chandler-human-relations-commission", "chandler-hrc"),
    "domestic violence commission": ("chandler-domestic-violence-commission", "chandler-dvc"),
    "public housing authority": ("chandler-public-housing-authority", "chandler-pha"),
    "neighborhood advisory committee": ("chandler-neighborhood-advisory-committee", "chandler-nac"),
    "mayor's youth commission": ("chandler-youth-commission", "chandler-yc"),
    "mayor's committee for people with disabilities": ("chandler-peoples-disabilities-committee", "chandler-pdc"),
    "economic development advisory": ("chandler-economic-development-advisory", "chandler-eda"),
    "psprs board fire": ("chandler-psprs-fire-board", "chandler-psprs-f"),
    "psprs board police": ("chandler-psprs-police-board", "chandler-psprs-p"),
    "housing and community services corporation": ("chandler-housing-corporation", "chandler-hcc"),
    "citizens' panel review": ("chandler-citizens-panel-review", "chandler-cpr"),
    "health care benefits trust": ("chandler-health-care-trust", "chandler-hct"),
    "workers' compensation": ("chandler-workers-comp-trust", "chandler-wct"),
    "airport commission": ("chandler-airport-commission", "chandler-air"),
}
DEFAULT_BODY_SLUGS = ["chandler-city-council"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    key = body_name.lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "chandler-city-council", "chandler-cc"


def extract_meeting_type(body_name: str) -> str:
    """Extract the meeting type from the body_name.

    Chandler's AgendaQuick page stores the meeting title in a format like
    "City Council Regular Meeting" or "Parks and Recreation Board Study Session".
    This function strips the body name part to get just the type.
    """
    tl = body_name.lower()
    # Cancel prefixes → "Cancelled"
    if "cancellation" in tl or "canceled" in tl or "cancelled" in tl:
        return "Cancelled"
    # Quorum notices → "Quorum Notice"
    if "quorum notice" in tl or "quorum notices" in tl:
        return "Quorum Notice"
    # Known meeting-type suffixes
    if "study session" in tl:
        return "Study Session"
    if "work session" in tl:
        return "Work Session"
    if "executive session" in tl or tl.endswith("executive  session"):
        return "Executive Session"
    if "executive" in tl:
        return "Executive Session"
    if "special meeting" in tl:
        return "Special Meeting"
    if "special" in tl:
        return "Special"
    if "regular meeting" in tl:
        return "Regular Meeting"
    return "Regular Meeting"  # default


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
    return f"{BASE_URL}/agenda_publish.cfm?id={CHANDLER_ID}&mt=ALL&get_month={month}&get_year={year}"


# ── Meeting list parsing ──

def parse_meetings(html: str) -> list[dict]:
    """Parse Chandler month-view table."""
    meetings: list[dict] = []
    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    for tr_match in row_re.finditer(html):
        tr = tr_match.group()
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
        if len(tds) < 2:
            continue
        # Extract date link from first td
        dm = re.search(r'href="([^"]*)"[^>]*>([^<]+)</a>', tds[0])
        if not dm:
            continue
        href = dm.group(1).replace("&amp;", "&")
        date = dm.group(2).strip()
        # Extract seq from href
        sm = re.search(r"seq=(\d+)", href)
        seq = sm.group(1) if sm else ""
        # Body name from second td
        body_name = re.sub(r"<[^>]+>", "", tds[1]).strip()
        # Results/video from third td
        results_url = ""
        video_url = ""
        if len(tds) >= 3:
            rm = re.search(r'href="([^"]*\.pdf)"', tds[2])
            if rm:
                results_url = urllib.parse.urljoin(BASE_URL, rm.group(1))
            vm = re.search(r'href="(https?://[^"]*swagit[^"]*)"', tds[2])
            if vm:
                video_url = vm.group(1)
            if not video_url:
                vm2 = re.search(r'href="([^"]*)"[^>]*>\s*Video\s*<', tds[2])
                if vm2:
                    video_url = urllib.parse.urljoin(BASE_URL, vm2.group(1))
        slug, code = _resolve_body(body_name)
        meetings.append({
            "meeting_date": _normalize_text_date(date) or date,
            "body_name": body_name,
            "body_slug": slug,
            "body_code": code,
            "meeting_type": extract_meeting_type(body_name),
            "meeting_id": seq,
            "meeting_seq": seq,
            "agenda_url": urllib.parse.urljoin(BASE_URL, href),
            "results_url": results_url,
            "video_url": video_url,
        })
    return meetings


def search_chandler_meetings(year: int, body_slugs: Optional[list[str]] = None) -> list[dict]:
    """Search Chandler meetings for a given year, month by month."""
    if body_slugs is None:
        all_m: list[dict] = []
        for m in range(1, 13):
            try:
                html = fetch_page(build_month_url(year, m), timeout=15)
                all_m.extend(parse_meetings(html))
            except Exception as e:
                log.warning("Chandler %d-%02d failed: %s", year, m, e)
        return all_m
    all_m: list[dict] = []
    for m in range(1, 13):
        try:
            html = fetch_page(build_month_url(year, m), timeout=15)
            all_m.extend(parse_meetings(html))
        except Exception as e:
            log.warning("Chandler %d-%02d failed: %s", year, m, e)
    filtered = [m for m in all_m if m["body_slug"] in body_slugs]
    log.info("Chandler %d: %d meetings (%d total)", year, len(filtered), len(all_m))
    return filtered


# ── Agenda item parsing ──

def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse agenda items from a Chandler agenda detail page."""
    items: list[dict] = []
    sort_order = 0

    # Find section headers
    section_map: list[tuple[int, str]] = []
    for sm in re.finditer(
        r'<(?:h[12]|strong|b)[^>]*>\s*'
        r'(CONSENT AGENDA|CALL TO ORDER|UNSCHEDULED PUBLIC APPEARANCES?|'
        r'CURRENT EVENTS|SERVICE RECOGNITIONS?|ADJOURN(?:MENT)?'
        r'|REGULAR MEETING AGENDA|STUDY SESSION AGENDA)\s*</',
        html, re.IGNORECASE,
    ):
        label = sm.group(1).upper()
        if "CONSENT" in label:
            sec = "Consent"
        elif "UNSCHEDULED" in label:
            sec = "Public Appearances"
        elif "CURRENT" in label:
            sec = "Current Events"
        elif "CALL" in label:
            sec = "Call to Order"
        elif "SERVICE" in label:
            sec = "Service Recognitions"
        elif "ADJOURN" in label:
            sec = "Adjournment"
        else:
            sec = label.title()
        section_map.append((sm.start(), sec))

    def _section(pos: int) -> str:
        best = ""
        for sp, sn in section_map:
            if sp <= pos:
                best = sn
        return best

    # Item rows: <td>N.</td> then 3 more tds then title td
    item_re = re.compile(
        r'<td[^>]*>\s*(\d[\w.-]*)\s*\.\s*</td>.*?'
        r'<td[^>]*class="mediumText"[^>]*>(.*?)</td>',
        re.DOTALL,
    )

    for m in item_re.finditer(html):
        item_number = m.group(1)
        content_html = m.group(2)
        title = re.sub(r"<[^>]+>", " ", content_html)
        title = re.sub(r"\s+", " ", title).replace("&nbsp;", " ").strip()
        section = _section(m.start())
        sort_order += 1

        # Find motion text in the next ~2000 chars
        motion_text = ""
        after = html[m.end():m.end() + 3000]
        motion_m = re.search(
            r"Move\s+(?:City Council|Commission|Board)\s+",
            after,
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
            "agenda_category": section,
            "sort_order": sort_order,
        })
    return items


# ── Results PDF vote parsing ──

def fetch_results_pdf_bytes(results_url: str) -> Optional[bytes]:
    """Download a Chandler Results PDF."""
    import urllib.request
    try:
        req = urllib.request.Request(results_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Results PDF not available for %s: %s", results_url, e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a PDF using pdftotext."""
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
    """Parse Chandler Results PDF for roll-call votes.

    Chandler Results PDFs come in two formats:

    Format A (old style):
      MOTION: Moved by Councilmember X, seconded by Councilmember Y
      to approve item N.
      AYES: (7) - Hartke, Ellis, ...
      NAYES: (0) - None
      ABSENT: (0)

    Format B (new style):
      Consent Agenda items 1-9 passed unanimously, 6-0,
                               Mayor Hartke absent excused.
      or
      Item N passed, 5-2, Councilmember Smith and Councilmember Jones dissenting.

    Returns dict with keys: supervisors (list), votes (list).
    """
    _CHANDLER_NAME_MAP = {
        "hartke": "Kevin Hartke", "encinas": "Angel Encinas", "ellis": "Christine Ellis",
        "orlando": "Matt Orlando", "harris": "OD Harris", "poston": "Jane Poston",
        "hawkins": "Jennifer Hawkins",
        # Historical members (from older Results PDFs)
        "orlik": "Matt Orlando", "cook": "Jane Poston", "pike": "Rene Pike",
        "santos": "OD Harris", "miranda": "Jane Poston", "quinn": "John Quinn",
        "dunn": "Jeremy Dunn",
    }
    _COUNCIL_NAMES = set(_CHANDLER_NAME_MAP.keys())

    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()

    # ── Format B: simpler "passed unanimously, X-Y" style ──
    lines = text.splitlines()
    vote_count_re = re.compile(r"(\d+)-(\d+)")

    # Find vote lines
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        lower = line.lower()

        # Detect vote results
        if "passed" in lower or "carried" in lower or "failed" in lower or "denied" in lower:
            # Extract vote count e.g. "6-0", "5-2"
            # Find the vote count (the last N-N pattern, not item numbers)
            all_vc = list(vote_count_re.finditer(line))
            vc = all_vc[-1] if len(all_vc) > 1 else (all_vc[0] if all_vc else None)
            # Check for "unanimous" which means 0 nays regardless of numbers
            ayes_count = int(vc.group(1)) if vc else 0
            nays_count = int(vc.group(2)) if vc else 0
            if "unanimously" in lower or "unanimous" in lower:
                nays_count = 0
                ayes_count = max(ayes_count, nays_count + 1)

            # Extract item numbers from "items N-M" or "Item N" pattern
            item_nums = ""
            im = re.search(r"items?\s+(\d[\d,\s-]*)", lower)
            if im:
                item_nums = im.group(1).strip()

            # Extract absent members
            absent_line = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if "absent" in next_line.lower() or "excused" in next_line.lower():
                    absent_line = next_line
                    # Extract names
                    for name_match in re.finditer(r"(Mayor|Councilmember|Member)\s+([A-Z][a-zA-Z]+)", absent_line):
                        n = name_match.group(2)
                        if n.lower() not in seen_sup:
                            seen_sup.add(n.lower())
                            supervisors.append({
                                "name": n, "normalized_name": n.lower(),
                                "present": False,
                            })
            result = "Carried Unanimously" if nays_count == 0 else ("Carried" if ayes_count > nays_count else "Failed")

            # Determine item numbers for agenda_item_number
            if not item_nums:
                # Look for "Item N" or "Agenda Item N" pattern earlier in the line
                im2 = re.search(r"Item[s]?\s+(\d+)", line)
                if im2:
                    item_nums = im2.group(1)

            votes.append({
                "agenda_item_number": item_nums,
                "ayes": [f"Member_{x+1}" for x in range(ayes_count)],
                "nays": [f"Member_{x+1}" for x in range(nays_count)],
                "motion_result": result,
                "supervisor_votes": [],
                "vote_text": line.strip() + (" " + absent_line if absent_line else ""),
            })

        # Check next line
        i += 1

    if votes:
        return {"supervisors": supervisors, "votes": votes}

    # ── Format A: detailed motion/AYES/NAYS/ABSENT style ──
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("MOTION:") or line.startswith("Moved by"):
            desc = line
            j = i + 1
            while j < len(lines) and "AYES:" not in lines[j] and "NAYES:" not in lines[j]:
                upper = lines[j].strip().upper()
                if any(upper.startswith(w) for w in ["APPROVED", "DENIED", "ADOPTED", "FAILED", "CARRIED", "TABLED"]):
                    desc += " " + lines[j].strip()
                j += 1

            ayes_names: list[str] = []
            nays_names: list[str] = []
            absent_names: list[str] = []
            result = ""

            while j < len(lines):
                lj = lines[j].strip()
                if lj.startswith("AYES:"):
                    name_part = lj.split("-", 1)[-1] if "-" in lj else ""
                    ayes_names = [n.strip() for n in name_part.split(",") if n.strip() and n.strip() not in ("None", "")]
                    if j + 1 < len(lines) and not lines[j+1].strip().startswith(("NAYES:", "ABSENT:", "MOTION:")):
                        ayes_names.extend([n.strip() for n in lines[j+1].split(",") if n.strip()])
                elif lj.startswith("NAYES:") or lj.startswith("NAYS:"):
                    name_part = lj.split("-", 1)[-1] if "-" in lj else ""
                    nays_names = [n.strip() for n in name_part.split(",") if n.strip() and n.strip() not in ("None", "")]
                elif lj.startswith("ABSENT:"):
                    name_part = lj.split("-", 1)[-1] if "-" in lj else ""
                    absent_names = [n.strip() for n in name_part.split(",") if n.strip() and n.strip() not in ("None", "")]
                elif any(lj.upper().startswith(w) for w in ["APPROVED", "DENIED", "ADOPTED", "FAILED", "CARRIED", "TABLED"]):
                    result = lj
                elif lj.startswith("MOTION:") or lj.startswith("Moved by"):
                    break
                j += 1

            if ayes_names or nays_names:
                all_member_names = set(a.lower() for a in ayes_names + nays_names + absent_names)
                for name in all_member_names:
                    if name not in seen_sup:
                        seen_sup.add(name)
                        full = _CHANDLER_NAME_MAP.get(name, name.title())
                        supervisors.append({
                            "name": full,
                            "normalized_name": name,
                            "present": name not in [a.lower() for a in absent_names],
                        })
                sup_votes = []
                for name in ayes_names:
                    sup_votes.append({"name": name, "vote": "yes", "raw_vote_text": name})
                for name in nays_names:
                    sup_votes.append({"name": name, "vote": "no", "raw_vote_text": name})
                item_num = ""
                for k in range(max(0, i - 5), i):
                    im = re.search(r"(\d[\w.-]*)\.", lines[k])
                    if im:
                        item_num = im.group(1)
                        break
                votes.append({
                    "agenda_item_number": item_num,
                    "ayes": ayes_names,
                    "nays": nays_names,
                    "motion_result": result or "Carried",
                    "supervisor_votes": sup_votes,
                    "vote_text": f"Ayes: {', '.join(ayes_names)}; Nays: {', '.join(nays_names) if nays_names else 'None'}",
                })
            i = j
        else:
            i += 1

    return {"supervisors": supervisors, "votes": votes}


# ── Attachments (dsp=atf) endpoint for finding minutes PDFs ──

CHANDLER_ORG_ID = 24263


def build_attachments_url(meeting_seq: str, meeting_date: str) -> str:
    """Build the URL for the attachments page of a meeting.

    The attachments page (dsp=atf) lists all PDFs attached to the meeting,
    including meeting minutes, results, and other supporting documents.

    The `ag` parameter is the meeting seq. Month and year are derived from
    the meeting date.
    """
    parts = meeting_date.split("-")
    year = parts[0] if len(parts) >= 1 else "2026"
    month = parts[1] if len(parts) >= 2 else "01"
    return (
        f"{BASE_URL}/agenda_publish.cfm?id={CHANDLER_ORG_ID}"
        f"&mt=ALL&get_month={month}&get_year={year}"
        f"&dsp=atf&ag={meeting_seq}"
    )


def fetch_attachments_page(attachments_url: str) -> Optional[str]:
    """Fetch the attachments (dsp=atf) page HTML."""
    try:
        return fetch_page(attachments_url, timeout=20)
    except Exception as e:
        log.debug("Attachments page not available for %s: %s", attachments_url, e)
        return None


def parse_attachments_for_minutes(html: str, meeting_seq: str = "") -> list[str]:
    """Extract minutes PDF URLs from an attachments page.

    Looks for PDF links that contain "minutes" or "minute" in the filename,
    and whose URL path includes the meeting_seq (to filter out other bodies).

    Returns list of absolute PDF URLs.
    """
    import urllib.parse
    pdfs: list[str] = []
    for m in re.finditer(r'href="([^"]*\.pdf)"', html, re.I):
        url = m.group(1)
        # Decode HTML entities
        url = url.replace("&#x25;20", " ").replace("&amp;", "&")
        filename = url.rsplit("/", 1)[-1].lower() if "/" in url else url.lower()
        # Skip non-minutes/results PDFs
        if not ("minutes" in filename or "minute" in filename or "result" in filename or "meeting" in filename):
            continue
        # Filter by meeting_seq in the URL path to avoid other bodies' PDFs
        if meeting_seq and f"/{meeting_seq}_" not in url and f"_{meeting_seq}" not in url:
            continue
        full_url = urllib.parse.urljoin(BASE_URL, url)
        if full_url not in pdfs:
            pdfs.append(full_url)
    return pdfs


# ── Chandler Minutes PDF vote parsing ──

def fetch_minutes_pdf_bytes(pdf_url: str) -> Optional[bytes]:
    """Download a Chandler meeting minutes PDF."""
    import urllib.request
    try:
        req = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Minutes PDF not available for %s: %s", pdf_url, e)
        return None


def parse_minutes_votes(text: str) -> dict:
    """Parse Chandler meeting minutes text for vote data.

    Chandler's minutes don't record individual roll-call votes, but they DO
    record attendance (Roll Call section) and dissenting votes by name.

    Key patterns:

      "Motion carried unanimously (7-0)"
      "Motion carried by majority (4-3; Councilmembers Encinas, Orlando, and Poston dissenting)"
      "Motion carried by majority (2-5; Mayor Hartke, Councilmembers ... dissenting)"

    From this we can infer:
    - Who was present from the Roll Call section
    - Who dissented on split votes
    - By subtraction: who was in the majority

    Returns dict with keys:
      - supervisors: all present members with normalized_name
      - votes: list of dicts {agenda_item_number, motion_result, supervisor_votes}
        where supervisor_votes contains {name, vote: "yes"/"no", raw_vote_text}
    """
    votes: list[dict] = []
    seen_voters: set[str] = set()
    # Track ALL present members from Roll Call so we can infer majority votes
    present_members: list[str] = []  # full names like "Kevin Hartke"

    lines = text.split("\n")

    # Known Chandler council name parts for fuzzy matching
    _COUNCIL_NAMES = {
        "hartke": ("Kevin Hartke", "Mayor"),
        "harris": ("OD Harris", "Vice Mayor"),
        "od harris": ("OD Harris", "Vice Mayor"),
        "encinas": ("Angel Encinas", "Councilmember"),
        "ellis": ("Christine Ellis", "Councilmember"),
        "stewart": ("Mark Stewart", "Councilmember"),
        "orlando": ("Matt Orlando", "Councilmember"),
        "poston": ("Jane Poston", "Councilmember"),
        "jones": ("Rene Lopez", "Councilmember"),
    }

    def _resolve_name(raw: str) -> str:
        raw = raw.strip().rstrip(".,; ").lower().lstrip()
        # Direct match
        if raw in _COUNCIL_NAMES:
            return _COUNCIL_NAMES[raw][0]
        # Try partial match: check if raw starts with any key
        for key, (full, _) in _COUNCIL_NAMES.items():
            if key.startswith(raw) or raw.startswith(key):
                return full
        return raw.title()

    # Parse Roll Call section to determine who was present
    in_roll_call = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Roll Call"):
            in_roll_call = True
            continue
        if not in_roll_call:
            continue
        # Stop at first blank line or next section header
        if not stripped or any(kw in stripped.upper() for kw in ["INVOCATION", "PLEDGE", "CONSENT"]):
            in_roll_call = False
            continue
        # Match "Mayor Name" or "Vice Mayor Name" or "Councilmember Name"
        # The line may also contain appointee info after extra spaces (e.g.
        # "Mayor Kevin Hartke                    Joshua Wright, City Manager")
        # We extract just the council member name before the extra spaces.
        m = re.match(
            r"(?:Mayor|Vice Mayor|Councilmember)\s+([A-Z][A-Za-z.]*(?:\s+[A-Z][a-z]+)?)",
            stripped,
        )
        if not m:
            # Try the appointee-only row format (no council member here)
            if any(kw in stripped.lower() for kw in
                   ["joshua wright", "kelly schwab", "dana delong"]):
                continue
        if m:
            name_raw = m.group(1)
            full = _resolve_name(name_raw)
            if full not in present_members:
                present_members.append(full)

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Pattern: Motion carried by majority (N-M; Councilmember X, Y, and Z dissenting)
        dissenting_match = re.search(
            r"Motion carried by majority\s*\((\d+)-(\d+);\s*(.*?)dissenting\)",
            stripped, re.I,
        )
        if dissenting_match:
            ayes = int(dissenting_match.group(1))
            nays = int(dissenting_match.group(2))
            dissenters_raw = dissenting_match.group(3)
            # Extract individual dissenter names.
            # Format: "Councilmembers Encinas, Orlando, and Poston"
            # or: "Mayor Hartke, Councilmembers Encinas, Ellis, Orlando, and Poston"
            # Strip the "Councilmembers" / "Councilmember" / "Mayor" / "Vice Mayor" prefix
            dissenters = []
            cleaned = dissenters_raw.strip()
            # Strip all role prefixes (may appear before each name group)
            cleaned = re.sub(
                r"(?:\s*Councilmembers?|\s*Mayor|\s*Vice Mayor)\s+", " ", cleaned
            )
            # Split on commas and "and", strip whitespace and trailing periods
            parts = re.split(r"[,\s]+and\s+|[,\s]+", cleaned)
            for part in parts:
                p = part.strip().strip(".;,")
                if p and p[0].isupper() and len(p) > 2 and p.lower() not in ("dissenting", "and"):
                    if p not in dissenters:
                        dissenters.append(p)

            supervisor_votes = []
            dissenter_norms = set()
            for name in dissenters:
                full = _resolve_name(name)
                norm = full.lower()
                dissenter_norms.add(norm)
                seen_voters.add(norm)
                supervisor_votes.append({
                    "name": full, "vote": "no",
                    "raw_vote_text": name.strip(),
                })

            # Infer majority: present members minus dissenters
            for full in present_members:
                norm = full.lower()
                if norm not in dissenter_norms:
                    seen_voters.add(norm)
                    supervisor_votes.append({
                        "name": full, "vote": "yes",
                        "raw_vote_text": full,
                    })

            idx = len(votes) + 1
            result = "Carried" if ayes > nays else "Failed"
            votes.append({
                "agenda_item_number": f"minutes-{idx}",
                "motion_result": result,
                "vote_text": f"{ayes}-{nays} ({', '.join(d.strip() for d in dissenters)} dissenting)",
                "supervisor_votes": supervisor_votes,
            })
            continue

        # Pattern: Motion carried unanimously (N-0)
        unanimous_match = re.search(
            r"Motion carried unanimously\s*\((\d+)-0\)",
            stripped, re.I,
        )
        if unanimous_match:
            idx = len(votes) + 1
            votes.append({
                "agenda_item_number": f"minutes-{idx}",
                "motion_result": "Carried Unanimously",
                "vote_text": f"Unanimous ({unanimous_match.group(1)}-0)",
                "supervisor_votes": [],
            })
            continue

        # Pattern: passed N-0 with exception (conflict of interest)
        exception_match = re.search(
            r"passed\s+(\d+)-0,\s*(.*?)(?:Councilmember|Mayor|Vice Mayor)\s+([A-Z][a-zA-Z]+).*?(?:conflict|excused|absent)",
            stripped, re.I,
        )
        if exception_match:
            idx = len(votes) + 1
            votes.append({
                "agenda_item_number": f"minutes-{idx}",
                "motion_result": "Carried",
                "vote_text": stripped.strip(),
                "supervisor_votes": [],
            })
            continue

    # Build supervisor list from present members (roll call)
    # Fall back to seen_voters if roll call parsing produced nothing
    source_names = present_members if present_members else list(seen_voters)
    supervisors = [
        {
            "name": name,
            "normalized_name": name.lower(),
            "present": True,
        }
        for name in source_names
    ]

    return {"supervisors": supervisors, "votes": votes}
