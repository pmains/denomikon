"""queries module."""

import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, select, text, or_, case, and_
from sqlalchemy.orm import Session

from db.models import (Person, Supervisor, Meeting, MeetingSupervisor,
    AgendaItem, AgendaItemVote, SupervisorVote, MeetingAttendance,
    ExecutiveSessionParticipant, BodyMembership, PublicBody, Jurisdiction,
    BodySeat)
from db.core import get_session
from db.votes import _normalize_vote_value, detect_controversy_flags

def _resolve_jurisdiction_id(session: Session, body: str) -> Optional[int]:
    """Resolve a meeting's jurisdiction_id from its public body code."""
    pb = session.execute(
        select(PublicBody).where(PublicBody.body_code == body)
    ).scalar_one_or_none()
    if pb:
        return pb.jurisdiction_id
    return None

def get_meetings_by_date_range(
    session: Session,
    body: str,
    start_date_iso: str,
    end_date_iso: str,
) -> list[Meeting]:
    """Get all meetings for a body with meeting_date in the given ISO date range (inclusive)."""
    q = (
        select(Meeting)
        .where(Meeting.body == body)
        .where(Meeting.meeting_date >= start_date_iso)
        .where(Meeting.meeting_date <= end_date_iso)
        .order_by(Meeting.meeting_date, Meeting.meeting_id)
    )
    return list(session.execute(q).scalars().all())

def get_meetings_by_status(
    session: Session,
    body: str,
    statuses: Optional[list[str]] = None,
    *,
    force: bool = False,
    meeting_ids: Optional[list[str]] = None,
) -> list[Meeting]:
    """Get meetings for a body filtered by sync_status and/or meeting_ids.

    If force is True, ignore status filter and return all matching meeting_ids.
    """
    q = select(Meeting).where(Meeting.body == body).order_by(Meeting.meeting_date, Meeting.meeting_id)
    if meeting_ids:
        q = q.where(Meeting.meeting_id.in_(meeting_ids))
    if not force and statuses:
        q = q.where(Meeting.sync_status.in_(statuses))
    return list(session.execute(q).scalars().all())

def get_sync_status_summary(session: Session) -> dict:
    """Get counts of meetings by sync_status."""
    rows = session.execute(
        select(
            Meeting.sync_status,
            func.count(Meeting.id).label("count"),
        )
        .group_by(Meeting.sync_status)
    ).all()

    summary = {
        "complete": 0,
        "partial": 0,
        "failed": 0,
        "pending": 0,
        "total": 0,
        "total_items": 0,
        "total_docs": 0,
    }
    for row in rows:
        status = row.sync_status or "pending"
        summary[status] = row.count
        summary["total"] += row.count

    # Aggregate item and doc counts
    agg = session.execute(
        select(
            func.coalesce(func.sum(Meeting.item_count_actual), 0),
            func.coalesce(func.sum(Meeting.supporting_doc_count), 0),
        )
    ).one()
    summary["total_items"] = agg[0]
    summary["total_docs"] = agg[1]

    return summary

def get_failed_meetings(session: Session, body: str = "bos") -> list[Meeting]:
    """Get meetings with failed or partial status (excludes manual_review)."""
    return get_meetings_by_status(session, body, ["failed", "partial"], force=False)

def get_meeting_attendance(
    session: Session,
    body: str,
    meeting_id: str,
) -> list["MeetingAttendance"]:
    """Get attendance records for a meeting."""
    rows = session.execute(
        select(MeetingAttendance).where(
            MeetingAttendance.body == body,
            MeetingAttendance.meeting_id == meeting_id,
        )
    ).scalars().all()
    return list(rows)

def get_executive_session_participants(
    session: Session,
    body: Optional[str] = None,
    meeting_id: Optional[str] = None,
    person_name: Optional[str] = None,
) -> list["ExecutiveSessionParticipant"]:
    """Get executive session participation records."""
    q = select(ExecutiveSessionParticipant)
    if body:
        q = q.where(ExecutiveSessionParticipant.body == body)
    if meeting_id:
        q = q.where(ExecutiveSessionParticipant.meeting_id == meeting_id)
    if person_name:
        q = q.where(ExecutiveSessionParticipant.normalized_name == person_name.lower())
    q = q.order_by(ExecutiveSessionParticipant.meeting_id)
    rows = session.execute(q).scalars().all()
    return list(rows)

