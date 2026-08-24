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

from scraper.jurisdictions.tempe._parsing import normalize_text, extract_pdf_text

log = logging.getLogger(__name__)

# ── Result keywords (end-of-item action markers) ──

RESULT_KEYWORDS = frozenset({
    "APPROVED", "PASS", "ACCEPTED", "ADOPTED", "RATIFIED",
    "DENIED", "FAILED", "WITHDRAWN",
})

# ── Parsing ──


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
        for_start = m.end()
        if tally_m:
            for_start = tally_m.end()
        remainder = text[for_start:for_start + 500]

        voters_m = re.search(
            r"For:\s*(.+?)(?:\n(?!\s*(?:Councilmember|Vice Mayor|Mayor|"
            r"Amberg|Chin|Hodge|Keating|Adams|Woods|Garlid))|\n{2,}|$)",
            remainder, re.DOTALL)
        voters = []
        if voters_m:
            raw = voters_m.group(1).strip()
            raw = raw.replace("\n", " ")
            raw = re.split(r"\nRegular|\n\d+", raw)[0].strip().rstrip(";")
            for part in re.split(r"\s*,\s*", raw):
                part = part.strip().rstrip(";")
                if not part:
                    continue
                name = re.sub(r"^(Mayor|Vice Mayor|Councilmember)\s+", "", part).strip()
                if name and name not in ("Councilmember", "Vice Mayor", "Mayor"):
                    voters.append(name)

        # Deduplicate voter blocks by item set
        dedup_key = (tuple(sorted(item_numbers)), m.group("mover"))
        if any(b.get("_dedup_key") == dedup_key for b in blocks):
            continue

        blocks.append({
            "item_numbers": item_numbers,
            "mover": m.group("mover"),
            "seconder": m.group("seconder") or "",
            "aye": aye,
            "nay": nay,
            "voters": list(dict.fromkeys(voters)),
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
            # Skip section header lines
            if scan_line in ("TEMPE CITY COUNCIL", "LEGAL ACTION SUMMARY") or \
               scan_line.startswith("Regular City Council Meeting"):
                continue
            if scan_line.startswith("Motion to") or scan_line.startswith("Aye:") or \
               scan_line.startswith("For:") or scan_line.startswith("Mayor ") or \
               scan_line.startswith("Vice Mayor") or scan_line.startswith("Councilmember"):
                continue
            if scan_line in RESULT_KEYWORDS:
                item_map[num]["result"] = scan_line
                break
            if scan_line == "NO ITEMS":
                item_map[num]["result"] = "no_action"
                break
            if scan_line.startswith("NOTE:"):
                continue
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


def parse_summary_text(text: str,
                       role_titles: Optional[dict[str, str]] = None) -> dict:
    """Parse a Tempe Legal Action Summary PDF text and return structured data.

    Parameters
    ----------
    text : str
        Normalized summary PDF text.
    role_titles : dict, optional
        Override mapping for normalized role names
        (e.g. ``{"woods": "Chair"}`` for non-CC bodies).

    Returns
    -------
    dict with keys:
        supervisors : list[dict]
        votes : list[dict]
    """
    text = normalize_text(text)

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
            result_map = {
                "approved": "approved", "pass": "approved", "accepted": "approved",
                "adopted": "approved", "ratified": "approved",
                "denied": "denied", "failed": "denied",
                "withdrawn": "withdrawn",
            }
            motion_result = result_map.get(result, result or "approved")

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
            votes_out.append({
                "agenda_item_number": num,
                "motion_result": result,
                "vote_text": "",
                "supervisor_votes": [],
                "c_number": "",
                "c_number_base": "",
            })

    # Build supervisor list
    role_fn = _infer_role
    if role_titles:
        def _role_with_overrides(name: str) -> str:
            key = name.lower().strip()
            return role_titles.get(key, _infer_role(name))
        role_fn = _role_with_overrides

    supervisors_out: list[dict] = []
    for voter_name in sorted(seen_voters):
        name_clean = voter_name.capitalize()
        supervisors_out.append({
            "name": name_clean,
            "normalized_name": voter_name.lower(),
            "role": role_fn(voter_name),
            "present": True,
        })

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
    """Ensure Tempe council members have BodyMembership rows."""
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
    from scraper.platforms.onbase import TEMPE_CONFIG, download_document

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

    text = extract_pdf_text(pdf_bytes)
    if not text:
        return {"supervisors": [], "votes": []}

    return parse_summary_text(text)


def _summary_document_names(meeting_id: int, meeting_date: str = "",
                            meeting_type: str = "") -> list[str]:
    """Return candidate document names for the Council summary PDF."""
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
