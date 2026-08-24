"""Parse Tempe Board of Adjustment (BOA) Meeting Summary PDFs for vote data.

BOA publishes Legal Action Summary PDFs (doctype=3) with results inline
at the end of item description lines. Vote tallies are aggregate only
(no individual member names).

Example format::

    2A. Board of Adjustment - 1/28/26 Study Session   APPROVED
    3A. Request Variance ...  APPROVED WITH MODIFIED CONDITIONS (7-0)
    5A. Appeal ...  APPEAL DENIED (7/0)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from scraper.jurisdictions.tempe._parsing import extract_pdf_text

log = logging.getLogger(__name__)

# ── OnBase document filename patterns ──


def boa_summary_document_names(meeting_id: int, meeting_date: str = "") -> list[str]:
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
            for ts in ("6_00_00_PM", "5_30_00_PM"):
                names.append(
                    f"Board_of_Adjustment_Regular_Meeting_{meeting_id}_Summary_"
                    f"{date.month}_{date.day}_{date.year}_{ts}.pdf"
                )
        except ValueError:
            pass
    return names


# ── Header/section constants ──

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

_BOA_VOTE_TALLY = re.compile(
    r"\((?P<aye>\d+)\s*[-/]\s*(?P<nay>\d+)"
    r"(?:\s*VOTE)?"
    r"(?:\s*,\s*(?P<abstain>\d+)\s*ABST(?:AIN|ENTION))?\)",
    re.IGNORECASE,
)

_ITEM_NUMBER_RE = re.compile(r"^(\d+[A-Za-z]?)\.\s+(.*)")


# ── BOA summary parser ──


def _is_boa_section_heading(text: str) -> bool:
    """Check if a line is a BOA section heading (all-caps, not actionable)."""
    cleaned = text.strip().rstrip(".").strip()
    if not cleaned:
        return True
    up = cleaned.upper()
    for heading in _BOA_SECTION_HEADINGS:
        if up.startswith(heading):
            return True
        if heading.startswith(up):
            return True
    return False


def _extract_boa_result(description_text: str) -> Optional[dict]:
    """Extract result keyword and optional vote tally from BOA item description.

    Returns dict with keys: motion_result, vote_text, aye, nay, abstain.
    Returns None if no result found.
    """
    flat = description_text.replace("\n", " ").strip()
    if not flat:
        return None

    if flat.rstrip(".").endswith(" - NONE"):
        return None

    up = flat.upper()

    best_idx = len(up) + 1
    best_kw = None
    for kw in _BOA_RESULT_KEYWORDS:
        idx = up.find(kw)
        if idx >= 0 and idx < best_idx:
            best_idx = idx
            best_kw = kw

    if best_kw is None:
        return None

    result_text = flat[best_idx:].strip().rstrip(".").rstrip()

    tally_m = _BOA_VOTE_TALLY.search(result_text)
    aye = int(tally_m.group("aye")) if tally_m else 0
    nay = int(tally_m.group("nay")) if tally_m else 0
    abstain = int(tally_m.group("abstain")) if tally_m and tally_m.group("abstain") else 0

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

    Returns a list of dicts:
        {agenda_item_number, motion_result, vote_text, aye, nay, abstain}
    """
    lines = text.split("\n")
    items: list[dict] = []

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
    page_header_patterns = (
        "Board of Adjustment Regular Meeting Meeting Summary",
        "Wednesday, April", "Thursday, April", "Wednesday, March",
        "Thursday, March", "Wednesday, February", "Thursday, February",
        "Wednesday, January", "Thursday, January",
    )
    _PAGE_NUM_RE = re.compile(r"^\d+$")

    current_item: Optional[str] = None
    description_lines: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Skip header lines (first 20 lines)
        if i < 20:
            if stripped in header_lines:
                continue
            if any(stripped.startswith(p) for p in header_start_patterns):
                continue
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
                    items.append({"agenda_item_number": current_item, **result})
            elif current_item:
                # Single-line item
                result = _extract_boa_result(rest)
                if result:
                    items.append({"agenda_item_number": current_item, **result})

            current_item = item_num
            description_lines = []

            if _is_boa_section_heading(rest.rstrip(".").strip()):
                current_item = None
                continue

            description_lines.append(rest)
            continue

        # Skip page-break headers and page numbers
        if any(stripped.startswith(p) for p in page_header_patterns):
            continue
        if _PAGE_NUM_RE.match(stripped):
            continue

        if current_item is not None:
            description_lines.append(stripped)

    # Finalize last item
    if current_item and description_lines:
        desc_text = "\n".join(description_lines)
        result = _extract_boa_result(desc_text)
        if result:
            items.append({"agenda_item_number": current_item, **result})

    return items