def get_split_votes(
    session: Session,
    body: Optional[str] = None,
) -> list["AgendaItemVote"]:
    """Get all split votes, optionally filtered by body."""
    q = select(AgendaItemVote).where(AgendaItemVote.is_split_vote == True)
    if body and body.lower() != "all":
        q = q.where(AgendaItemVote.body == body)
    q = q.order_by(AgendaItemVote.meeting_id, AgendaItemVote.agenda_item_number)
    rows = session.execute(q).scalars().all()
    return list(rows)

def get_dissenting_votes(
    session: Session,
    member_name: Optional[str] = None,
) -> list["SupervisorVote"]:
    """Get dissent votes, optionally filtered by member name."""
    from sqlalchemy import join as sa_join
    q = select(SupervisorVote).where(SupervisorVote.is_dissent == True)
    if member_name:
        norm = member_name.lower().strip()
        q = (
            select(SupervisorVote)
            .join(Supervisor, SupervisorVote.supervisor_id == Supervisor.id)
            .where(
                SupervisorVote.is_dissent == True,
                Supervisor.normalized_name.ilike(f"%{norm}%"),
            )
        )
    rows = session.execute(q).scalars().all()
    return list(rows)

def get_supervisor_by_slug_or_name(
    session: Session,
    slug_or_name: str,
) -> Optional[Supervisor]:
    """Look up a supervisor by URL slug or name.

    Slugs use the normalized_name field with hyphens instead of spaces.
    Falls back to partial name matching for flexibility.
    """
    from sqlalchemy import or_

    # Try slug: hyphen → space, match normalized_name exactly
    maybe_norm = slug_or_name.replace("-", " ").strip()
    sup = session.execute(
        select(Supervisor).where(Supervisor.normalized_name == maybe_norm)
    ).scalar_one_or_none()
    if sup:
        return sup

    # Try name match with noise filtering (shortest = most canonical)
    matches = session.execute(
        select(Supervisor)
        .where(
            or_(
                Supervisor.name.ilike(f"%{maybe_norm}%"),
                Supervisor.normalized_name.ilike(f"%{maybe_norm}%"),
            )
        )
        .order_by(func.length(Supervisor.name))
    ).scalars().all()
    for candidate in matches:
        if len(candidate.name) < 40 and not re.search(r"\d", candidate.name):
            return candidate

    return None

def get_bos_supervisors(session: Session) -> list[Supervisor]:
    """Get BOS supervisors with meaningful attendance (≥5 meeting appearances).

    Uses meeting_supervisors (body='bos') to identify actual supervisors
    versus noise rows from vote-parsing artifacts.
    """
    rows = session.execute(
        select(Supervisor, func.count(MeetingSupervisor.id).label("attendance"))
        .join(
            MeetingSupervisor,
            MeetingSupervisor.supervisor_id == Supervisor.id,
        )
        .where(MeetingSupervisor.body == "bos")
        .group_by(Supervisor.id)
        .having(func.count(MeetingSupervisor.id) >= 5)
        .order_by(Supervisor.name)
    ).all()
    # Also filter out obvious noise (long names, digits)
    result = []
    for sup, _att in rows:
        if len(sup.name) < 40 and not re.search(r"\d", sup.name):
            result.append(sup)
    return result

