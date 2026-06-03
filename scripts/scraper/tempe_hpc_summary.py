"""Extract vote data from Tempe Historic Preservation Commission (HPC) Minutes Packets.

HPC publishes Minutes Packets (documentType=6 in OnBase) for their Regular
meetings. Unlike the City Council's Legal Action Summary (documentType=3),
HPC minutes are full narrative/transcript-style documents that include
per-item motion blocks with roll-call vote tallies and individual
commissioner names.

Example vote block in the minutes packet::

    4A) Historic Preservation Commission – Regular Meeting 03/11/26

    Motion by Vice Chair Fackler to approve Meeting Minutes for 03/11/26;
    second by Commissioner Lerner. Motion passed on 9-0 vote.
    Ayes: Chair Justice, Vice Chair Fackler, Commissioners Kurooka, Williams,
    Lamp, Senat, Lerner, Melcher and Davis
    Nays: None
    Abstain: None
    Absent: None

Only items that receive a formal motion have vote data.  Procedural items
(Call to Order, Public Appearances, Adjournment) have no motions.
"""

from __future__ import annotations

import datetime
import io
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

# ── HPC roles that appear in voter name lists ──

_ROLE_PREFIXES = frozenset({
    "Chair", "Vice Chair", "Commissioner", "Commissioners",
})

# ── HPC document filename patterns ──

_HPC_MINUTES_FILENAME_PATTERN = (
    "Historic_Preservation_Commission_Meeting_{mid}_Minutes_Packet"
    "_{month}_{day}_{year}_{time_suffix}.pdf"
)

_HPC_MINUTES_FILENAME_SIMPLE = (
    "Historic_Preservation_Commission_Meeting_{mid}_Minutes.pdf"
)

_TIME_SUFFIXES = ["6_00_00_PM", "5_30_00_PM"]


# ── Text extraction helpers ──


