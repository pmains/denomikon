"""Parse Tempe City Council Legal Action Summary PDFs for vote data.

The summary PDF (doctype=3 in OnBase) records each agenda item's result
and, for items that received a roll-call vote, the motion maker, second,
vote tally, and individual supervisor names.

Example vote block in the PDF::

    PASS
    Motion to Approve Items 4B1 - 4B8 made by Councilmember Chin and seconded by
    Councilmember Keating
    Aye: 7; Nay: 0; Abstain: 0; Absent: 0; Recused: 0;
    For: Mayor Woods, Vice Mayor Garlid, Councilmember Adams, Councilmember
    Amberg, Councilmember Chin, Councilmember Hodge, Councilmember Keating

Individual item result lines follow each item's description::

    7B1. Approve the utilization ...
    APPROVED
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

# ── Result keywords (end-of-item action markers) ──

RESULT_KEYWORDS = frozenset({
    "APPROVED", "PASS", "ACCEPTED", "ADOPTED", "RATIFIED",
    "DENIED", "FAILED", "WITHDRAWN",
})

# ── Parsing ──


def _normalize_text(text: str) -> str:
    """Collapse whitespace and fix hyphenated line breaks."""
    text = re.sub(r"(\w)-\n+(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def find_items(text: str) -> list[dict]:
    """Find all agenda items in the summary text.

    Returns a list of dicts: {number, title, action, block_start, block_end}
    """
    # Split into lines for item boundary scanning
    lines = text.split("\n")
    items: list[dict] = []
    header_prefixes = {
        "Tempe City Council", "Harry E. Mitchell", "Tempe City Hall",
        "Virtual meeting", "REGULAR COUNCIL", "LEGAL ACTION", "MEETING VIDEO",
    }

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        if line in header_prefixes or line.startswith("Regular City Council Meeting Legal Action Summary"):
            continue
        if re.match(r"^\d+[A-Z]?\d*\.\s", line):
            m = re.match(r"^(\d+[A-Z]?\d*)\.\s+(.*)", line)
            if m:
                number = m.group(1)
                title_start = m.group(2).strip()
                items.append({
                    "number": number,
                    "title": title_start,
                    "line_idx": i,
                })

    return items


def find_vote_blocks(text: str) -> list[dict]:
    """Find all motion/vote blocks in the summary text and expand ranges.

    Returns list of dicts: {item_numbers: [...], mover, seconder, aye, nay,
    abstain, absent, recused, voters: [names]}
    """
    blocks: list[dict] = []

    # Split on motion lines
    motion_pat = re.compile(
        r"Motion to (?:Approve|Adopt|Ratify|Accept)\s+"
        r"(?:Item|Items)\s+(?P<items>[\dA-Za-z\s,\-;–—]+?)\s{1,5}"
        r"made\s+by (?:Councilmember|Vice Mayor|Mayor)\s+(?P<mover>\w+)"
        r"(?:\s+and seconded by\s+(?:Councilmember|Vice Mayor|Mayor)\s+(?P<seconder>\w+))?"
    )

    for m in motion_pat.finditer(text):
        raw_items = m.group("items").strip()
        item_numbers = _expand_item_ranges(raw_items)

        # Find the tally line after this motion
        tally_pat = re.compile(
            r"Aye:\s*(\d+)\s*;\s*Nay:\s*(\d+)\s*;\s*Abstain:\s*(\d+)"
            r"\s*;\s*Absent:\s*(\d+)\s*;\s*Recused:\s*(\d+)"
        )
        tally_m = tally_pat.search(text, m.end())
        aye = int(tally_m.group(1)) if tally_m else 0
        nay = int(tally_m.group(2)) if tally_m else 0

        # Find the "For:" voter list after the tally.
        # The voter names span multiple lines in raw PDF, but in normalized
        # text they look like:
        #   "For: Mayor Woods, Vice Mayor Garlid, Councilmember Adams, Councilmember
        #   Amberg, Councilmember Chin, ..."
        # We scan from the tally end and capture until we hit a blank line
        # or a text pattern that's clearly not a voter name.
        for_start = m.end()
        if tally_m:
            for_start = tally_m.end()
        remainder = text[for_start:for_start + 500]

        voters_m = re.search(r"For:\s*(.+?)(?:\n(?!\s*(?:Councilmember|Vice Mayor|Mayor|Amberg|Chin|Hodge|Keating|Adams|Woods|Garlid))|\n{2,}|$)",
                             remainder, re.DOTALL)
        voters = []
        if voters_m:
            raw = voters_m.group(1).strip()
            # Flatten any embedded newlines
            raw = raw.replace("\n", " ")
            # Clean up
            raw = re.split(r"\nRegular|\n\d+", raw)[0].strip().rstrip(";")
            for part in re.split(r"\s*,\s*", raw):
                part = part.strip().rstrip(";")
                if not part:
                    continue
                # Strip role prefixes to get clean names
                name = re.sub(r"^(Mayor|Vice Mayor|Councilmember)\s+", "", part).strip()
                if name and name not in ("Councilmember", "Vice Mayor", "Mayor"):
                    voters.append(name)

        # Deduplicate voter blocks by item set — consent motions often appear
        # multiple times in the PDF (before and after sub-item results).
        # Use the (item_numbers tuple, mover) as a dedup key.
        dedup_key = (tuple(sorted(item_numbers)), m.group("mover"))
        if any(b.get("_dedup_key") == dedup_key for b in blocks):
            continue

        blocks.append({
            "item_numbers": item_numbers,
            "mover": m.group("mover"),
            "seconder": m.group("seconder") or "",
            "aye": aye,
            "nay": nay,
            "voters": list(dict.fromkeys(voters)),  # ordered unique
            "_dedup_key": dedup_key,
        })

    return blocks


def find_item_results(text: str, items: list[dict]) -> list[dict]:
    """Find the result keyword for each agenda item.

    The result appears on its own line after the item's description
    (or after a "Fiscal Impact:" block).
    """
    lines = text.split("\n")
    item_map: dict[str, dict] = {}
    for r in items:
        item_map[r["number"]] = dict(r, result="")

    for r in items:
        num = r["number"]
        line_idx = r["line_idx"]

        # Scan from this item's line forward to find the first result keyword
        for offset in range(1, min(60, len(lines) - line_idx)):
            scan_line = lines[line_idx + offset].strip()
            if not scan_line:
                continue
            # Skip section header lines that match other patterns
            if scan_line in ("TEMPE CITY COUNCIL", "LEGAL ACTION SUMMARY") or \
               scan_line.startswith("Regular City Council Meeting"):
                continue
            # Skip lines that look like motion/tally/voter data
            if scan_line.startswith("Motion to") or scan_line.startswith("Aye:") or \
               scan_line.startswith("For:") or scan_line.startswith("Mayor ") or \
               scan_line.startswith("Vice Mayor") or scan_line.startswith("Councilmember"):
                continue
            # Check for result keyword — must be the only content on the line
            if scan_line in RESULT_KEYWORDS:
                item_map[num]["result"] = scan_line
                break
            # Check for "NO ITEMS"
            if scan_line == "NO ITEMS":
                item_map[num]["result"] = "no_action"
                break
            # Check for informative text that's NOT a result
            if scan_line.startswith("NOTE:"):
                continue
            # Check for "SECOND AND FINAL PUBLIC HEARING WAS SCHEDULED..."
            if scan_line.startswith("SECOND AND FINAL"):
                continue
            # If we hit another item number line, stop looking
            if re.match(r"^\d+[A-Z]?\d*\.\s", scan_line):
                break

    return list(item_map.values())


def _expand_item_ranges(text: str) -> list[str]:
    """Expand item range notation like ``4B1 - 4B8``, ``7A1 - 7A3``."""
    items: list[str] = []
    for part in re.split(r"[;,]\s*", text):
        part = part.strip()
        # Handle "Items X - Y" or "Items X, Y, Z" or "Item X"
        part = re.sub(
            rf"\b(?:Item|Items)\s+",
            "", part, flags=re.IGNORECASE
        ).strip()
        range_m = re.match(r"^([A-Z0-9]+)\s*[-–—]\s*([A-Z0-9]+)$", part, re.IGNORECASE)
        if range_m:
            start_str, end_str = range_m.group(1), range_m.group(2)
            start_m = re.search(r"(\d+)$", start_str)
            end_m = re.search(r"(\d+)$", end_str)
            if start_m and end_m:
                start_num = int(start_m.group(1))
                end_num = int(end_m.group(1))
                prefix = start_str[:max(0, start_m.start(1))]
                for n in range(start_num, end_num + 1):
                    items.append(f"{prefix}{n}")
        else:
            items.append(part.strip())
    return items


def _infer_role(name: str) -> str:
    """Infer Tempe council role from name."""
    known_roles = {
        "woods": "Mayor",
        "garlid": "Vice Mayor",
    }
    key = name.lower().strip()
    return known_roles.get(key, "Councilmember")


def parse_summary_text(text: str) -> dict:
    """Parse a Tempe Legal Action Summary PDF text and return structured data.

    Returns
    -------
    dict with keys:
        supervisors : list[dict]  — present at this meeting
        votes : list[dict]  — vote results per agenda item
    """
    text = _normalize_text(text)

    # Find items and results
    items = find_items(text)
    items_with_results = find_item_results(text, items)

    # Find vote blocks (motions with tallies)
    vote_blocks = find_vote_blocks(text)

    # Build item → vote block lookup
    block_for_item: dict[str, dict] = {}
    for block in vote_blocks:
        for item_num in block["item_numbers"]:
            block_for_item[item_num] = block

    # Collect all voters present at this meeting
    seen_voters: dict[str, bool] = {}

    # Build vote records
    votes_out: list[dict] = []
    for item in items_with_results:
        num = item["number"]
        result = item["result"].lower() if item["result"] else ""

        block = block_for_item.get(num)

        if block:
            # This item had a roll-call vote
            result_map = {
                "approved": "approved", "pass": "approved", "accepted": "approved",
                "adopted": "approved", "ratified": "approved",
                "denied": "denied", "failed": "denied",
                "withdrawn": "withdrawn",
            }
            motion_result = result_map.get(result, result or "approved")

            # Build supervisor votes
            supervisor_votes = []
            for voter_name in block["voters"]:
                seen_voters[voter_name.lower()] = True
                supervisor_votes.append({
                    "name": voter_name,
                    "vote": "aye",
                })

            vote_text = (f"Motion to Approve Items {', '.join(block['item_numbers'])} "
                         f"made by Councilmember {block['mover']} "
                         f"{'and seconded by Councilmember ' + block['seconder'] + ' ' if block['seconder'] else ''}"
                         f"  Aye: {block['aye']}; Nay: {block['nay']};")

            votes_out.append({
                "agenda_item_number": num,
                "motion_result": motion_result,
                "vote_text": vote_text,
                "supervisor_votes": supervisor_votes,
                "c_number": "",
                "c_number_base": "",
            })
        elif result and result not in ("", "no_action"):
            # Item had an explicit result but no individual motion (consent sub-item)
            votes_out.append({
                "agenda_item_number": num,
                "motion_result": result,
                "vote_text": "",
                "supervisor_votes": [],
                "c_number": "",
                "c_number_base": "",
            })

    # Build supervisor list
    supervisors_out: list[dict] = []
    for voter_name in sorted(seen_voters):
        name_clean = voter_name.capitalize()
        supervisors_out.append({
            "name": name_clean,
            "normalized_name": voter_name.lower(),
            "role": _infer_role(voter_name),
            "present": True,
        })

    if not supervisors_out and votes_out:
        # If we have votes but no explicit voter list, infer from tally
        pass

    log.info("Summary: %d items, %d vote blocks, %d supervisors",
             len(items), len(vote_blocks), len(supervisors_out))

    return {
        "supervisors": supervisors_out,
        "votes": votes_out,
    }


def backfill_tempe_votes(dry_run: bool = True, limit: int = 0,
                         verbose: bool = True) -> dict:
    """Backfill Tempe CC vote data for meetings missing Legal Action Summary votes.

    Queries all Tempe CC Regular City Council Meetings that have been
    fully synced (sync_status='complete') but have no vote records in
    the agenda_item_votes table.  Attempts to fetch and parse their
    Legal Action Summary PDFs from OnBase.

    Parameters
    ----------
    dry_run : bool
        If True, report findings but do NOT persist votes to the database.
        Set to False to actually store extracted votes.
    limit : int
        Maximum number of meetings to process.  0 = all.
    verbose : bool
        Print per-meeting status lines.

    Returns
    -------
    dict with summary keys:
        attempted, found_votes, no_summary, errors, total_meetings_without_votes
    """
    import sys
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes
    from sqlalchemy import select, func

    session = get_session()

    # Find all complete Regular CC meetings without any vote records
    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )

    rows = session.execute(
        select(
            MeetingModel.id,
            MeetingModel.meeting_id,
            MeetingModel.meeting_date,
            MeetingModel.meeting_type,
        )
        .where(MeetingModel.body == "tempe-cc")
        .where(MeetingModel.meeting_type == "Regular City Council Meeting")
        .where(MeetingModel.item_count_actual > 0)
        .where(~MeetingModel.id.in_(meetings_with_votes))
        .order_by(MeetingModel.meeting_date)
    ).all()
    session.close()

    if not rows:
        report = {
            "attempted": 0, "found_votes": 0, "no_summary": 0, "errors": 0,
            "total_meetings_without_votes": 0,
            "details": [],
        }
        return report

    if limit:
        rows = rows[:limit]

    details: list[dict] = []
    counts = {"found": 0, "no_summary": 0, "errors": 0}

    for idx, row in enumerate(rows, 1):
        meeting_db_id = row.id
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        meeting_type = row.meeting_type or "Regular City Council Meeting"

        try:
            vote_data = fetch_and_parse_summary(
                int(meeting_id), meeting_date, meeting_type,
            )
        except Exception as e:
            counts["errors"] += 1
            err_msg = str(e)[:200]
            if verbose:
                print(f"  [{idx}/{len(rows)}] {meeting_id} {meeting_date}: ERROR - {err_msg}")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "error",
                "error": err_msg,
            })
            continue

        num_votes = len(vote_data.get("votes", []))

        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    persist_votes(
                        session, "tempe-cc", meeting_id,
                        vote_data["supervisors"],
                        vote_data["votes"],
                    )
                    # Ensure vote supervisors are registered as public body members
                    _ensure_backfill_members(session, vote_data["supervisors"])
                    session.commit()
                except Exception as e:
                    session.rollback()
                    counts["errors"] += 1
                    err_msg = str(e)[:200]
                    if verbose:
                        print(f"  [{idx}/{len(rows)}] {meeting_id} {meeting_date}: PERSIST ERROR - {err_msg}")
                    details.append({
                        "meeting_id": meeting_id,
                        "meeting_date": meeting_date,
                        "status": "persist_error",
                        "error": err_msg,
                    })
                    continue
                finally:
                    session.close()

            sup_names = [s.get("name", "") for s in vote_data.get("supervisors", [])]
            if verbose:
                print(f"  [{idx}/{len(rows)}] {meeting_id} {meeting_date}: {num_votes} votes"
                      f" ({', '.join(sup_names)})")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "votes_found",
                "vote_count": num_votes,
                "supervisors": sup_names,
            })
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(rows)}] {meeting_id} {meeting_date}: no summary PDF")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "no_summary",
            })

    report = {
        "attempted": len(rows),
        "found_votes": counts["found"],
        "no_summary": counts["no_summary"],
        "errors": counts["errors"],
        "total_meetings_without_votes": len(rows),
        "details": details,
    }
    return report


def _ensure_backfill_members(session, sup_list):
    """Ensure Tempe council members have BodyMembership rows.
    Mirrors _ensure_tempe_members from main.py but standalone."""
    from sqlalchemy import select
    from db import PublicBody, BodyMembership
    from db.persist import _find_or_create_person, _ensure_membership

    _TEMPE_NAME_MAP = {
        "adams": "Jennifer Adams", "amberg": "Nikki Amberg",
        "chin": "Arlene Chin", "garlid": "Doreen Garlid",
        "hodge": "Berdetta Hodge", "keating": "Randy Keating",
        "navarro": "Joel Navarro", "woods": "Corey D Woods",
    }
    titler_map = {"woods": "Mayor", "garlid": "Vice Mayor"}

    for sup in sup_list:
        norm = sup.get("normalized_name", "").strip().lower()
        if not norm:
            continue
        role = titler_map.get(norm, "Councilmember")
        name = _TEMPE_NAME_MAP.get(norm) or sup.get("name", norm.capitalize())
        person, _ = _find_or_create_person(
            session, name, norm,
            log_prefix="_ensure_backfill_members[",
        )
        if person and person.id:
            membership = _ensure_membership(session, person.id, "tempe-cc")
            if membership and role:
                membership.role = role
    session.flush()


def fetch_and_parse_summary(meeting_id: int, meeting_date: str = "",
                            meeting_type: str = "") -> dict:
    """Download and parse the Legal Action Summary PDF for a meeting.

    Parameters
    ----------
    meeting_id : int
        OnBase meeting ID.
    meeting_date : str, optional
        Date in YYYY-MM-DD format. Used to construct the document name.
    meeting_type : str, optional
        Meeting type (e.g. ``Regular City Council Meeting``).

    Returns
    -------
    dict with keys ``supervisors``, ``votes``, or empty lists on failure.
    """
    from scraper.onbase import TEMPE_CONFIG, download_document

    # Construct a few candidate filenames
    candidates = _summary_document_names(meeting_id, meeting_date, meeting_type)

    for name in candidates:
        try:
            pdf_bytes = download_document(TEMPE_CONFIG, meeting_id, name, doc_type=3)
            if pdf_bytes and len(pdf_bytes) >= 1000 and not pdf_bytes[:5] == b"<!DOC":
                log.info("Summary PDF found: %s (%d bytes)", name, len(pdf_bytes))
                break
        except Exception:
            continue
    else:
        log.info("No summary PDF available for meeting %d", meeting_id)
        return {"supervisors": [], "votes": []}

    text = _extract_pdf_text(pdf_bytes)
    if not text:
        return {"supervisors": [], "votes": []}

    return parse_summary_text(text)


def _summary_document_names(meeting_id: int, meeting_date: str = "",
                            meeting_type: str = "") -> list[str]:
    """Return candidate document names for the summary PDF."""
    names = [f"Regular_City_Council_Meeting_{meeting_id}_Summary.pdf"]

    if meeting_date and " " not in meeting_date:
        try:
            import datetime
            date = datetime.date.fromisoformat(meeting_date)
            for suffix in [f"_{date.month}_{date.day}_{date.year}_6_00_00_PM.pdf",
                           f"_{date.month}_{date.day}_{date.year}_5_30_00_PM.pdf"]:
                names.append(f"Regular_City_Council_Meeting_{meeting_id}_Summary{suffix}")
        except ValueError:
            pass

    if meeting_type:
        type_slug = meeting_type.replace(" ", "_")
        names.insert(0, f"{type_slug}_{meeting_id}_Summary.pdf")

    return names


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF byte stream."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
#  BOA (Board of Adjustment) & HA (Housing Authority) votes
# ═══════════════════════════════════════════════════════════════

BOA_HEADER_PREFIXES = frozenset({
    "Board of Adjustment",
    "REGULAR MEETING SUMMARY",
    "Harry E. Mitchell",
    "Tempe City Hall",
    "Virtual meeting",
    "Members of the Board of Adjustment",
    "Visit",
    "Virtual Board & Commission Meetings",
    "Agenda Online",
    "Meeting Date:",
    "Meeting Time:",
    "Board Members Present:",
    "Board Members Absent:",
    "Staff Present:",
})

HA_HEADER_PREFIXES = frozenset({
    "Tempe Housing Authority",
    "REGULAR MEETING SUMMARY",
    "Harry E. Mitchell",
    "Tempe City Hall",
    "Virtual meeting",
    "Members of the Tempe Housing Authority",
    "Visit",
    "Virtual Board & Commission Meetings",
    "Agenda Online",
    "Meeting Date:",
    "Meeting Time:",
    "Board Members Present:",
    "Board Members Absent:",
    "Staff Present:",
})


# ── BOA candidate filenames ──


def _boa_summary_document_names(meeting_id: int, meeting_date: str = "") -> list[str]:
    """Return candidate document names for BOA Summary PDF."""
    names = [
        f"Board_of_Adjustment_Regular_Meeting_{meeting_id}_Summary.pdf",
        f"Board_of_Adjustment_Meeting_{meeting_id}_Summary.pdf",
        f"Board_of_Adjustment_{meeting_id}_Summary.pdf",
    ]
    if meeting_date:
        try:
            import datetime
            date = datetime.date.fromisoformat(meeting_date)
            ts_name = (
                f"Board_of_Adjustment_Regular_Meeting_{meeting_id}_Summary_"
                f"{date.month}_{date.day}_{date.year}_6_00_00_PM.pdf"
            )
            names.append(ts_name)
            ts_name2 = (
                f"Board_of_Adjustment_Regular_Meeting_{meeting_id}_Summary_"
                f"{date.month}_{date.day}_{date.year}_5_30_00_PM.pdf"
            )
            names.append(ts_name2)
        except ValueError:
            pass
    return names


# ── HA candidate filenames ──


def _ha_summary_document_names(meeting_id: int) -> list[str]:
    """Return candidate document names for HA Summary PDF."""
    return [
        f"Tempe_Housing_Authority_{meeting_id}_Summary.pdf",
        f"Tempe_Housing_Authority_Meeting_{meeting_id}_Summary.pdf",
    ]


# ── BOA inline summary parser ──
#
# BOA summaries have results INLINE at the end of description lines:
#
#   2A. Board of Adjustment - 1/28/26 Study Session   APPROVED
#   3A. Request Variance ...  APPROVED WITH MODIFIED CONDITIONS (7-0)
#   5A. Appeal ...  APPEAL DENIED (7/0)
#
# Vote tally formats: (7-0), (7/0 VOTE), (5/2), (7/0)

# Inline result keywords found in BOA summaries
_BOA_RESULT_KEYWORDS = frozenset({
    "APPROVED WITH MODIFIED CONDITIONS",
    "APPEAL APPROVED",
    "APPEAL DENIED",
    "APPROVED",
    "DENIED",
    "FAILED",
    "WITHDRAWN",
    "CONTINUED",
    "NO AFFIRMATIVE VOTE",
})

# Section headings in BOA — numbered items whose title is all-caps
_BOA_SECTION_HEADINGS = frozenset({
    "CALL TO ORDER",
    "CONSIDERATION OF MEETING MINUTES",
    "VARIANCE REQUEST",
    "VARIANCE",
    "ABATEMENT APPEAL",
    "ADMINISTRATIVE DECISION APPEAL",
    "ABATEMENT APPEAL/ADMINISTRATIVE DECISION APPEAL",
    "CHAIR/STAFF UPDATE(S) AND ANNOUNCEMENT(S)",
    "ADJOURNMENT",
    "ELECTION OF OFFICERS",
})

# Vote tally regex: handles (7-0), (7/0 VOTE), (5/2), (7/0), (7-0 VOTE, 1 ABSTAIN)
_BOA_VOTE_TALLY = re.compile(
    r"\((?P<aye>\d+)\s*[-/]\s*(?P<nay>\d+)"
    r"(?:\s*VOTE)?"
    r"(?:\s*,\s*(?P<abstain>\d+)\s*ABST(?:AIN|ENTION))?\)",
    re.IGNORECASE,
)

# Item number pattern: 1., 2A., 3B., 4., 5A.
_ITEM_NUMBER_RE = re.compile(r"^(\d+[A-Za-z]?)\.\s+(.*)")

# All-caps section heading check
_ALL_CAPS_LINE = re.compile(r"^[A-Z][A-Z\s/()\-–—]+$")


# ── BOA description block → result + tally extraction ──


def _is_boa_section_heading(text: str) -> bool:
    """Check if a line is a BOA section heading (all-caps, not actionable).

    A heading is a numbered line whose title is purely all-caps and is in
    the known heading set.  Items like "6. EXECUTIVE SESSION VOTE..."
    look all-caps but are NOT headings (they have results), so only match
    against known heading names.
    """
    cleaned = text.strip().rstrip(".").strip()
    if not cleaned:
        return True
    # Check against known heading set
    up = cleaned.upper()
    for heading in _BOA_SECTION_HEADINGS:
        if up.startswith(heading):
            return True
        if heading.startswith(up):
            return True
    return False


def _extract_boa_result(description_text: str) -> dict:
    """Extract result keyword and optional vote tally from BOA item description text.

    Finds the EARLIEST (leftmost) matching result keyword.  This handles
    cases like "APPROVED NO AFFIRMATIVE VOTE WAS TAKEN" correctly: the
    result is APPROVED, not NO AFFIRMATIVE VOTE.

    Returns dict with keys: motion_result, vote_text, aye, nay, abstain.
    Returns None if no result found.
    """
    # Normalize line breaks to spaces for scanning
    flat = description_text.replace("\n", " ").strip()
    if not flat:
        return None

    # Check for "- NONE" suffix (indicates no items under this heading)
    if flat.rstrip(".").endswith(" - NONE"):
        return None

    up = flat.upper()

    # Find the EARLIEST keyword match (by position in text)
    best_idx = len(up) + 1
    best_kw = None

    for kw in _BOA_RESULT_KEYWORDS:
        idx = up.find(kw)
        if idx >= 0 and idx < best_idx:
            best_idx = idx
            best_kw = kw

    if best_kw is None:
        return None

    # Extract everything from the keyword onward
    result_text = flat[best_idx:].strip().rstrip(".").rstrip()
    if result_text.endswith("."):
        result_text = result_text[:-1].strip()

    # Look for vote tally in result text
    tally_m = _BOA_VOTE_TALLY.search(result_text)
    aye = int(tally_m.group("aye")) if tally_m else 0
    nay = int(tally_m.group("nay")) if tally_m else 0
    abstain = int(tally_m.group("abstain")) if tally_m and tally_m.group("abstain") else 0

    # Extract just the motion_result (remove tally)
    motion_result = _BOA_VOTE_TALLY.sub("", result_text).strip().rstrip(",").strip()

    return {
        "motion_result": motion_result,
        "vote_text": result_text,
        "aye": aye,
        "nay": nay,
        "abstain": abstain,
    }


def extract_boa_items_from_summary(text: str) -> list[dict]:
    """Parse BOA summary text and extract agenda item results with vote tallies.

    The BOA summary format has results inline at the end of description lines.
    Section headings (numbered, all-caps) are skipped.

    Returns a list of dicts:
        {
            "agenda_item_number": str,
            "motion_result": str,
            "vote_text": str,
            "aye": int,
            "nay": int,
            "abstain": int,
        }
    """
    lines = text.split("\n")
    items: list[dict] = []

    # Header lines to skip (first ~15 lines + page-break headers)
    header_lines = frozenset({
        "Board of Adjustment Regular Meeting",
        "BOARD OF ADJUSTMENT",
        "REGULAR MEETING SUMMARY",
    })
    header_start_patterns = (
        "Legal Advice:", "Harry E. Mitchell", "Tempe City Hall",
        "31 East Fifth Street", "AND/OR Virtual", "Wednesday,", "Thursday,",
        "REVISED", "Members of the Board", "Visit Virtual",
        "Virtual Board", "attendance information",
    )
    # Page-break header patterns (appear mid-document between pages)
    page_header_patterns = (
        "Board of Adjustment Regular Meeting Meeting Summary",
        "Wednesday, April",
        "Thursday, April",
        "Wednesday, March",
        "Thursday, March",
        "Wednesday, February",
        "Thursday, February",
        "Wednesday, January",
        "Thursday, January",
    )
    # Numbers appearing alone on a line (page numbers like "2")
    _PAGE_NUM_RE = re.compile(r"^\d+$")

    current_item: dict = None
    description_lines: list[str] = []
    line_count = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Skip header lines (first 20 lines of the document)
        if i < 20:
            if stripped in header_lines:
                continue
            if any(stripped.startswith(p) for p in header_start_patterns):
                continue
            # Also skip the legal boilerplate pages (they have no item numbers)
            if "According to the Arizona Open Meeting Law" in stripped:
                continue

        # Check for item number pattern
        item_m = _ITEM_NUMBER_RE.match(stripped)
        if item_m:
            item_num = item_m.group(1)
            rest = item_m.group(2).strip()

            # Finalize previous item
            if current_item and description_lines:
                desc_text = "\n".join(description_lines)
                result = _extract_boa_result(desc_text)
                if result:
                    items.append({
                        "agenda_item_number": current_item,
                        **result,
                    })
            elif current_item:
                # Single-line item; check the rest directly
                result = _extract_boa_result(rest)
                if result:
                    items.append({
                        "agenda_item_number": current_item,
                        **result,
                    })

            current_item = item_num
            description_lines = []

            # Check if this item is a section heading (all-caps title, no actionable result)
            if _is_boa_section_heading(rest.rstrip(".").strip()):
                current_item = None
                continue

            # For multi-line items, the rest of this line starts the description
            description_lines.append(rest)
            continue

        # Skip page-break headers and page numbers (appear mid-document)
        if any(stripped.startswith(p) for p in page_header_patterns):
            continue
        if _PAGE_NUM_RE.match(stripped):
            continue

        # Not an item line — add to description if we're in an item
        if current_item is not None:
            description_lines.append(stripped)

    # Finalize last item
    if current_item and description_lines:
        desc_text = "\n".join(description_lines)
        result = _extract_boa_result(desc_text)
        if result:
            items.append({
                "agenda_item_number": current_item,
                **result,
            })

    return items


# ── PDF fetch + parse (shared by BOA and HA) ──


def _download_and_parse_summary_pdf(
    meeting_id: int,
    candidate_names: list[str],
    label: str,
) -> dict:
    """Download a summary PDF via OnBase and parse it.

    For BOA: uses the inline result parser (aggregate tallies only).
    For HA: uses the CC-style roll-call parser (individual member names).

    Returns dict with keys ``supervisors`` and ``votes`` for HA (CC-style),
    or just ``votes`` for BOA (DRC-style).
    """
    from scraper.onbase import TEMPE_CONFIG, download_document

    for name in candidate_names:
        try:
            pdf_bytes = download_document(TEMPE_CONFIG, meeting_id, name, doc_type=3)
            if pdf_bytes and len(pdf_bytes) >= 1000 and not pdf_bytes[:5] == b"<!DOC":
                log.info("%s Summary PDF found: %s (%d bytes)", label, name, len(pdf_bytes))
                break
        except Exception:
            continue
    else:
        log.info("No %s summary PDF available for meeting %d", label, meeting_id)
        return {"supervisors": [], "votes": []}

    text = _extract_pdf_text(pdf_bytes)
    if not text or not text.strip():
        return {"supervisors": [], "votes": []}

    if label == "BOA":
        items = extract_boa_items_from_summary(text)
        log.info("BOA summary for meeting %d: %d items with results", meeting_id, len(items))
        return {"supervisors": [], "votes": items}
    else:
        # HA uses the CC-style roll-call parser, but with HA-specific role names.
        # Normalize HA roles to match what parse_summary_text expects:
        #   "Board Member" -> "Councilmember"
        #   "Chair" -> "Mayor"
        #   "Vice Chair" -> "Vice Mayor"
        #   "Resident Member" -> "Councilmember"
        ha_normalized = text
        ha_normalized = re.sub(r"\bBoard Member\b", "Councilmember", ha_normalized)
        ha_normalized = re.sub(r"\bVice Chair\b", "Vice Mayor", ha_normalized)
        ha_normalized = re.sub(r"\b(?<!Vice )Chair\b", "Mayor", ha_normalized)
        # Handle "Resident" across linebreaks -> "Councilmember"
        ha_normalized = re.sub(r"\bResident[\s\n]+Member\b", "Councilmember", ha_normalized)
        parsed = parse_summary_text(ha_normalized)
        log.info("HA summary for meeting %d: %d items with results, %d supervisors",
                 meeting_id, len(parsed["votes"]), len(parsed["supervisors"]))
        return parsed


# ── Convenience wrappers ──


def extract_boa_votes(meeting_id: int, meeting_date: str = "",
                      meeting_type: str = "") -> dict:
    """Fetch and parse BOA meeting summary PDF, returning vote data.

    Returns dict with ``votes`` key (no individual member names).
    """
    candidates = _boa_summary_document_names(meeting_id, meeting_date)
    return _download_and_parse_summary_pdf(meeting_id, candidates, label="BOA")


def extract_ha_votes(meeting_id: int, meeting_date: str = "",
                     meeting_type: str = "") -> dict:
    """Fetch and parse HA meeting summary PDF, returning vote data.

    Returns dict with ``supervisors`` and ``votes`` keys (CC-style roll-call).
    """
    candidates = _ha_summary_document_names(meeting_id)
    return _download_and_parse_summary_pdf(meeting_id, candidates, label="HA")


# ── Backfill: BOA ──


def backfill_boa_votes(dry_run: bool = True, limit: int = 0,
                        verbose: bool = True) -> dict:
    """Backfill BOA vote data for Regular meetings missing summary votes.

    Queries all BOA Regular meetings that have been fully synced
    (sync_status='complete') but have no vote records.  Attempts to
    fetch and parse their summary PDFs from OnBase.

    Parameters
    ----------
    dry_run : bool
        If True, report findings but do NOT persist votes.
    limit : int
        Maximum number of meetings to process.  0 = all.
    verbose : bool
        Print per-meeting status lines.

    Returns
    -------
    dict with summary keys
    """
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes
    from sqlalchemy import select, func

    session = get_session()

    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )

    BOA_BODIES = ("tempe-boa",)

    rows = session.execute(
        select(
            MeetingModel.id,
            MeetingModel.meeting_id,
            MeetingModel.meeting_date,
            MeetingModel.meeting_type,
            MeetingModel.body,
        )
        .where(MeetingModel.body.in_(BOA_BODIES))
        .where(MeetingModel.meeting_type.ilike("%Regular%"))
        .where(MeetingModel.sync_status == "complete")
        .where(MeetingModel.item_count_actual > 0)
        .where(
            ~MeetingModel.id.in_(meetings_with_votes)
            | MeetingModel.id.is_(None)
        )
        .order_by(MeetingModel.meeting_date)
    ).all()
    session.close()

    if not rows:
        if verbose:
            print("  (no BOA meetings without votes)")
        return {
            "attempted": 0, "found_votes": 0, "no_summary": 0, "errors": 0,
            "details": [],
        }

    past_rows = [r for r in rows if r.meeting_date and r.meeting_date < "2026-06-01"]
    if limit:
        past_rows = past_rows[:limit]

    details: list[dict] = []
    counts = {"found": 0, "no_summary": 0, "errors": 0}

    for idx, row in enumerate(past_rows, 1):
        meeting_db_id = row.id
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        body_code = row.body

        try:
            vote_data = extract_boa_votes(int(meeting_id), meeting_date)
        except Exception as e:
            counts["errors"] += 1
            err_msg = str(e)[:200]
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: ERROR - {err_msg}")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "error",
                "error": err_msg,
            })
            continue

        num_votes = len(vote_data["votes"])

        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    persist_votes(
                        session, body_code, meeting_id,
                        [],  # BOA summaries don't list individual member names
                        vote_data["votes"],
                    )
                    session.commit()
                except Exception as e:
                    session.rollback()
                    counts["errors"] += 1
                    err_msg = str(e)[:200]
                    if verbose:
                        print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: PERSIST ERROR - {err_msg}")
                    details.append({
                        "meeting_id": meeting_id,
                        "meeting_date": meeting_date,
                        "status": "persist_error",
                        "error": err_msg,
                    })
                    continue
                finally:
                    session.close()

            if verbose:
                details_str = []
                for v in vote_data["votes"]:
                    vstr = f"{v['agenda_item_number']}: {v.get('motion_result', '?')}"
                    if v.get('aye') or v.get('nay'):
                        vstr += f" ({v['aye']}-{v['nay']}"
                        if v.get('abstain'):
                            vstr += f", {v['abstain']} abs"
                        vstr += ")"
                    details_str.append(vstr)
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes")
                if verbose and details_str:
                    print(f"       {' | '.join(details_str)}")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "votes_found",
                "vote_count": num_votes,
            })
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no summary")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "no_summary",
            })

    return {
        "attempted": len(past_rows),
        "found_votes": counts["found"],
        "no_summary": counts["no_summary"],
        "errors": counts["errors"],
        "details": details,
    }


# ── Backfill: HA ──


def backfill_ha_votes(dry_run: bool = True, limit: int = 0,
                       verbose: bool = True) -> dict:
    """Backfill HA (Housing Authority) vote data for meetings missing votes.

    Queries all HA meetings that have been fully synced
    (sync_status='complete') with agenda items but have no vote records.
    Attempts to fetch and parse their summary PDFs from OnBase.

    Parameters
    ----------
    dry_run : bool
        If True, report findings but do NOT persist votes.
    limit : int
        Maximum number of meetings to process.  0 = all.
    verbose : bool
        Print per-meeting status lines.

    Returns
    -------
    dict with summary keys
    """
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes
    from sqlalchemy import select, func

    session = get_session()

    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )

    HA_BODIES = ("tempe-ha",)

    rows = session.execute(
        select(
            MeetingModel.id,
            MeetingModel.meeting_id,
            MeetingModel.meeting_date,
            MeetingModel.meeting_type,
            MeetingModel.body,
        )
        .where(MeetingModel.body.in_(HA_BODIES))
        .where(MeetingModel.sync_status == "complete")
        .where(MeetingModel.item_count_actual > 0)
        .where(
            ~MeetingModel.id.in_(meetings_with_votes)
            | MeetingModel.id.is_(None)
        )
        .order_by(MeetingModel.meeting_date)
    ).all()
    session.close()

    if not rows:
        if verbose:
            print("  (no HA meetings without votes)")
        return {
            "attempted": 0, "found_votes": 0, "no_summary": 0, "errors": 0,
            "details": [],
        }

    past_rows = [r for r in rows if r.meeting_date and r.meeting_date < "2026-06-01"]
    if limit:
        past_rows = past_rows[:limit]

    details: list[dict] = []
    counts = {"found": 0, "no_summary": 0, "errors": 0}

    for idx, row in enumerate(past_rows, 1):
        meeting_db_id = row.id
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        body_code = row.body

        try:
            vote_data = extract_ha_votes(int(meeting_id))
        except Exception as e:
            counts["errors"] += 1
            err_msg = str(e)[:200]
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: ERROR - {err_msg}")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "error",
                "error": err_msg,
            })
            continue

        num_votes = len(vote_data["votes"])

        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    # HA may have individual member names (CC-style roll-call)
                    supervisors = vote_data.get("supervisors", [])
                    persist_votes(
                        session, body_code, meeting_id,
                        supervisors,
                        vote_data["votes"],
                    )
                    session.commit()
                except Exception as e:
                    session.rollback()
                    counts["errors"] += 1
                    err_msg = str(e)[:200]
                    if verbose:
                        print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: PERSIST ERROR - {err_msg}")
                    details.append({
                        "meeting_id": meeting_id,
                        "meeting_date": meeting_date,
                        "status": "persist_error",
                        "error": err_msg,
                    })
                    continue
                finally:
                    session.close()

            sup_names = [s.get("name", "") for s in vote_data.get("supervisors", [])]
            if verbose:
                sup_str = f" ({', '.join(sup_names)})" if sup_names else ""
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes{sup_str}")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "votes_found",
                "vote_count": num_votes,
                "supervisors": sup_names if sup_names else [],
            })
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no summary")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "no_summary",
            })

    # ════════════════════════════════════════════════
    #  NEW: RIO vote extraction (appended below)
    # ════════════════════════════════════════════════

    return {
        "attempted": len(past_rows),
        "found_votes": counts["found"],
        "no_summary": counts["no_summary"],
        "errors": counts["errors"],
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════
#  RIO (Rio Salado Community Facilities District Board) votes
# ═══════════════════════════════════════════════════════════════
#
# RIO publishes Legal Action Summary PDFs (doctype=3) that follow the same
# format as City Council: roll-call motions with individual member names.
# The role titles differ:
#   "Board Member" ↔ "Councilmember"
#   "Chair" ↔ "Mayor"
#   "Vice Chair" ↔ "Vice Mayor"
#
# We normalize the text to match what parse_summary_text expects.


def _rio_summary_document_names(meeting_id: int, meeting_date: str = "") -> list[str]:
    """Return candidate document names for RIO Summary PDF."""
    names = [
        f"Rio_Salado_Community_Facilities_District_Board_{meeting_id}_Summary.pdf",
        f"Rio_Salado_CFD_Board_{meeting_id}_Summary.pdf",
    ]
    if meeting_date:
        try:
            import datetime
            date = datetime.date.fromisoformat(meeting_date)
            ts_name = (
                f"Rio_Salado_Community_Facilities_District_Board_Special_Meeting_"
                f"{meeting_id}_Agenda_{date.month}_{date.day}_{date.year}_"
                f"6_00_00_PM.pdf"
            )
            names.append(ts_name)
            ts_name2 = (
                f"Rio_Salado_Community_Facilities_District_Board_Special_Meeting_"
                f"{meeting_id}_Agenda_{date.month}_{date.day}_{date.year}_"
                f"5_45_00_PM.pdf"
            )
            names.append(ts_name2)
        except ValueError:
            pass
    return names


def _normalize_rio_text(text: str) -> str:
    """Normalize Rio Salado CFD Board role titles to City Council equivalents."""
    text = re.sub(r"\bVice Chair\b", "Vice Mayor", text)
    text = re.sub(r"\bBoard Member\b", "Councilmember", text)
    text = re.sub(r"(?<!Vice )\bChair\b", "Mayor", text)
    text = re.sub(
        r"RIO SALADO COMMUNITY FACILITIES DISTRICT BOARD MEETING",
        "REGULAR COUNCIL MEETING",
        text,
    )
    text = text.replace(
        "Rio Salado Community Facilities District Board Meeting Legal Action Summary",
        "Regular City Council Meeting Legal Action Summary",
    )
    text = text.replace(
        "Rio Salado Community Facilities District Board",
        "Tempe City Council",
    )
    return text


def extract_rio_votes(meeting_id: int, meeting_date: str = "",
                      meeting_type: str = "") -> dict:
    """Fetch and parse RIO meeting Summary PDF, returning vote data.

    The RIO Summary PDF (doctype=3) has the same format as the City
    Council's Legal Action Summary, with roll-call motions, vote
    tallies, and individual member names.

    Returns dict with ``supervisors`` and ``votes`` keys.
    """
    from scraper.onbase import TEMPE_CONFIG, download_document

    candidates = _rio_summary_document_names(meeting_id, meeting_date)
    pdf_bytes = None
    for name in candidates:
        try:
            pdf_bytes = download_document(
                TEMPE_CONFIG, meeting_id, name, doc_type=3
            )
            if pdf_bytes and len(pdf_bytes) >= 1000 \
                    and not pdf_bytes[:5] == b"<!DOC":
                log.info("RIO Summary PDF found: %s (%d bytes)",
                         name, len(pdf_bytes))
                break
            pdf_bytes = None
        except Exception:
            continue

    if not pdf_bytes:
        log.info("No RIO summary PDF available for meeting %d", meeting_id)
        return {"supervisors": [], "votes": []}

    text = _extract_pdf_text(pdf_bytes)
    if not text or not text.strip():
        return {"supervisors": [], "votes": []}

    normalized = _normalize_rio_text(text)
    return parse_summary_text(normalized)


def backfill_rio_votes(dry_run: bool = True, limit: int = 0,
                        verbose: bool = True) -> dict:
    """Backfill RIO vote data for meetings missing summary votes."""
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes, _find_or_create_person, _ensure_membership
    from sqlalchemy import select, func

    session = get_session()
    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )
    rows = session.execute(
        select(
            MeetingModel.id, MeetingModel.meeting_id,
            MeetingModel.meeting_date, MeetingModel.meeting_type,
            MeetingModel.body,
        )
        .where(MeetingModel.body.in_(("tempe-rio",)))
        .where(MeetingModel.sync_status == "complete")
        .where(MeetingModel.item_count_actual > 0)
        .where(~MeetingModel.id.in_(meetings_with_votes) | MeetingModel.id.is_(None))
        .order_by(MeetingModel.meeting_date)
    ).all()
    session.close()

    if not rows:
        if verbose:
            print("  (no RIO meetings without votes)")
        return {"attempted": 0, "found_votes": 0, "no_summary": 0, "errors": 0, "details": []}

    past_rows = [r for r in rows if r.meeting_date and r.meeting_date < "2026-06-01"]
    if limit:
        past_rows = past_rows[:limit]

    details = []
    counts = {"found": 0, "no_summary": 0, "errors": 0}

    for idx, row in enumerate(past_rows, 1):
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        body_code = row.body

        try:
            vote_data = extract_rio_votes(int(meeting_id), meeting_date)
        except Exception as e:
            counts["errors"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: ERROR - {e}")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "error", "error": str(e)[:200]})
            continue

        num_votes = len(vote_data["votes"])

        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    supervisors = vote_data.get("supervisors", [])
                    persist_votes(session, body_code, meeting_id, supervisors, vote_data["votes"])
                    for sup in supervisors:
                        norm = sup.get("normalized_name", "").strip().lower()
                        if norm:
                            name = sup.get("name", norm.capitalize())
                            person, _ = _find_or_create_person(session, name, norm, log_prefix="rio[")
                            if person and person.id:
                                _ensure_membership(session, person.id, "tempe-rio")
                    session.commit()
                except Exception as e:
                    session.rollback()
                    counts["errors"] += 1
                    if verbose:
                        print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: PERSIST ERROR - {e}")
                    details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "persist_error", "error": str(e)[:200]})
                    continue
                finally:
                    session.close()

            sup_names = [s.get("name", "") for s in vote_data.get("supervisors", [])]
            if verbose:
                sup_str = f" ({', '.join(sup_names)})" if sup_names else ""
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes{sup_str}")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "votes_found", "vote_count": num_votes, "supervisors": sup_names})
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no summary")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "no_summary"})

    return {
        "attempted": len(past_rows),
        "found_votes": counts["found"],
        "no_summary": counts["no_summary"],
        "errors": counts["errors"],
        "details": details,
    }