def get_supervisor_vote_stats(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> dict:
    """Get aggregated voting statistics for a supervisor.

    Returns a dict with:
        total_votes, yes, no, abstain, absences,
        split_votes_attended, with_majority, against_majority,
        attendance_rate, attendance_present, attendance_absent
    """
    from collections import Counter

    # --- 1. Raw vote counts (filtered by body scope on AgendaItemVote) ---
    rows = session.execute(
        select(SupervisorVote.vote, AgendaItemVote.id.label("aiv_id"))
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    total_votes = len(rows)
    norm_counts: Counter = Counter()
    aiv_ids: set[int] = set()
    for row in rows:
        norm_counts[_normalize_vote_value(row.vote)] += 1
        aiv_ids.add(row.aiv_id)

    yes_count = norm_counts.get("yes", 0)
    no_count = norm_counts.get("no", 0)
    abstain_count = norm_counts.get("abstain", 0)

    # --- 2. Vote-level attributes (split, majority) from other members ---
    # Fetch all votes on the same AIVs from ALL supervisors
    aiv_id_list = list(aiv_ids)
    if aiv_id_list:
        all_votes = session.execute(
            select(
                SupervisorVote.agenda_item_vote_id,
                SupervisorVote.supervisor_id,
                SupervisorVote.vote,
            )
            .where(SupervisorVote.agenda_item_vote_id.in_(aiv_id_list))
        ).all()
    else:
        all_votes = []

    # Build per-AIV data
    aiv_votes: dict[int, dict[str, list[int]]] = {}
    for av in all_votes:
        aiv_votes.setdefault(av.agenda_item_vote_id, {"yes": [], "no": []})
        nv = _normalize_vote_value(av.vote)
        if nv == "yes":
            aiv_votes[av.agenda_item_vote_id]["yes"].append(av.supervisor_id)
        elif nv == "no":
            aiv_votes[av.agenda_item_vote_id]["no"].append(av.supervisor_id)

    split_count = 0
    with_maj = 0
    against_maj = 0

    for aiv_id, vd in aiv_votes.items():
        yes_sup = len(vd["yes"])
        no_sup = len(vd["no"])
        is_split = yes_sup > 0 and no_sup > 0
        if not is_split:
            continue
        split_count += 1
        # Determine majority
        if yes_sup > no_sup:
            majority = "yes"
        elif no_sup > yes_sup:
            majority = "no"
        else:
            majority = "tie"
        # Check where this supervisor voted
        # Re-find the supervisor's vote for this AIV
        sup_nv = None
        for av in all_votes:
            if av.agenda_item_vote_id == aiv_id and av.supervisor_id == sup_id:
                sup_nv = _normalize_vote_value(av.vote)
                break
        if sup_nv is None:
            continue
        if majority == "tie":
            continue
        if sup_nv == majority:
            with_maj += 1
        elif sup_nv in ("yes", "no"):
            against_maj += 1

    # --- 3. Attendance ---
    present = session.execute(
        select(func.count())
        .select_from(MeetingSupervisor)
        .where(
            MeetingSupervisor.supervisor_id == sup_id,
            MeetingSupervisor.body == body,
            MeetingSupervisor.present == True,
        )
    ).scalar() or 0

    absent = session.execute(
        select(func.count())
        .select_from(MeetingSupervisor)
        .where(
            MeetingSupervisor.supervisor_id == sup_id,
            MeetingSupervisor.body == body,
            MeetingSupervisor.present == False,
        )
    ).scalar() or 0

    total_meetings = present + absent
    attendance_rate = round(present / total_meetings, 4) if total_meetings > 0 else None

    return {
        "total_votes": total_votes,
        "yes": yes_count,
        "no": no_count,
        "abstain": abstain_count,
        "absences": absent,
        "attendance_present": present,
        "attendance_absent": absent,
        "attendance_rate": attendance_rate,
        "split_votes_attended": split_count,
        "with_majority": with_maj,
        "against_majority": against_maj,
        "dissent_rate": round(against_maj / split_count, 4) if split_count > 0 else None,
    }

def get_supervisor_split_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get all split votes involving this supervisor.

    Returns list of dicts with:
        meeting_id, meeting_date, meeting_type, agenda_item_number,
        agenda_item_title, c_number, supervisor_vote,
        motion_result, majority_position, with_or_against_majority
    """
    # 1. Get all AIV IDs this supervisor voted on (body-scoped)
    my_aiv_rows = session.execute(
        select(SupervisorVote.agenda_item_vote_id, SupervisorVote.vote)
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    if not my_aiv_rows:
        return []

    sup_vote_map = {av.agenda_item_vote_id: _normalize_vote_value(av.vote) for av in my_aiv_rows}
    aiv_ids = list(sup_vote_map.keys())

    # 2. Get all votes on these AIVs (all supervisors)
    all_votes = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    # Group by aiv
    aiv_group: dict[int, list[str]] = {}
    for r in all_votes:
        aiv_group.setdefault(r.agenda_item_vote_id, []).append(
            _normalize_vote_value(r.vote)
        )

    # Identify split AIVs: both yes and no votes present
    split_aiv_ids: set[int] = set()
    aiv_majority: dict[int, Optional[str]] = {}
    for aiv_id, votes in aiv_group.items():
        vote_set = set(votes)
        if "yes" in vote_set and "no" in vote_set:
            split_aiv_ids.add(aiv_id)
            yes_cnt = sum(1 for v in votes if v == "yes")
            no_cnt = sum(1 for v in votes if v == "no")
            if yes_cnt > no_cnt:
                aiv_majority[aiv_id] = "yes"
            elif no_cnt > yes_cnt:
                aiv_majority[aiv_id] = "no"
            else:
                aiv_majority[aiv_id] = "tie"

    if not split_aiv_ids:
        return []

    # 3. Fetch AIV details + meeting + agenda item info for split AIVs (raw SQL to avoid ORM ambiguity)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in split_aiv_ids)
    rows = session.execute(
        sa_text(f"""
            SELECT
                aiv.meeting_id,
                aiv.agenda_item_number,
                aiv.c_number,
                aiv.motion_result,
                ai.agenda_item_title,
                m.meeting_date,
                m.meeting_type
            FROM agenda_item_votes aiv
            LEFT JOIN agenda_items ai
                ON ai.meeting_id = aiv.meeting_id
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in rows:
        aiv_key = None
        # Need to find the AIV id — re-query to get id from meeting_id+item_number
        aiv = session.execute(
            select(AgendaItemVote.id)
            .where(
                AgendaItemVote.body == body,
                AgendaItemVote.meeting_id == r.meeting_id,
                AgendaItemVote.agenda_item_number == r.agenda_item_number,
            )
        ).scalar_one_or_none()
        if aiv is None:
            continue
        aiv_id = aiv
        sup_nv = sup_vote_map.get(aiv_id, "unknown")
        maj = aiv_majority.get(aiv_id)
        with_maj = None
        if maj and sup_nv in ("yes", "no") and maj not in ("tie", "unknown"):
            with_maj = "with_majority" if sup_nv == maj else "against_majority"

        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "majority_position": maj,
            "with_or_against_majority": with_maj,
        })

    return results