# ── Convenience wrapper ──


def extract_boa_votes(meeting_id: int, meeting_date: str = "",
                      meeting_type: str = "") -> dict:
    """Fetch and parse BOA meeting summary PDF, returning vote data.

    Returns dict with ``votes`` key (no individual member names).
    """
    from scraper.platforms.onbase import TEMPE_CONFIG, download_document

    candidates = boa_summary_document_names(meeting_id, meeting_date)
    pdf_bytes = _download_any(TEMPE_CONFIG, meeting_id, candidates)
    if not pdf_bytes:
        return {"supervisors": [], "votes": []}

    text = extract_pdf_text(pdf_bytes)
    if not text or not text.strip():
        return {"supervisors": [], "votes": []}

    items = extract_boa_items_from_summary(text)
    log.info("BOA summary for meeting %d: %d items with results", meeting_id, len(items))
    return {"supervisors": [], "votes": items}


def _download_any(config, meeting_id, candidates):
    """Try each candidate document name until one succeeds."""
    from scraper.platforms.onbase import download_document
    for name in candidates:
        try:
            pdf_bytes = download_document(config, meeting_id, name, doc_type=3)
            if pdf_bytes and len(pdf_bytes) >= 1000 and not pdf_bytes[:5] == b"<!DOC":
                return pdf_bytes
        except Exception:
            continue
    return None


def backfill_boa_votes(dry_run: bool = True, limit: int = 0,
                        verbose: bool = True) -> dict:
    """Backfill BOA vote data for Regular meetings missing summary votes."""
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes
    from sqlalchemy import select, func

    session = get_session()
    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )

    rows = session.execute(
        select(MeetingModel.id, MeetingModel.meeting_id, MeetingModel.meeting_date,
               MeetingModel.meeting_type, MeetingModel.body)
        .where(MeetingModel.body.in_(("tempe-boa",)))
        .where(MeetingModel.meeting_type.ilike("%Regular%"))
        .where(MeetingModel.sync_status == "complete")
        .where(MeetingModel.item_count_actual > 0)
        .where(~MeetingModel.id.in_(meetings_with_votes) | MeetingModel.id.is_(None))
        .order_by(MeetingModel.meeting_date)
    ).all()
    session.close()

    if not rows:
        if verbose:
            print("  (no BOA meetings without votes)")
        return {"attempted": 0, "found_votes": 0, "no_summary": 0, "errors": 0, "details": []}

    past_rows = [r for r in rows if r.meeting_date and r.meeting_date < "2026-06-01"]
    if limit:
        past_rows = past_rows[:limit]

    details: list[dict] = []
    counts = {"found": 0, "no_summary": 0, "errors": 0}

    for idx, row in enumerate(past_rows, 1):
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        body_code = row.body

        try:
            vote_data = extract_boa_votes(int(meeting_id), meeting_date)
        except Exception as e:
            counts["errors"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: ERROR - {e}")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                            "status": "error", "error": str(e)[:200]})
            continue

        num_votes = len(vote_data["votes"])
        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    persist_votes(session, body_code, meeting_id, [],
                                  vote_data["votes"])
                    session.commit()
                except Exception as e:
                    session.rollback()
                    counts["errors"] += 1
                    if verbose:
                        print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: PERSIST ERROR - {e}")
                    details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                                    "status": "persist_error", "error": str(e)[:200]})
                    continue
                finally:
                    session.close()
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                            "status": "votes_found", "vote_count": num_votes})
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no summary")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "no_summary"})

    return {"attempted": len(past_rows), "found_votes": counts["found"],
            "no_summary": counts["no_summary"], "errors": counts["errors"], "details": details}
