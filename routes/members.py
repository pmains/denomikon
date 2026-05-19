"""Members routes blueprint."""

import logging
from typing import Optional

from flask import Blueprint, render_template, request, jsonify, redirect
from sqlalchemy import select, func, text as sa_text, or_

from db import (
    get_session, Supervisor, MeetingSupervisor, Person,
    BodyMembership, _enhance_member_for_template,
    get_bos_supervisors, get_supervisor_by_slug_or_name,
    get_supervisor_vote_stats, get_supervisor_split_votes,
    get_supervisor_dissents, get_supervisor_abstentions,
    get_supervisor_absences, get_supervisor_full_voting_record,
    get_supervisor_slug, get_supervisor_majority_alignment_stats,
    get_supervisor_voting_alignment, get_supervisor_swing_votes,
    get_supervisor_controversial_votes,
    Jurisdiction, PublicBody, seed_default_jurisdictions,
    get_public_bodies_by_jurisdiction, get_body_members,
    Meeting, MeetingAttendance, ExecutiveSessionParticipant,
    AgendaItemVote, AgendaItem,
)
from routes import SYNC_STATUS_BADGES, _cache

log = logging.getLogger(__name__)

members_bp = Blueprint("members", __name__, url_prefix="")

# ---------------------------------------------------------------------------
# BOS Member / Supervisor Voting Portal — Routes
# ---------------------------------------------------------------------------

VOTE_BADGE_CLASSES = {
    "yes": "success",
    "no": "danger",
    "abstain": "warning",
    "absent": "secondary",
    "recused": "secondary",
}

MAJORITY_BADGE_CLASSES = {
    "with_majority": "success",
    "against_majority": "danger",
}


@members_bp.route("/members")
def members():
    """Member directory — redirect to the unified bodies index."""
    return redirect("/bodies")