def get_supervisor_dissents(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get split votes where this supervisor voted against the majority.

    Returns the same dict format as get_supervisor_split_votes, but only
    items where with_or_against_majority == 'against_majority'.
    """
    all_split = get_supervisor_split_votes(session, sup_id, body)
    return [s for s in all_split if s.get("with_or_against_majority") == "against_majority"]

def get_supervisor_abstentions(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get votes where this supervisor abstained."""
    from sqlalchemy import text as sa_text

    # Use raw SQL to avoid ORM join-ambiguity issues with the 4-table chain
    sql = sa_text("""
        SELECT
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            m.meeting_date,
            m.meeting_type
        FROM supervisor_votes sv
        JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_id = aiv.meeting_id
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
        WHERE sv.supervisor_id = :sup_id
          AND aiv.body = :body
          AND sv.vote IN ('abstain', 'abstained')
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    rows = session.execute(sql, {"sup_id": sup_id, "body": body}).all()

    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "motion_result": r.motion_result or "",
        }
        for r in rows
    ]

def get_supervisor_absences(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get meetings where this supervisor was marked absent.

    Returns list of dicts with meeting_id, meeting_date, meeting_type, title.
    """
    rows = session.execute(
        select(
            Meeting.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            Meeting.display_name,
            Meeting.meeting_title,
        )
        .join(
            MeetingSupervisor,
            (MeetingSupervisor.meeting_id == Meeting.meeting_id)
            & (MeetingSupervisor.body == Meeting.body),
        )
        .where(
            MeetingSupervisor.supervisor_id == sup_id,
            MeetingSupervisor.present == False,
            Meeting.body == body,
        )
        .order_by(Meeting.meeting_date)
    ).all()

    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "title": r.display_name or r.meeting_title or r.meeting_id,
        }
        for r in rows
    ]