def _normalize_text(text: str) -> str:
    """Collapse whitespace in the extracted PDF text."""
    text = re.sub(r"(\w)-\n+(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


# ── Document name generation ──


def _minutes_document_names(meeting_id: int, meeting_date: str = "") -> list[str]:
    """Return candidate document names for HPC Minutes Packet (documentType=6).

    Returns filenames without paths, ordered by specificity (most specific
    first).
    """
    names: list[str] = []

    if meeting_date and " " not in meeting_date:
        try:
            date = datetime.date.fromisoformat(meeting_date)
            month, day, year = date.month, date.day, date.year
            for ts in _TIME_SUFFIXES:
                names.append(
                    _HPC_MINUTES_FILENAME_PATTERN.format(
                        mid=meeting_id, month=month, day=day,
                        year=year, time_suffix=ts,
                    )
                )
        except ValueError:
            pass

    names.append(
        _HPC_MINUTES_FILENAME_SIMPLE.format(mid=meeting_id)
    )

    return names


# ── Motion / vote block parsing ──

# Regex: motion line
# "Motion by {role} {name} to {description}; second by {role} {name}."
_MOTION_LINE_RE = re.compile(
    r"Motion by (?P<mover_role>Chair|Vice Chair|Commissioner)\s+"
    r"(?P<mover_name>\w[\w\s]*?)?\s+"
    r"to\s+(?P<action>.*?);\s+second\s+by\s+"
    r"(?P<seconder_role>Chair|Vice Chair|Commissioner)\s+"
    r"(?P<seconder_name>\w[\w\s]*?)?[.;]",
    re.DOTALL,
)

# Regex: "Motion passed on {aye}-{nay} vote."
_MOTION_RESULT_RE = re.compile(
    r"Motion\s+passed\s+on\s+(?P<aye>\d+)\s*[-–—]\s*(?P<nay>\d+)\s+vote\.",
    re.IGNORECASE,
)

# Regex: "Ayes: ...", "Nays: ...", "Abstain: ...", "Absent: ..."
_AYES_LINE_RE = re.compile(r"Ayes:\s*(.*)", re.IGNORECASE)
_NAYS_LINE_RE = re.compile(r"Nays:\s*(.*)", re.IGNORECASE)
_ABSTAIN_LINE_RE = re.compile(r"Abstain:\s*(.*)", re.IGNORECASE)
_ABSENT_LINE_RE = re.compile(r"Absent:\s*(.*)", re.IGNORECASE)

# Regex: item number like "4A)" or "4)" near the top of a section
_ITEM_NUM_RE = re.compile(r"(\d+[A-Za-z]?)\)\s+(.*)")


def _strip_role(name: str) -> str:
    """Remove role prefix from a voter name, returning just the last name.

    Examples::
        "Chair Jones" -> "Jones"
        "Vice Chair Fackler" -> "Fackler"
        "Commissioner Lerner" -> "Lerner"
        "Commissioners Kurooka" -> "Kurooka" (plural form, rare)
        "Commissioners" -> "" (bare plural artifact with no name)
    """
    name = name.strip()
    for prefix in ("Commissioners ", "Commissioner ", "Vice Chair ", "Chair "):
        if name.startswith(prefix):
            stripped = name[len(prefix):].strip()
            if stripped:
                return stripped
            return ""  # bare prefix only — discard
    # Bare "Commissioners" with no following name
    if name.lower() in ("commissioners", "commissioner"):
        return ""
    return name


def _find_item_number(lines: list[str], motion_line_idx: int) -> str:
    """Scan backwards from a motion line to find the nearest item number.

    Item numbers appear as ``4A)`` or ``3A)`` at the start of a section.
    Returns the item number string, or empty string if none found.
    """
    for i in range(motion_line_idx - 1, max(motion_line_idx - 10, -1), -1):
        if i < 0 or i >= len(lines):
            break
        line = lines[i].strip()
        m = _ITEM_NUM_RE.match(line)
        if m:
            return m.group(1)
        # Also check for simple "4A)" without trailing description
        m2 = re.match(r"^(\d+[A-Za-z]?)\)\s*$", line)
        if m2:
            return m2.group(1)
    return ""


def _parse_voter_list(voter_text: str) -> list[str]:
    """Parse a voter line like ``Ayes: Chair Jones, Vice Chair Fackler, ...``.

    Strips role prefixes, normalizes commas between items, and handles
    line-continuation patterns where names span multiple lines.

    Handles known PDF-extraction artifacts:
    - "Commissioners" appearing alone (artifact from "Commissioner Name")
    - "None" as a placeholder (not a real voter)
    - "and" between names in comma-separated lists
    """
    if not voter_text:
        return []

    # Remove the label prefix ("Ayes:", "Nays:", etc.)
    voter_text = re.sub(r"^(Ayes|Nays|Abstain|Absent):\s*", "", voter_text, flags=re.IGNORECASE)
    voter_text = voter_text.strip().rstrip(";.")

    # Flatten newlines and collapse spaces
    voter_text = voter_text.replace("\n", " ")
    voter_text = re.sub(r"[ \t]+", " ", voter_text)

    # Handle "Commissioners Name" -> "Name" (PDF extraction artifact
    # where "Commissioner Name" becomes "Commissioners" then the name
    # on a separate line, or "Commissioners Name" as one unit)
    voter_text = re.sub(
        r"\bCommissioners\s+(?=[A-Z][a-z])",
        "Commissioner ", voter_text
    )

    # Replace "and" with comma for consistent splitting
    voter_text = re.sub(r"\s+and\s+", ", ", voter_text)

    # Split on commas
    parts: list[str] = []
    for part in re.split(r"\s*,\s*", voter_text):
        part = part.strip().rstrip(";.")
        if not part:
            continue
        parts.append(part)

    # Strip role prefixes and deduplicate, filtering out non-names
    cleaned = []
    seen = set()
    skip_names = {"none", "commissioners", "commissioner"}

    for p in parts:
        name = _strip_role(p)
        name_lower = name.lower().strip()

        # Skip PDF artifacts and non-names
        if not name or name_lower in skip_names:
            continue
        if name_lower in seen:
            continue

        cleaned.append(name)
        seen.add(name_lower)

    return cleaned


def parse_hpc_minutes_text(text: str) -> dict:
    """Parse HPC minutes packet text and extract vote data.

    Returns
    -------
    dict with keys:
        votes : list[dict]
            Each dict has: agenda_item_number, motion_result, vote_text,
            aye, nay, members_aye, members_nay, members_abstain,
            members_absent
        members : list[dict]
            All commissioners who cast votes at this meeting, with
            normalized_name and role.
    """
    text = _normalize_text(text)
    lines = text.split("\n")

    votes: list[dict] = []
    all_members: dict[str, str] = {}  # name_lower -> role

    # Scan for motion blocks using regex on the full text
    # This handles motions that span multiple lines
    for motion_m in _MOTION_LINE_RE.finditer(text):
        motion_start = motion_m.start()

        # Find the line index that this motion starts on
        motion_line_idx = text[:motion_start].count("\n")

        mover_name = motion_m.group("mover_name").strip()
        mover_role = motion_m.group("mover_role")
        action_text = motion_m.group("action").strip()
        seconder_name = motion_m.group("seconder_name").strip() if motion_m.group("seconder_name") else ""

        # Collect motion block text from the motion start to the
        # next blank line, next section heading, or next item number
        block_end = motion_start + 800  # max chars to scan
        if block_end > len(text):
            block_end = len(text)
        block_text = text[motion_start:block_end]

        # Find end of the vote block: stop at blank line after Absent line
        # or after Nays line (whichever comes last)
        block_text_lines = block_text.split("\n")
        truncated = []
        found_nays = False
        for bl in block_text_lines:
            stripped_bl = bl.strip()
            if not stripped_bl:
                if found_nays:
                    break
                truncated.append(bl)
                continue
            # Stop at next section item number
            if re.match(r"^\d+[A-Za-z]?\)\s", stripped_bl):
                break
            # Stop at roll-call result-like lines that aren't part of the vote block
            if re.match(r"^[A-Z][a-z]+:\s", stripped_bl) and \
               not re.match(r"(Ayes|Nays|Abstain|Absent):", stripped_bl, re.IGNORECASE):
                if found_nays:
                    break
            truncated.append(bl)
            if re.match(r"Nays?:?\s+", stripped_bl, re.IGNORECASE):
                found_nays = True

        block_text = "\n".join(truncated)

        # Find item number by scanning backwards
        item_num = _find_item_number(lines, motion_line_idx)

        # Parse result tally: "Motion passed on 9-0 vote."
        result_m = _MOTION_RESULT_RE.search(block_text)
        aye_count = int(result_m.group("aye")) if result_m else 0
        nay_count = int(result_m.group("nay")) if result_m else 0

        # Parse voter lists, merging continuation lines
        # (e.g. "Ayes: ..." followed by "and Davis" on the next line)
        ayes_text = ""
        nays_text = ""
        abstain_text = ""
        absent_text = ""
        for bl in truncated:
            bl_stripped = bl.strip()
            if re.match(r"Ayes?:", bl_stripped, re.IGNORECASE):
                ayes_text = bl_stripped
            elif re.match(r"Nays?:", bl_stripped, re.IGNORECASE):
                nays_text = bl_stripped
            elif re.match(r"Abstain:", bl_stripped, re.IGNORECASE):
                abstain_text = bl_stripped
            elif re.match(r"Absent:", bl_stripped, re.IGNORECASE):
                absent_text = bl_stripped
            elif ayes_text and not re.match(r"(Nays?|Abstain|Absent):", bl_stripped, re.IGNORECASE):
                # Continuation of ayes line (e.g. "and Davis" on next line)
                ayes_text += " " + bl_stripped

        members_aye = _parse_voter_list(ayes_text)
        members_nay = _parse_voter_list(nays_text)
        members_abstain = _parse_voter_list(abstain_text)
        members_absent = _parse_voter_list(absent_text)

        # Build vote text for display
        vote_text = block_text[:300].replace("\n", " ")

        # Determine motion result
        if nay_count == 0:
            motion_result = "approved"
        elif aye_count == 0:
            motion_result = "denied"
        elif aye_count > nay_count:
            motion_result = "approved"
        else:
            motion_result = "denied"

        # Track all members for this meeting
        for name in members_aye + members_nay + members_abstain + members_absent:
            all_members[name.lower()] = _infer_hpc_role(name, ayes_text + nays_text + block_text)

        vote_record = {
            "agenda_item_number": item_num,
            "motion_result": motion_result,
            "vote_text": vote_text,
            "aye": aye_count,
            "nay": nay_count,
            "members_aye": members_aye,
            "members_nay": members_nay,
            "members_abstain": members_abstain,
            "members_absent": members_absent,
        }
        votes.append(vote_record)

    # Build member list
    members_out = []
    for name_lower, role in sorted(all_members.items()):
        name_cap = name_lower.capitalize()
        members_out.append({
            "name": name_cap,
            "normalized_name": name_lower,
            "role": role,
        })

    log.info("HPC minutes: %d vote items, %d members", len(votes), len(members_out))
    return {"votes": votes, "members": members_out}


def _infer_hpc_role(name: str, context: str) -> str:
    """Infer HPC role (Chair, Vice Chair, Commissioner) from context.

    Looks for ``Chair {name}`` or ``Vice Chair {name}`` or
    ``Commissioner {name}`` in the surrounding text.
    """
    name_lower = name.lower()
    # Check for "Chair {name}" (but NOT "Vice Chair {name}")
    if re.search(rf"\bChair\s+{re.escape(name_lower)}\b", context, re.IGNORECASE):
        # Make sure it's not "Vice Chair {name}"
        if not re.search(rf"\bVice\s+Chair\s+{re.escape(name_lower)}\b", context, re.IGNORECASE):
            return "Chair"
    if re.search(rf"\bVice\s+Chair\s+{re.escape(name_lower)}\b", context, re.IGNORECASE):
        return "Vice Chair"
    return "Commissioner"


# ── Document fetching ──


def fetch_and_parse_hpc_minutes(meeting_id: int, meeting_date: str = "") -> dict:
    """Download and parse the HPC Minutes Packet PDF for a meeting.

    Parameters
    ----------
    meeting_id : int
        OnBase meeting ID.
    meeting_date : str, optional
        Date in YYYY-MM-DD format. Used to construct the document name.

    Returns
    -------
    dict with keys ``members`` and ``votes``, or empty lists on failure.
    """
    from scraper.onbase import TEMPE_CONFIG, download_document

    candidates = _minutes_document_names(meeting_id, meeting_date)

    for name in candidates:
        try:
            pdf_bytes = download_document(TEMPE_CONFIG, meeting_id, name, doc_type=6)
            if pdf_bytes and len(pdf_bytes) >= 1000 and not pdf_bytes[:5] == b"<!DOC":
                log.info("HPC Minutes PDF found: %s (%d bytes)", name, len(pdf_bytes))
                break
        except Exception:
            continue
    else:
        log.info("No HPC minutes packet available for meeting %d", meeting_id)
        return {"members": [], "votes": []}

    text = _extract_pdf_text(pdf_bytes)
    if not text or not text.strip():
        return {"members": [], "votes": []}

    return parse_hpc_minutes_text(text)


# ── Backfill ──


def backfill_hpc_votes(dry_run: bool = True, limit: int = 0,
                        verbose: bool = True) -> dict:
    """Backfill HPC vote data for meetings missing minutes-packet votes.

    Queries all HPC meetings that have been fully synced
    (sync_status='complete') with agenda items but have no vote records.
    Attempts to fetch and parse their Minutes Packet PDFs from OnBase.

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
        attempted, found_votes, no_minutes, errors, total_meetings_without_votes
    """
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes
    from sqlalchemy import select, func

    session = get_session()

    # Find meetings that already have votes
    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )

    HPC_BODIES = ("tempe-hpc",)

    rows = session.execute(
        select(
            MeetingModel.id,
            MeetingModel.meeting_id,
            MeetingModel.meeting_date,
            MeetingModel.meeting_type,
            MeetingModel.body,
        )
        .where(MeetingModel.body.in_(HPC_BODIES))
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
            print("  (no HPC meetings without votes)")
        return {
            "attempted": 0, "found_votes": 0, "no_minutes": 0, "errors": 0,
            "details": [],
        }

    # Filter to past meetings only
    past_rows = [r for r in rows if r.meeting_date and r.meeting_date < datetime.date.today().isoformat()]
    if limit:
        past_rows = past_rows[:limit]

    if not past_rows:
        if verbose:
            print("  (no past HPC meetings without votes)")
        return {
            "attempted": 0, "found_votes": 0, "no_minutes": 0, "errors": 0,
            "details": [],
        }

    details: list[dict] = []
    counts = {"found": 0, "no_minutes": 0, "errors": 0}

    for idx, row in enumerate(past_rows, 1):
        meeting_db_id = row.id
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        body_code = row.body

        try:
            vote_data = fetch_and_parse_hpc_minutes(
                int(meeting_id), meeting_date,
            )
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

        num_votes = len(vote_data.get("votes", []))

        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    # Convert HPC vote format to what persist_votes expects.
                    # persist_votes expects "supervisors" list (with name,
                    # normalized_name) and "votes" list (with agenda_item_number,
                    # motion_result, vote_text, and optionally supervisor_votes
                    # containing per-member aye/nay).
                    supervisors = vote_data.get("members", [])

                    # Build per-member vote details for each recorded vote
                    for v in vote_data["votes"]:
                        sup_votes = []
                        for name in v.get("members_aye", []):
                            sup_votes.append({"name": name, "vote": "aye"})
                        for name in v.get("members_nay", []):
                            sup_votes.append({"name": name, "vote": "nay"})
                        v["supervisor_votes"] = sup_votes

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

            mem_names = [s.get("name", "") for s in vote_data.get("members", [])]
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes"
                      f" ({', '.join(mem_names)})")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "votes_found",
                "vote_count": num_votes,
                "members": mem_names,
            })
        else:
            counts["no_minutes"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no minutes packet")
            details.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "status": "no_minutes",
            })

    report = {
        "attempted": len(past_rows),
        "found_votes": counts["found"],
        "no_minutes": counts["no_minutes"],
        "errors": counts["errors"],
        "details": details,
    }
    return report


