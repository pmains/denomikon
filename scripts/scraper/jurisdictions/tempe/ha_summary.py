"""Parse Tempe Housing Authority (HA) Meeting Summary PDFs for vote data.

HA publishes Legal Action Summary PDFs (doctype=3) that use the same
City Council-style format (roll-call motions with individual member names).
Role titles differ: "Board Member" ↔ "Councilmember", "Chair" ↔ "Mayor",
"Vice Chair" ↔ "Vice Mayor". We normalize before parsing.

Example vote block::

    Motion to Approve Item 3A made by Board Member Adams and seconded by
    Board Member Chin
    Aye: 7; Nay: 0; Abstain: 0; Absent: 0; Recused: 0;
    For: Chair Smith, Vice Chair Jones, Board Member Adams, ...
"""

from __future__ import annotations

import logging
import re

from scraper.jurisdictions.tempe._parsing import extract_pdf_text
from scraper.jurisdictions.tempe.council_summary import parse_summary_text

log = logging.getLogger(__name__)


def ha_summary_document_names(meeting_id: int) -> list[str]:
    """Return candidate document names for HA Summary PDF."""
    return [
        f"Tempe_Housing_Authority_{meeting_id}_Summary.pdf",
        f"Tempe_Housing_Authority_Meeting_{meeting_id}_Summary.pdf",
    ]


def _normalize_ha_text(text: str) -> str:
    """Normalize HA role titles to City Council equivalents."""
    text = re.sub(r"\bBoard Member\b", "Councilmember", text)
    text = re.sub(r"\bVice Chair\b", "Vice Mayor", text)
    text = re.sub(r"(?<!Vice )\bChair\b", "Mayor", text)
    text = re.sub(r"\bResident[\s\n]+Member\b", "Councilmember", text)
    return text


def extract_ha_votes(meeting_id: int, meeting_date: str = "",
                     meeting_type: str = "") -> dict:
    """Fetch and parse HA meeting summary PDF, returning vote data.

    Returns dict with ``supervisors`` and ``votes`` keys (CC-style roll-call).
    """
    from scraper.platforms.onbase import TEMPE_CONFIG, download_document

    candidates = ha_summary_document_names(meeting_id)
    pdf_bytes = None
    for name in candidates:
        try:
            pdf_bytes = download_document(TEMPE_CONFIG, meeting_id, name, doc_type=3)
            if pdf_bytes and len(pdf_bytes) >= 1000 and not pdf_bytes[:5] == b"<!DOC":
                log.info("HA Summary PDF found: %s (%d bytes)", name, len(pdf_bytes))
                break
            pdf_bytes = None
        except Exception:
            continue

    if not pdf_bytes:
        log.info("No HA summary PDF available for meeting %d", meeting_id)
        return {"supervisors": [], "votes": []}

    text = extract_pdf_text(pdf_bytes)
    if not text or not text.strip():
        return {"supervisors": [], "votes": []}

    normalized = _normalize_ha_text(text)
    return parse_summary_text(normalized)


def backfill_ha_votes(dry_run: bool = True, limit: int = 0,
                       verbose: bool = True) -> dict:
    """Backfill HA vote data for meetings missing summary votes."""
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
        .where(MeetingModel.body.in_(("tempe-ha",)))
        .where(MeetingModel.sync_status == "complete")
        .where(MeetingModel.item_count_actual > 0)
        .where(~MeetingModel.id.in_(meetings_with_votes) | MeetingModel.id.is_(None))
        .order_by(MeetingModel.meeting_date)
    ).all()
    session.close()

    if not rows:
        if verbose:
            print("  (no HA meetings without votes)")
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
            vote_data = extract_ha_votes(int(meeting_id))
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
                    supervisors = vote_data.get("supervisors", [])
                    persist_votes(session, body_code, meeting_id,
                                  supervisors, vote_data["votes"])
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

            sup_names = [s.get("name", "") for s in vote_data.get("supervisors", [])]
            if verbose:
                sup_str = f" ({', '.join(sup_names)})" if sup_names else ""
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes{sup_str}")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                            "status": "votes_found", "vote_count": num_votes,
                            "supervisors": sup_names if sup_names else []})
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no summary")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "no_summary"})

    return {"attempted": len(past_rows), "found_votes": counts["found"],
            "no_summary": counts["no_summary"], "errors": counts["errors"], "details": details}
