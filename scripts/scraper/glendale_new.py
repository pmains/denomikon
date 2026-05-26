"""
City of Glendale meeting and agenda extraction via AgendaQuick (Destiny Software).

Glendale uses the AgendaQuick platform at ``public.destinyhosted.com`` —
the same system as Chandler (``scripts/scraper/chandler.py``).

The meeting list table (4 columns: date, meeting name, empty minutes, links)
and the agenda detail page (7-column table) share the same AgendaQuick DNA
but differ in detail from Chandler's.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

from scraper.io_utils import _normalize_text_date

log = logging.getLogger(__name__)

JURISDICTION_ID = 9
PUBLIC_BODY_CODE = "glendale-cc"
SOURCE_SYSTEM = "agendaquick"
SOURCE_INSTANCE_URL = "https://public.destinyhosted.com"

BASE_URL = "https://public.destinyhosted.com"
GLENDALE_ID = "45363"

# Body map: maps body-name keywords → (slug, code).
# The mt=CODE parameter filters by body on the AgendaQuick calendar page.
# Codes are from the <select name="mt"> on the Glendale AgendaQuick page.
BODY_MAP: dict[str, tuple[str, str]] = {
    "city council": ("glendale-city-council", "glendale-cc"),
    "planning commission": ("glendale-planning-commission", "glendale-pc"),
    "board of adjustment": ("glendale-board-of-adjustment", "glendale-boa"),
    "historic preservation commission": ("glendale-historic-preservation-commission", "glendale-hpc"),
    "historic preservation": ("glendale-historic-preservation-commission", "glendale-hpc"),
    "parks and recreation advisory commission": ("glendale-parks-recreation-advisory", "glendale-parac"),
    "parks and recreation": ("glendale-parks-recreation-advisory", "glendale-parac"),
    "arts commission": ("glendale-arts-commission", "glendale-ac"),
    "airport commission": ("glendale-airport-advisory", "glendale-avac"),
    "aviation advisory": ("glendale-airport-advisory", "glendale-avac"),
    "aviation": ("glendale-airport-advisory", "glendale-avac"),
    "library advisory board": ("glendale-library-advisory-board", "glendale-lab"),
    "library": ("glendale-library-advisory-board", "glendale-lab"),
    "commission on community and culture": ("glendale-community-culture", "glendale-cocc"),
    "community and culture": ("glendale-community-culture", "glendale-cocc"),
    "citizens transportation oversight": ("glendale-transportation-oversight", "glendale-citoc"),
    "transportation oversight": ("glendale-transportation-oversight", "glendale-citoc"),
    "citizens utility advisory": ("glendale-utility-advisory", "glendale-ciuac"),
    "utility advisory": ("glendale-utility-advisory", "glendale-ciuac"),
    "citizen bicycle": ("glendale-bicycle-advisory", "glendale-cbac"),
    "citizens active transportation": ("glendale-active-transportation", "glendale-catac"),
    "active transportation": ("glendale-active-transportation", "glendale-catac"),
    "human relations commission": ("glendale-human-relations", "glendale-hrc"),
    "human relations": ("glendale-human-relations", "glendale-hrc"),
    "public safety personnel retirement": ("glendale-psprs", "glendale-psprb"),
    "psprs": ("glendale-psprs", "glendale-psprb"),
    "industrial development authority": ("glendale-ida", "glendale-ida"),
    "ida": ("glendale-ida", "glendale-ida"),
    "bond committee": ("glendale-bond-committee", "glendale-bc"),
    "bond": ("glendale-bond-committee", "glendale-bc"),
    "risk management trust": ("glendale-risk-management", "glendale-rmtfb"),
    "workers' compensation trust": ("glendale-workers-comp", "glendale-wctfb"),
    "workers compensation": ("glendale-workers-comp", "glendale-wctfb"),
    "personnel board": ("glendale-personnel-board", "glendale-pb"),
    "commission on persons with disabilities": ("glendale-persons-disabilities", "glendale-cmpd"),
    "persons with disabilities": ("glendale-persons-disabilities", "glendale-cmpd"),
    "community development advisory": ("glendale-community-development-advisory", "glendale-codac"),
    "government services committee": ("glendale-government-services", "glendale-gsc"),
    "commission on diverse cultures": ("glendale-diverse-cultures", "glendale-cdc"),
    "council compensation": ("glendale-council-compensation", "glendale-ccc"),
    "business council": ("glendale-business-council", "glendale-bcc"),
    "municipal property corporation": ("glendale-municipal-property", "glendale-mpc"),
    "public notices": ("glendale-public-notices", "glendale-pub"),
    "code review": ("glendale-code-review", "glendale-ccr"),
    "abatement hearing": ("glendale-abatement-hearing", "glendale-ah"),
    "judicial selection": ("glendale-judicial-selection", "glendale-jsab"),
    "west valley": ("glendale-west-valley-dv", "glendale-wvdv"),
}
DEFAULT_BODY_SLUGS = ["glendale-city-council"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _resolve_body(body_name: str) -> tuple[str, str]:
    """Match a body-name string to (slug, code)."""
    key = body_name.lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "glendale-city-council", "glendale-cc"


def extract_meeting_type(body_name: str) -> str:
    """Extract meeting type from the body_name string.

    Recognises suffixes like 'Regular', 'Workshop', 'Study Session',
    'Special Meeting', 'Cancelled', 'Quorum Notice', etc.
    """
    tl = body_name.lower()
    # Cancellation / vacated
    if "cancellation" in tl or "canceled" in tl or "cancelled" in tl or "vacated" in tl:
        return "Cancelled"
    # Quorum notices
    if "quorum" in tl:
        return "Quorum Notice"
    # Known meeting-type patterns in body_name (the name often includes the type,
    # e.g. "City Council Regular", "Second Amended City Council Workshop",
    # "Special City Council Meeting")
    if "study session" in tl:
        return "Study Session"
    if "work session" in tl or "workshop" in tl:
        return "Workshop"
    if "executive session" in tl:
        return "Executive Session"
    if "special meeting" in tl or tl.startswith("special "):
        return "Special Meeting"
    if "regular" in tl:
        return "Regular Meeting"
    if "upcoming agenda" in tl:
        return "Upcoming Agenda Items"
    return "Regular Meeting"


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return decoded HTML."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def build_month_url(year: int, month: int, mt: str = "ALL") -> str:
    """Build the AgendaQuick month-view URL for Glendale.

    Args:
        year: Four-digit year.
        month: Month number (1-12).
        mt: Meeting-type code filter (default "ALL").
    """
    return (
        f"{BASE_URL}/agenda_publish.cfm"
        f"?id={GLENDALE_ID}&mt={mt}"
        f"&get_month={month}&get_year={year}"
    )


# ── Meeting list parsing ──

def parse_meetings(html: str) -> list[dict]:
    """Parse the Glendale AgendaQuick month-view table.

    The table has 4 columns:
      0: Agenda link (date + link with seq=NUM)
      1: Meeting name (body name)
      2: Minutes / Results column (usually empty for Glendale)
      3: Other Links (Audio / Video)
    """
    meetings: list[dict] = []
    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    for tr_match in row_re.finditer(html):
        tr = tr_match.group()
        # Skip header rows (thead/tfoot)
        if "<td" not in tr or "id=\"meeting-table\"" in tr:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
        if len(tds) < 2:
            continue

        # Column 0: Agenda link with date
        dm = re.search(r'href="([^"]*)"[^>]*>([^<]+)</a>', tds[0])
        if not dm:
            continue
        href = dm.group(1).replace("&amp;", "&")
        date = dm.group(2).strip()
        # Extract seq from href
        sm = re.search(r"seq=(\d+)", href)
        seq = sm.group(1) if sm else ""

        # Column 1: Body name
        body_name = re.sub(r"<[^>]+>", "", tds[1]).strip()
        body_name = body_name.replace("\u00a0", " ").strip()

        # Column 2: Minutes/Results (usually empty for Glendale)
        results_url = ""
        if len(tds) >= 3:
            rm = re.search(r'href="([^"]*\.pdf)"', tds[2])
            if rm:
                results_url = urllib.parse.urljoin(BASE_URL, rm.group(1))

        # Column 4 (tds[3]): Other Links (Video/Audio)
        video_url = ""
        audio_url = ""
        if len(tds) >= 4:
            # Check for Swagit video links first
            vm = re.search(r'href="(https?://[^"]*swagit[^"]*)"', tds[3])
            if vm:
                video_url = vm.group(1)
            if not video_url:
                vm2 = re.search(r'href="([^"]*)"[^>]*>\s*Video\s*<', tds[3])
                if vm2:
                    video_url = urllib.parse.urljoin(BASE_URL, vm2.group(1))
            # Check for Audio links
            if not video_url:
                am = re.search(r'href="([^"]*)"[^>]*>\s*Audio\s*<', tds[3])
                if am:
                    audio_url = am.group(1)

        slug, code = _resolve_body(body_name)
        
        # Skip cancellation / vacated notices — they are not real meetings
        _type = extract_meeting_type(body_name)
        if _type == "Cancelled":
            log.debug("Skipping cancelled meeting: %s %s", date, body_name[:60])
            continue
        
        meetings.append({
            "meeting_date": _normalize_text_date(date) or date,
            "body_name": body_name,
            "body_slug": slug,
            "body_code": code,
            "meeting_type": _type,
            "meeting_id": seq,
            "meeting_seq": seq,
            "agenda_url": urllib.parse.urljoin(BASE_URL, href),
            "results_url": results_url,
            "video_url": video_url or audio_url,
        })
    return meetings


def search_glendale_meetings(year: int, body_slugs: Optional[list[str]] = None) -> list[dict]:
    """Search Glendale meetings for a given year, month by month."""
    if body_slugs is None:
        all_m: list[dict] = []
        for m in range(1, 13):
            try:
                html = fetch_page(build_month_url(year, m), timeout=15)
                all_m.extend(parse_meetings(html))
            except Exception as e:
                log.warning("Glendale %d-%02d failed: %s", year, m, e)
        return all_m
    all_m: list[dict] = []
    for m in range(1, 13):
        try:
            html = fetch_page(build_month_url(year, m), timeout=15)
            all_m.extend(parse_meetings(html))
        except Exception as e:
            log.warning("Glendale %d-%02d failed: %s", year, m, e)
    filtered = [m for m in all_m if m["body_slug"] in body_slugs]
    log.info("Glendale %d: %d meetings (%d total)", year, len(filtered), len(all_m))
    return filtered


# ── Agenda item parsing ──

def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse agenda items from a Glendale agenda detail page.

    Glendale's agenda detail page uses a 7-column table.  Each item row:
      <tr class="top">
        <td><a name="ReturnToN"></a></td>
        <td>N.</td>         (item number)
        <td class="tdempty"></td>
        <td class="tdempty"></td>
        <td class="tdempty"></td>
        <td colspan="2" class="mediumText"> TITLE </td>
      </tr>

    Section headers are <strong>LABEL</strong> inside a colspan="7" td.
    """
    items: list[dict] = []
    sort_order = 0

    # ── Section headers ──
    # Glendale uses <strong>LABEL</strong> inside a colspan=7 td
    section_map: list[tuple[int, str]] = []
    for sm in re.finditer(
        r"<strong>\s*"
        r"(CALL TO ORDER|ROLL CALL|PRAYER[^<]*INVOCATION|POSTING OF COLORS|"
        r"PLEDGE OF ALLEGIANCE|APPROVAL OF THE MINUTES|CONSENT AGENDA\s*|"
        r"CONSENT RESOLUTIONS|RESOLUTIONS|BOARDS[,\s]+COMMISSIONS|"
        r"COUNCIL COMMENTS|CITIZEN COMMENTS|PUBLIC HEARINGS\s*|"
        r"UNFINISHED BUSINESS|NEW BUSINESS|"
        r"COUNCIL COMMENTS AND SUGGESTIONS|ADJOURN(?:MENT)?)\s*"
        r"</strong>",
        html,
        re.IGNORECASE,
    ):
        label = sm.group(1).upper().strip()
        sec = _normalize_section(label)
        section_map.append((sm.start(), sec))

    def _section(pos: int) -> str:
        best = ""
        for sp, sn in section_map:
            if sp <= pos:
                best = sn
        return best

    # ── Item rows ──
    # Pattern: <td ...>N.</td> followed by the title in the 6th td (colspan="2")
    # Use a regex that captures the item number from the 2nd td, then finds
    # the title link in the 6th td (colspan="2" td with class containing mediumText).
    item_re = re.compile(
        r"<td[^>]*>\s*(\d[\w.-]*)\s*\.\s*</td>\s*"
        r"<td[^>]*class=\"mediumText tdempty\"[^>]*>\s*</td>\s*"
        r"<td[^>]*class=\"mediumText tdempty\"[^>]*>\s*</td>\s*"
        r"<td[^>]*class=\"tdempty\"[^>]*>.*?</td>\s*"
        r"<td[^>]*colspan=\"2\"[^>]*class=\"mediumText\"[^>]*>"
        r"\s*(.*?)\s*</td>",
        re.DOTALL,
    )

    for m in item_re.finditer(html):
        item_number = m.group(1)
        content_html = m.group(2)
        # Extract title text from the anchor or div tags
        title = re.sub(r"<[^>]+>", " ", content_html)
        title = re.sub(r"\s+", " ", title).replace("&nbsp;", " ").replace("&amp;", "&").strip()

        section = _section(m.start())
        sort_order += 1

        # Look for action/recommendation text near this item
        agenda_item_text = ""
        after = html[m.end():m.end() + 3000]
        # Check for motion or recommendation text
        motion_m = re.search(
            r"(Moved\s+(?:Councilmember|Mayor|Council|Member|Commissioner)\s+|"
            r"Recommended\s+(?:by|action)\s+|"
            r"Motion\s+(?:by|made)\s+)",
            after,
            re.IGNORECASE,
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
                else:
                    motion_text = re.sub(r"<[^>]+>", " ", after[ms:ms + 500])
            motion_text = re.sub(r"\s+", " ", motion_text).strip()
            if motion_text:
                agenda_item_text = motion_text

        items.append({
            "meeting_id": meeting_seq,
            "agenda_item_number": item_number,
            "agenda_item_title": title,
            "agenda_item_text": agenda_item_text,
            "item_type": "",
            "agenda_category": section,
            "sort_order": sort_order,
        })
    return items


def _normalize_section(label: str) -> str:
    """Normalise Glendale agenda section labels to canonical names."""
    if "CALL TO ORDER" in label:
        return "Call to Order"
    if "ROLL CALL" in label:
        return "Roll Call"
    if "PRAYER" in label or "INVOCATION" in label:
        return "Prayer/Invocation"
    if "POSTING" in label:
        return "Posting of Colors"
    if "PLEDGE" in label:
        return "Pledge of Allegiance"
    if "APPROVAL" in label and "MINUTES" in label:
        return "Approval of Minutes"
    if "CONSENT" in label and "RESOLUTION" in label:
        return "Consent Resolutions"
    if "CONSENT" in label:
        return "Consent Agenda"
    if "RESOLUTION" in label:
        return "Resolutions"
    if "BOARDS" in label or "COMMISSIONS" in label:
        return "Boards, Commissions & Other Bodies"
    if "COUNCIL COMMENTS" in label:
        return "Council Comments"
    if "CITIZEN COMMENTS" in label or "PUBLIC COMMENTS" in label:
        return "Citizen Comments"
    if "PUBLIC HEARING" in label:
        return "Public Hearings"
    if "UNFINISHED BUSINESS" in label:
        return "Unfinished Business"
    if "NEW BUSINESS" in label:
        return "New Business"
    if "ADJOURN" in label:
        return "Adjournment"
    return label.title()


# ── Results PDF / vote parsing ──

def fetch_results_pdf_bytes(results_url: str) -> Optional[bytes]:
    """Download a Glendale Results PDF."""
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
    """Parse Glendale Results PDF for roll-call votes.

    Glendale results PDFs follow the same format as Chandler's — both are
    AgendaQuick-generated.  Supports two formats:

    Format A (detailed):
      MOTION: Moved by Councilmember X, seconded by Councilmember Y
      to approve item N.
      AYES: (7) - Hartke, Ellis, ...
      NAYS: (0) - None
      ABSENT: (0)

    Format B (summary):
      Consent Agenda items 1-9 passed unanimously, 6-0,
      or
      Item N passed, 5-2, Councilmember Smith and Councilmember Jones dissenting.

    Returns dict with keys: supervisors (list), votes (list).
    """
    _GLENDALE_NAME_MAP = {
        "hutchens": "Jerry P. Hutchens",
        "weiers": "Lauren Tolmachoff",
        "tolmachoff": "Lauren Tolmachoff",
        "guzman": "Dianna T. Guzman",
        "chavira": "Ray M. Chavira",
        "cudny": "Gregory Cudny",
        "sundvold": "Lacey Sundvold",
    }
    _COUNCIL_NAMES = set(_GLENDALE_NAME_MAP.keys())

    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()

    lines = text.splitlines()
    vote_count_re = re.compile(r"(\d+)-(\d+)")

    # ── Format B: simpler "passed unanimously, X-Y" style ──
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        lower = line.lower()

        if "passed" in lower or "carried" in lower or "failed" in lower or "denied" in lower:
            all_vc = list(vote_count_re.finditer(line))
            vc = all_vc[-1] if len(all_vc) > 1 else (all_vc[0] if all_vc else None)
            ayes_count = int(vc.group(1)) if vc else 0
            nays_count = int(vc.group(2)) if vc else 0
            if "unanimously" in lower or "unanimous" in lower:
                nays_count = 0
                ayes_count = max(ayes_count, nays_count + 1)

            item_nums = ""
            im = re.search(r"items?\s+(\d[\d,\s-]*)", lower)
            if im:
                item_nums = im.group(1).strip()

            absent_line = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if "absent" in next_line.lower() or "excused" in next_line.lower():
                    absent_line = next_line
                    for name_match in re.finditer(
                        r"(Mayor|Councilmember|Member)\s+([A-Z][a-zA-Z]+)",
                        absent_line,
                    ):
                        n = name_match.group(2)
                        if n.lower() not in seen_sup:
                            seen_sup.add(n.lower())
                            supervisors.append({
                                "name": n,
                                "normalized_name": n.lower(),
                                "present": False,
                            })

            result = (
                "Carried Unanimously"
                if nays_count == 0
                else ("Carried" if ayes_count > nays_count else "Failed")
            )

            if not item_nums:
                im2 = re.search(r"Item[s]?\s+(\d+)", line)
                if im2:
                    item_nums = im2.group(1)

            votes.append({
                "agenda_item_number": item_nums,
                "ayes": [f"Member_{x+1}" for x in range(ayes_count)],
                "nays": [f"Member_{x+1}" for x in range(nays_count)],
                "motion_result": result,
                "supervisor_votes": [],
                "vote_text": line.strip()
                + (" " + absent_line if absent_line else ""),
            })

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
                if any(
                    upper.startswith(w)
                    for w in [
                        "APPROVED",
                        "DENIED",
                        "ADOPTED",
                        "FAILED",
                        "CARRIED",
                        "TABLED",
                    ]
                ):
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
                    ayes_names = [
                        n.strip()
                        for n in name_part.split(",")
                        if n.strip() and n.strip() not in ("None", "")
                    ]
                    if j + 1 < len(lines) and not lines[j + 1].strip().startswith(
                        ("NAYES:", "ABSENT:", "MOTION:")
                    ):
                        ayes_names.extend(
                            [
                                n.strip()
                                for n in lines[j + 1].split(",")
                                if n.strip()
                            ]
                        )
                elif lj.startswith("NAYES:") or lj.startswith("NAYS:"):
                    name_part = lj.split("-", 1)[-1] if "-" in lj else ""
                    nays_names = [
                        n.strip()
                        for n in name_part.split(",")
                        if n.strip() and n.strip() not in ("None", "")
                    ]
                elif lj.startswith("ABSENT:"):
                    name_part = lj.split("-", 1)[-1] if "-" in lj else ""
                    absent_names = [
                        n.strip()
                        for n in name_part.split(",")
                        if n.strip() and n.strip() not in ("None", "")
                    ]
                elif any(
                    lj.upper().startswith(w)
                    for w in [
                        "APPROVED",
                        "DENIED",
                        "ADOPTED",
                        "FAILED",
                        "CARRIED",
                        "TABLED",
                    ]
                ):
                    result = lj
                elif lj.startswith("MOTION:") or lj.startswith("Moved by"):
                    break
                j += 1

            if ayes_names or nays_names:
                all_member_names = set(
                    a.lower() for a in ayes_names + nays_names + absent_names
                )
                for name in all_member_names:
                    if name not in seen_sup:
                        seen_sup.add(name)
                        full = _GLENDALE_NAME_MAP.get(name, name.title())
                        supervisors.append({
                            "name": full,
                            "normalized_name": name,
                            "present": name
                            not in [a.lower() for a in absent_names],
                        })
                sup_votes = []
                for name in ayes_names:
                    sup_votes.append({
                        "name": name,
                        "vote": "yes",
                        "raw_vote_text": name,
                    })
                for name in nays_names:
                    sup_votes.append({
                        "name": name,
                        "vote": "no",
                        "raw_vote_text": name,
                    })
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
                    "vote_text": (
                        f"Ayes: {', '.join(ayes_names)}; "
                        f"Nays: {', '.join(nays_names) if nays_names else 'None'}"
                    ),
                })
            i = j
        else:
            i += 1

    return {"supervisors": supervisors, "votes": votes}