def get_supervisor_full_voting_record(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get chronological list of all recorded votes for this supervisor.

    Returns list of dicts with:
        meeting_id, meeting_date, meeting_type, agenda_item_number,
        agenda_item_title, c_number, vote (normalized), motion_result,
        is_split_vote, majority_position, with_or_against_majority
    """
    from sqlalchemy import text as sa_text

    # Use raw SQL to avoid ORM join-ambiguity issues with the 4-table chain
    sql = sa_text("""
        SELECT
            sv.vote,
            sv.raw_vote_text,
            sv.agenda_item_vote_id,
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            m.meeting_date,
            m.meeting_type
        FROM supervisor_votes sv
        JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_id = aiv.meeting_id
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
        WHERE sv.supervisor_id = :sup_id
          AND aiv.body = :body
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    sup_votes = session.execute(sql, {"sup_id": sup_id, "body": body}).all()

    if not sup_votes:
        return []

    # Gather all AIV IDs for split/majority detection
    aiv_ids = [r.agenda_item_vote_id for r in sup_votes]

    # Get all votes on these AIVs (for split/majority detection)
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    aiv_group: dict[int, list[str]] = {}
    for r in all_v:
        aiv_group.setdefault(r.agenda_item_vote_id, []).append(
            _normalize_vote_value(r.vote)
        )

    is_split: dict[int, bool] = {}
    majority: dict[int, Optional[str]] = {}
    for aiv_id, votes in aiv_group.items():
        vote_set = set(votes)
        has_yes = "yes" in vote_set
        has_no = "no" in vote_set
        is_split[aiv_id] = has_yes and has_no
        if has_yes and has_no:
            yes_cnt = sum(1 for v in votes if v == "yes")
            no_cnt = sum(1 for v in votes if v == "no")
            if yes_cnt > no_cnt:
                majority[aiv_id] = "yes"
            elif no_cnt > yes_cnt:
                majority[aiv_id] = "no"
            else:
                majority[aiv_id] = "tie"
        else:
            majority[aiv_id] = None

    results = []
    for r in sup_votes:
        aiv_id = r.agenda_item_vote_id
        sup_nv = _normalize_vote_value(r.vote)
        maj = majority.get(aiv_id)
        split_flag = is_split.get(aiv_id, False)
        with_maj = None
        if maj and split_flag and sup_nv in ("yes", "no") and maj not in ("tie", "unknown"):
            with_maj = "with_majority" if sup_nv == maj else "against_majority"

        is_inferred = bool(r.raw_vote_text and r.raw_vote_text.startswith("inferred"))
        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "vote": sup_nv,
            "is_inferred": is_inferred,
            "motion_result": r.motion_result or "",
            "is_split_vote": split_flag,
            "majority_position": maj,
            "with_or_against_majority": with_maj,
        })

    return results

def get_supervisor_slug(sup: Supervisor) -> str:
    """Get URL slug for a supervisor."""
    return sup.normalized_name.replace(" ", "-")

def get_supervisor_majority_alignment_stats(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> dict:
    """Get detailed majority alignment stats for a supervisor.

    Extends get_supervisor_vote_stats with additional analytics fields.
    Returns breakdown of unanimous vs split-vote behavior.
    """
    from collections import Counter

    # Get raw vote data
    rows = session.execute(
        select(SupervisorVote.vote, AgendaItemVote.id.label("aiv_id"))
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    total_votes = len(rows)
    if total_votes == 0:
        return {
            "total_votes": 0, "unanimous_votes": 0,
            "split_votes_attended": 0, "with_majority": 0,
            "against_majority": 0, "abstain_on_split": 0,
            "majority_alignment_rate": None, "dissent_rate": None,
        }

    aiv_ids = {r.aiv_id for r in rows}
    aiv_id_list = list(aiv_ids)

    # Get all votes on all relevant AIVs (for split/majority detection)
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.supervisor_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_id_list))
    ).all()

    # Build per-AIV data
    aiv_data: dict[int, dict] = {}
    for av in all_v:
        aiv_data.setdefault(av.agenda_item_vote_id, {
            "votes": [],  # all normalized
            "sup_votes": {},  # sup_id -> norm_vote
        })
        nv = _normalize_vote_value(av.vote)
        aiv_data[av.agenda_item_vote_id]["votes"].append(nv)
        aiv_data[av.agenda_item_vote_id]["sup_votes"][av.supervisor_id] = nv

    # Build sup vote map: aiv_id -> norm_vote for this specific supervisor
    sup_own_votes = {}
    for r in rows:
        sup_own_votes[r.aiv_id] = _normalize_vote_value(r.vote)

    unanimous = 0
    split_attended = 0
    with_maj = 0
    against_maj = 0
    abstain_on_split = 0

    for aiv_id in aiv_ids:
        dd = aiv_data.get(aiv_id, {})
        vlist = dd.get("votes", [])
        yes_cnt = sum(1 for v in vlist if v == "yes")
        no_cnt = sum(1 for v in vlist if v == "no")
        is_split = yes_cnt > 0 and no_cnt > 0

        if not is_split:
            unanimous += 1
            continue

        split_attended += 1
        sup_nv = sup_own_votes.get(aiv_id, "unknown")

        if sup_nv == "abstain":
            abstain_on_split += 1
            continue
        if sup_nv not in ("yes", "no"):
            continue

        if yes_cnt > no_cnt:
            maj = "yes"
        elif no_cnt > yes_cnt:
            maj = "no"
        else:
            maj = "tie"

        if maj == "tie":
            continue
        if sup_nv == maj:
            with_maj += 1
        else:
            against_maj += 1

    majority_alignment_rate = (
        round(with_maj / split_attended, 4) if split_attended > 0 else None
    )
    dissent_rate = (
        round(against_maj / split_attended, 4) if split_attended > 0 else None
    )

    return {
        "total_votes": total_votes,
        "unanimous_votes": unanimous,
        "split_votes_attended": split_attended,
        "with_majority": with_maj,
        "against_majority": against_maj,
        "abstain_on_split": abstain_on_split,
        "majority_alignment_rate": majority_alignment_rate,
        "dissent_rate": dissent_rate,
    }