# ── Convenience wrapper for use in main.py sync handler ──


def extract_hpc_votes(meeting_id: int, meeting_date: str = "",
                       meeting_type: str = "") -> dict:
    """Fetch and parse HPC minutes packet, returning vote data.

    This is the standard entry point called from the Tempe sync handler
    in main.py.

    Parameters
    ----------
    meeting_id : int
        OnBase meeting ID.
    meeting_date : str
        Date in YYYY-MM-DD format.
    meeting_type : str
        Meeting type (ignored for HPC — all Regular meetings use minutes).

    Returns
    -------
    dict with keys ``members`` and ``votes``.
    """
    return fetch_and_parse_hpc_minutes(meeting_id, meeting_date)


# ═══════════════════════════════════════════════════════════════
#  CLI entry point (for standalone testing)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python tempe_hpc_summary.py <meeting_id> [meeting_date]")
        print("  meeting_date is optional YYYY-MM-DD (helps construct filenames)")
        sys.exit(1)

    mid = int(sys.argv[1])
    mdate = sys.argv[2] if len(sys.argv) > 2 else ""

    result = fetch_and_parse_hpc_minutes(mid, mdate)
    print(f"\nMeeting {mid} ({mdate or 'no date'}):")
    print(f"  Votes found: {len(result.get('votes', []))}")
    print(f"  Members: {[m.get('name', '') for m in result.get('members', [])]}")

    for v in result.get("votes", []):
        print(f"\n  Item {v['agenda_item_number']}: {v['motion_result']}")
        print(f"    Aye: {v['aye']}, Nay: {v['nay']}")
        if v.get("members_aye"):
            print(f"    Ayes: {', '.join(v['members_aye'])}")
        if v.get("members_nay"):
            print(f"    Nays: {', '.join(v['members_nay'])}")