@members_bp.route("/api/members/<slug>/votes")
def member_votes_api(slug):
    """JSON API for the Full Voting Record table.

    Supports pagination, search, and per-column filtering via query params.
    """
    session = get_session()

    sup = get_supervisor_by_slug_or_name(session, slug)
    if not sup:
        session.close()
        return jsonify({"rows": [], "total": 0, "page": 1, "per_page": 25}), 200

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 25))
    except ValueError:
        per_page = 25
    per_page = max(10, min(100, per_page))
    search_q = (request.args.get("q") or "").strip().lower()
    filter_vote = (request.args.get("vote") or "").strip().lower()
    filter_result = (request.args.get("result") or "").strip().lower()
    filter_majority = (request.args.get("majority") or "").strip().lower()
    filter_split = (request.args.get("split") or "").strip().lower()

    # Load the full dataset
    all_records = get_supervisor_full_voting_record(session, sup.id, body="bos")
    session.close()

    if not all_records:
        return jsonify({"rows": [], "total": 0, "page": 1, "per_page": 25}), 200

    # Apply filters
    filtered = all_records
    if search_q:
        filtered = [
            r for r in filtered
            if search_q in (r.get("agenda_item_title") or "").lower()
            or search_q in (r.get("meeting_date") or "")
            or search_q in (r.get("meeting_type") or "").lower()
            or search_q in str(r.get("agenda_item_number", ""))
            or search_q in (r.get("c_number") or "").lower()
            or search_q in (r.get("vote") or "")
            or search_q in (r.get("motion_result") or "").lower()
        ]
    if filter_vote and filter_vote != "all":
        filtered = [r for r in filtered if (r.get("vote") or "") == filter_vote]
    if filter_result and filter_result != "all":
        filtered = [r for r in filtered if (r.get("motion_result") or "") == filter_result]
    if filter_majority and filter_majority != "all":
        filtered = [
            r for r in filtered
            if (r.get("with_or_against_majority") or "") == filter_majority
        ]
    if filter_split in ("true", "1", "yes"):
        filtered = [r for r in filtered if r.get("is_split_vote")]
    elif filter_split in ("false", "0", "no"):
        filtered = [r for r in filtered if not r.get("is_split_vote")]

    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    rows = filtered[start:end]

    return jsonify({
        "rows": [
            {
                "meeting_id": r["meeting_id"],
                "meeting_date": r["meeting_date"],
                "meeting_type": r["meeting_type"],
                "agenda_item_number": r["agenda_item_number"],
                "agenda_item_title": r["agenda_item_title"],
                "c_number": r["c_number"],
                "vote": r["vote"],
                "motion_result": r["motion_result"],
                "is_split_vote": r["is_split_vote"],
                "is_inferred": r["is_inferred"],
                "majority_position": r["majority_position"],
                "with_or_against_majority": r["with_or_against_majority"],
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@members_bp.route("/members/<jurisdiction_slug>/<body_code>/<slug>")
def member_detail(jurisdiction_slug, body_code, slug):
    """Member profile by jurisdiction + body + slug — disambiguates name collisions."""
    session = get_session()

    sup = get_supervisor_by_slug_or_name(session, slug)
    if not sup:
        session.close()
        return render_template("member_detail.html", member=None, slug=slug)

    slug_out = get_supervisor_slug(sup)
    stats = get_supervisor_vote_stats(session, sup.id, body="bos")
    split_votes = get_supervisor_split_votes(session, sup.id, body="bos")
    dissents = [s for s in split_votes if s.get("with_or_against_majority") == "against_majority"]
    abstentions = get_supervisor_abstentions(session, sup.id, body="bos")
    absences = get_supervisor_absences(session, sup.id, body="bos")
    full_record = get_supervisor_full_voting_record(session, sup.id, body="bos")
    full_record_count = len(full_record)
    full_record = full_record[:25]
    session.close()

    return render_template(
        "member_detail.html",
        member=sup,
        slug=slug_out,
        stats=stats,
        split_votes=split_votes,
        dissents=dissents,
        abstentions=abstentions,
        absences=absences,
        full_record=full_record,
        full_record_count=full_record_count,
        full_record_api_url=f"/api/members/{slug_out}/votes",
        vote_badges=VOTE_BADGE_CLASSES,
        majority_badges=MAJORITY_BADGE_CLASSES,
        member_url=f"/members/{jurisdiction_slug}/{body_code}/{slug_out}",
    )


@members_bp.route("/members/<slug>")
def member_detail_legacy(slug):
    """Legacy member route — redirect to qualified URL."""
    session = get_session()
    sup = get_supervisor_by_slug_or_name(session, slug)
    if sup:
        slug_out = get_supervisor_slug(sup)
        session.close()
        return redirect(f"/members/maricopa-county/bos/{slug_out}")
    session.close()
    return render_template("member_detail.html", member=None, slug=slug)


@members_bp.route("/debug/inferred-abstentions")
def debug_inferred_abstentions():
    """Review page for parser-gap detection.

    Lists all inferred abstentions grouped by meeting, so humans can
    quickly check whether the vote was truly abstained or the parser
    missed an explicit Yes/No from the summary.
    """
    session = get_session()

    from sqlalchemy import text as sa_text
    # Joining AgendaItemVote → Meeting for meeting_date
    rows = session.execute(
        sa_text("""
            SELECT
                aiv.meeting_id,
                m.meeting_date,
                aiv.agenda_item_number,
                aiv.vote_text,
                sv.supervisor_id,
                sup.name,
                sup.normalized_name
            FROM supervisor_votes sv
            JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
            JOIN supervisors sup ON sup.id = sv.supervisor_id
            LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
            WHERE sv.raw_vote_text LIKE :prefix
              AND aiv.body = :body
            ORDER BY aiv.meeting_id, aiv.agenda_item_number
        """)
        .bindparams(prefix="inferred%", body="bos")
    ).all()
    session.close()

    # Group by meeting
    by_meeting: dict[str, list] = {}
    for r in rows:
        mid = r.meeting_id
        by_meeting.setdefault(mid, []).append({
            "meeting_date": r.meeting_date,
            "agenda_item_number": r.agenda_item_number,
            "vote_text": (r.vote_text or "")[:200],
            "supervisor_name": r.name,
            "supervisor_slug": r.normalized_name.replace(" ", "-"),
        })

    return render_template(
        "debug_inferred.html",
        by_meeting=sorted(by_meeting.items(), key=lambda kv: kv[0]),
    )


@members_bp.route("/members/<slug>/analytics")
def member_analytics(slug):
    """Voting analytics profile page for a member."""
    session = get_session()

    sup = get_supervisor_by_slug_or_name(session, slug)
    if not sup:
        session.close()
        return render_template(
            "analytics.html",
            member=None,
            slug=slug,
        )

    slug_out = get_supervisor_slug(sup)
    alignment_stats = get_supervisor_majority_alignment_stats(session, sup.id, body="bos")
    voting_alignment = get_supervisor_voting_alignment(session, sup.id, body="bos")
    swing_votes = get_supervisor_swing_votes(session, sup.id, body="bos")
    controversial_votes = get_supervisor_controversial_votes(session, sup.id, body="bos")

    session.close()

    return render_template(
        "analytics.html",
        member=sup,
        slug=slug_out,
        alignment_stats=alignment_stats,
        voting_alignment=voting_alignment,
        swing_votes=swing_votes,
        controversial_votes=controversial_votes,
        vote_badges=VOTE_BADGE_CLASSES,
        majority_badges=MAJORITY_BADGE_CLASSES,
    )