def get_supervisor_voting_alignment(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Compare voting patterns between this supervisor and all others.

    For each other BOS supervisor, compute:
    - total comparable votes (both voted yes or no)
    - same votes
    - different votes
    - overall alignment percentage
    - split-vote alignment percentage

    Excludes abstentions and absences from pairwise alignment.
    """
    # Get all BOS supervisor IDs
    sup_ids = [
        r[0]
        for r in session.execute(
            select(Supervisor.id)
            .join(
                MeetingSupervisor,
                MeetingSupervisor.supervisor_id == Supervisor.id,
            )
            .where(
                MeetingSupervisor.body == body,
            )
            .group_by(Supervisor.id)
            .having(func.count(MeetingSupervisor.id) >= 5)
            .order_by(Supervisor.name)
        ).all()
    ]

    if sup_id not in sup_ids:
        return []

    other_ids = [sid for sid in sup_ids if sid != sup_id]
    if not other_ids:
        return []

    # Get all votes for this supervisor (body-scoped)
    sup_aiv_votes: dict[int, str] = {}
    for r in session.execute(
        select(SupervisorVote.agenda_item_vote_id, SupervisorVote.vote)
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            sup_aiv_votes[r.agenda_item_vote_id] = nv

    if not sup_aiv_votes:
        return []

    aiv_ids = list(sup_aiv_votes.keys())

    # Get all other supervisors' votes on the same AIVs
    # other_votes[other_sup_id][aiv_id] = norm_vote
    other_votes: dict[int, dict[int, str]] = {oid: {} for oid in other_ids}
    for r in session.execute(
        select(
            SupervisorVote.supervisor_id,
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(
            SupervisorVote.supervisor_id.in_(other_ids),
            SupervisorVote.agenda_item_vote_id.in_(aiv_ids),
        )
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            other_votes[r.supervisor_id][r.agenda_item_vote_id] = nv

    # Also determine which AIVs are split votes
    # For each AIV, collect all normalized votes to detect split
    all_votes_on_aiv: dict[int, list[str]] = {}
    for r in session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            all_votes_on_aiv.setdefault(r.agenda_item_vote_id, []).append(nv)

    split_aiv_ids: set[int] = set()
    for aiv_id, vl in all_votes_on_aiv.items():
        if "yes" in vl and "no" in vl:
            split_aiv_ids.add(aiv_id)

    # Get other supervisor names
    sup_names: dict[int, str] = {
        r.id: r.name
        for r in session.execute(select(Supervisor)).scalars().all()
    }

    results = []
    for oid in other_ids:
        other_name = sup_names.get(oid, f"Supervisor #{oid}")
        other_o = get_supervisor_by_slug_or_name(session, other_name)
        slug = get_supervisor_slug(other_o) if other_o else ""

        # Compare votes
        comparable_aivs = set(sup_aiv_votes.keys()) & set(other_votes[oid].keys())
        if not comparable_aivs:
            continue

        same = 0
        diff = 0
        split_same = 0
        split_diff = 0

        for aiv_id in comparable_aivs:
            sv = sup_aiv_votes[aiv_id]
            ov = other_votes[oid][aiv_id]
            if sv == ov:
                same += 1
                if aiv_id in split_aiv_ids:
                    split_same += 1
            else:
                diff += 1
                if aiv_id in split_aiv_ids:
                    split_diff += 1

        total = same + diff
        split_total = split_same + split_diff
        overall_pct = round(same / total * 100, 1) if total else None
        split_pct = round(split_same / split_total * 100, 1) if split_total else None

        results.append({
            "other_supervisor_id": oid,
            "other_name": other_name,
            "slug": slug,
            "total_comparable_votes": total,
            "same_votes": same,
            "different_votes": diff,
            "overall_alignment_pct": overall_pct,
            "split_vote_comparable": split_total,
            "split_vote_same": split_same,
            "split_vote_alignment_pct": split_pct,
        })

    return results

def get_supervisor_swing_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Identify swing votes for a BOS supervisor.

    Swing vote = split vote where motion passed/failed by one vote
    and this supervisor voted with the prevailing side.
    For BOS (5 members), one-vote margin = 3-2 or 2-3.

    Returns list of dicts with meeting/agenda/vote detail.
    """
    # Get this supervisor's votes on split AIVs
    sup_votes = session.execute(
        select(
            SupervisorVote.vote,
            SupervisorVote.agenda_item_vote_id,
        )
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    if not sup_votes:
        return []

    aiv_ids = [r.agenda_item_vote_id for r in sup_votes]

    # Get all votes on these AIVs
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.supervisor_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    # Build tally per AIV and find this sup's vote
    from collections import Counter
    aiv_tallies: dict[int, Counter] = {}
    sup_aiv_nv: dict[int, str] = {}
    for r in all_v:
        aiv_tallies.setdefault(r.agenda_item_vote_id, Counter())
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            aiv_tallies[r.agenda_item_vote_id][nv] += 1
        if r.supervisor_id == sup_id:
            sup_aiv_nv[r.agenda_item_vote_id] = nv

    # Identify swing AIVs: split vote, margin=1, sup with prevailing side
    swing_aiv_ids: set[int] = set()
    for aiv_id, tally in aiv_tallies.items():
        yes = tally.get("yes", 0)
        no = tally.get("no", 0)
        is_split = yes > 0 and no > 0
        if not is_split:
            continue
        margin = abs(yes - no)
        if margin != 1:
            continue
        # Now check: was this supervisor's vote with the prevailing side?
        sup_nv = sup_aiv_nv.get(aiv_id)
        if sup_nv is None:
            continue
        if sup_nv not in ("yes", "no"):
            continue
        prev_side = "yes" if yes > no else "no"
        if sup_nv == prev_side:
            swing_aiv_ids.add(aiv_id)

    if not swing_aiv_ids:
        return []

    # Fetch meeting/agenda details
    ids_list = list(swing_aiv_ids)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in ids_list)
    rows = session.execute(
        sa_text(f"""
            SELECT
                aiv.id AS aiv_id,
                aiv.meeting_id,
                aiv.agenda_item_number,
                aiv.c_number,
                aiv.motion_result,
                ai.agenda_item_title,
                m.meeting_date,
                m.meeting_type
            FROM agenda_item_votes aiv
            LEFT JOIN agenda_items ai
                ON ai.meeting_id = aiv.meeting_id
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in rows:
        tally = aiv_tallies.get(r.aiv_id, Counter())
        sup_nv = sup_aiv_nv.get(r.aiv_id, "unknown")
        prev_side = "yes" if tally.get("yes", 0) > tally.get("no", 0) else "no"
        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "vote_tally": f"{tally.get('yes',0)}-{tally.get('no',0)}",
            "prevailing_side": prev_side,
        })

    return results

def get_supervisor_controversial_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get controversial votes involving this supervisor.

    Flags items as controversial using detect_controversy_flags heuristics.
    Returns list of dicts with meeting/agenda detail and reason flags.
    """
    from sqlalchemy import text as sa_text
    from collections import defaultdict

    # Get this supervisor's votes with item text
    sql = sa_text("""
        SELECT
            sv.vote,
            sv.agenda_item_vote_id AS aiv_id,
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            COALESCE(ai.agenda_item_text, '') AS agenda_item_text,
            m.meeting_date,
            m.meeting_type
        FROM supervisor_votes sv
        JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_id = aiv.meeting_id
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
        WHERE sv.supervisor_id = :sup_id
          AND aiv.body = :body
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    rows = session.execute(sql, {"sup_id": sup_id, "body": body}).all()

    if not rows:
        return []

    # Gather all AIV IDs to detect split votes and abstentions
    aiv_ids = [r.aiv_id for r in rows]

    # Get all votes on these AIVs
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    aiv_has_abstention: set[int] = set()
    aiv_is_split: set[int] = set()
    aiv_votes: dict[int, list] = defaultdict(list)
    for r in all_v:
        aiv_votes[r.agenda_item_vote_id].append(r.vote)

    for aiv_id, vlist in aiv_votes.items():
        norm = [_normalize_vote_value(v) for v in vlist]
        if "abstain" in norm:
            aiv_has_abstention.add(aiv_id)
        yes = sum(1 for v in norm if v == "yes")
        no = sum(1 for v in norm if v == "no")
        if yes > 0 and no > 0:
            aiv_is_split.add(aiv_id)

    results = []
    for r in rows:
        aiv_id = r.aiv_id
        is_split = aiv_id in aiv_is_split
        has_abst = aiv_id in aiv_has_abstention

        flags = detect_controversy_flags(
            item_title=r.agenda_item_title or "",
            item_text=r.agenda_item_text or "",
            is_split_vote=is_split,
            motion_result=r.motion_result or "",
            has_abstention=has_abst,
        )

        if not flags:
            continue

        sv = r.vote or ""
        sup_nv = _normalize_vote_value(sv) if sv else "unknown"

        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "controversy_flags": flags,
        })

    return results

def get_public_bodies_by_jurisdiction(slug):
    """Get all public bodies for a jurisdiction slug."""
    session = get_session()
    try:
        jurisdiction = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == slug)
        ).scalar_one_or_none()
        if not jurisdiction:
            return []
        bodies = session.execute(
            select(PublicBody).where(PublicBody.jurisdiction_id == jurisdiction.id).order_by(PublicBody.name)
        ).scalars().all()
        return list(bodies)
    finally:
        session.close()

def _enhance_member_for_template(person: Person, public_body_id: int) -> Person:
    """Add backward-compatible attributes to a Person for template rendering.

    Sets active_from, active_to, title, district_or_seat, body from the
    person's most recent BodyMembership so existing templates still work
    without those database columns.

    Uses setattr on the object's __dict__ since the columns no longer
    exist on the Person model.
    """
    session = get_session()
    try:
        membership = session.execute(
            select(BodyMembership)
            .where(BodyMembership.person_id == person.id)
            .where(BodyMembership.public_body_id == public_body_id)
            .order_by(BodyMembership.term_start.desc())
            .limit(1)
        ).scalar_one_or_none()
        if membership:
            person.__dict__["active_from"] = membership.term_start
            person.__dict__["active_to"] = membership.term_end
            person.__dict__["title"] = membership.role
            # Look up body_code from public_body_id
            pb = session.execute(
                select(PublicBody).where(PublicBody.id == public_body_id)
            ).scalar_one_or_none()
            if pb:
                person.__dict__["body"] = pb.body_code or ""
            # Look up district_or_seat from BodySeat if set
            if membership.body_seat_id:
                seat = session.execute(
                    select(BodySeat).where(BodySeat.id == membership.body_seat_id)
                ).scalar_one_or_none()
                if seat:
                    person.__dict__["district_or_seat"] = seat.seat_name
    finally:
        session.close()
    return person

def get_body_members(body_code, page=1, per_page=10):
    """Get paginated members of a public body by body_code.

    Returns currently active members (no term_end or term_end in the future)
    ordered by most recent term start date.
    """
    session = get_session()
    try:
        today = date.today()
        offset = (page - 1) * per_page

        pb = session.execute(
            select(PublicBody).where(PublicBody.body_code == body_code)
        ).scalar_one_or_none()
        if not pb:
            return [], 0

        # Get active member ids for counting
        active_subq = (
            select(BodyMembership.person_id)
            .where(BodyMembership.public_body_id == pb.id)
            .where(BodyMembership.term_start <= today)
            .where(
                (BodyMembership.term_end.is_(None))
                | (BodyMembership.term_end >= today)
            )
        ).subquery()

        total = session.execute(
            select(func.count(active_subq.c.person_id.distinct()))
        ).scalar() or 0

        members = session.execute(
            select(Person)
            .join(BodyMembership, BodyMembership.person_id == Person.id)
            .where(BodyMembership.public_body_id == pb.id)
            .where(BodyMembership.term_start <= today)
            .where(
                (BodyMembership.term_end.is_(None))
                | (BodyMembership.term_end >= today)
            )
            .order_by(BodyMembership.term_start.desc().nullslast(), Person.name)
            .offset(offset).limit(per_page)
        ).scalars().all()
        return list(members), total
    finally:
        session.close()