# ── CLI / Main entry point ──

def main():
    """CLI entry point for testing."""
    import argparse
    import json

    p = argparse.ArgumentParser(description="Glendale AgendaQuick Scraper")
    p.add_argument("--year", type=int, default=2026, help="Year to search")
    p.add_argument("--month", type=int, default=None, help="Single month (1-12)")
    p.add_argument("--limit", type=int, default=None, help="Max meetings to show")
    p.add_argument("--bodies", help="Comma-separated body slugs (default: glendale-city-council)")
    p.add_argument("--agenda", type=str, default=None, metavar="SEQ", help="Fetch agenda items for a meeting seq")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args()

    body_slugs = None
    if args.bodies:
        body_slugs = [s.strip() for s in args.bodies.split(",")]
    else:
        body_slugs = DEFAULT_BODY_SLUGS

    if args.agenda:
        # Fetch agenda detail page
        url = (
            f"{BASE_URL}/agenda_publish.cfm"
            f"?id={GLENDALE_ID}&mt=ALL"
            f"&get_month=5&get_year={args.year}"
            f"&dsp=ag&seq={args.agenda}"
        )
        html = fetch_page(url)
        items = parse_agenda_items(html, args.agenda)
        print(f"Found {len(items)} agenda items for seq={args.agenda}")
        for item in items:
            print(f"  {item['agenda_item_number']:>4s}. [{item['agenda_category']}] {item['agenda_item_title'][:120]}")
        return

    meetings = search_glendale_meetings(args.year, body_slugs)
    if args.month:
        meetings = [
            m for m in meetings
            if m["meeting_date"].startswith(f"{args.year}-{args.month:02d}")
        ]

    if args.limit:
        meetings = meetings[: args.limit]

    print(f"Found {len(meetings)} Glendale meetings in {args.year}")
    for m in meetings:
        print(f"  {m['meeting_date']} | {m['body_name']:45s} | seq={m['meeting_seq']} | type={m['meeting_type']}")
        if m.get("video_url"):
            print(f"    Video: {m['video_url']}")

    if args.json:
        print(json.dumps(meetings, indent=2, default=str))


if __name__ == "__main__":
    main()
