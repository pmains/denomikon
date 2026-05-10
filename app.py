#!/usr/bin/env python3
"""
Poliscopic Meetings — Flask web app for browsing Maricopa County board meeting data.

Usage:
    cd /path/to/maricopa-agendas
    .venv/bin/python app.py    # or: python3 app.py  (if flask/sqlalchemy are installed)

Opens at http://127.0.0.1:5000/meetings
"""

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Startup diagnostics — helps debug database discovery
# ---------------------------------------------------------------------------
_here = Path(__file__).resolve().parent
_scripts_dir = _here / "scripts"
_expected_db = _here / "data" / "maricopa.sqlite"

# Add scripts/ to the import path so "from db import ..." works
sys.path.insert(0, str(_scripts_dir))

# Compute an absolute path for the SQLite database so it works regardless
# of the current working directory.  The ./data/maricopa.sqlite relative
# path in db.py would break if Flask is started from somewhere else.
_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    _database_url = f"sqlite:///{_expected_db}"
    os.environ["DATABASE_URL"] = _database_url

# ---------------------------------------------------------------------------
# Runtime checks (printed to stderr on startup)
# ---------------------------------------------------------------------------
_diag_ok = True

try:
    # noinspection PyUnresolvedReferences
    import flask as _flask_mod
except ImportError:
    print("ERROR: Flask is not installed.  Run:  pip install flask", file=sys.stderr)
    _diag_ok = False

try:
    import sqlalchemy as _sa_mod
except ImportError:
    print("ERROR: SQLAlchemy is not installed.  Run:  pip install sqlalchemy", file=sys.stderr)
    _diag_ok = False

if not _expected_db.exists():
    print(
        f"WARNING: Database file not found at {_expected_db}",
        file=sys.stderr,
    )
    print(
        "  Sync some meetings first:\n"
        f"    .venv/bin/python scripts/maricopa_agenda_scraper.py\n"
        "    --sync --start-date=2025-01-01 --end-date=2025-12-31",
        file=sys.stderr,
    )

print(f"Database URL: {_database_url}", file=sys.stderr)
print(f"Data path:    {_expected_db}", file=sys.stderr)
print(f"DB exists:    {_expected_db.exists()}", file=sys.stderr)

if _diag_ok:
    from flask import Flask, render_template, redirect, request, jsonify
    from db import get_session, Meeting, AgendaItem, SupportingDocument
    from db import AgendaItemVote, SupervisorVote, Supervisor, MeetingSupervisor, PZItemDetail
    from db import Case, CaseEvent
    from db import (
        get_bos_supervisors,
        get_supervisor_by_slug_or_name,
        get_supervisor_vote_stats,
        get_supervisor_split_votes,
        get_supervisor_dissents,
        get_supervisor_abstentions,
        get_supervisor_absences,
        get_supervisor_full_voting_record,
        get_supervisor_slug,
        get_supervisor_majority_alignment_stats,
        get_supervisor_voting_alignment,
        get_supervisor_swing_votes,
        get_supervisor_controversial_votes,
    )
    from sqlalchemy import select, func, or_, text as sa_text
else:
    print("FATAL: Missing dependencies — cannot start.", file=sys.stderr)
    raise SystemExit(1)

app = Flask(__name__)

SYNC_STATUS_BADGES = {
    "complete": "success",
    "failed": "danger",
    "partial": "warning",
    "manual_review": "secondary",
    "pending": "info",
}


@app.route("/")
def index():
    return redirect("/meetings")


def get_distinct_meeting_types(body=None):
    """Get all distinct meeting_type values from the database, optionally filtered by body."""
    session = get_session()
    q = select(Meeting.meeting_type).distinct().order_by(Meeting.meeting_type)
    if body and body.lower() != "all":
        if body.lower() in ("bz", "pz", "planning"):
            q = q.where(Meeting.body == "pz")
        elif body.lower() in ("adj", "board of adjustment"):
            q = q.where(Meeting.body == "adj")
        elif body.lower() in ("drain", "drainage", "drb"):
            q = q.where(Meeting.body == "drain")
        elif body.lower() in ("health", "board of health", "boh"):
            q = q.where(Meeting.body == "health")
        elif body.lower() in ("tab", "transportation advisory board"):
            q = q.where(Meeting.body == "tab")
        elif body.lower() in ("ida", "industrial development authority"):
            q = q.where(Meeting.body == "ida")
        else:
            q = q.where(Meeting.body == "bos")
    rows = session.execute(q).scalars().all()
    session.close()
    return [r for r in rows if r]


def get_filtered_meetings(body=None, meeting_type=None, start_date=None, end_date=None, page=1, per_page=25):
    """Query meetings with optional filters and pagination. Returns (meetings_list, total_count, page, total_pages)."""
    session = get_session()

    # Build base query (no LIMIT/OFFSET yet)
    base_q = select(
        Meeting.body,
        Meeting.meeting_id,
        Meeting.meeting_date,
        Meeting.meeting_type,
        Meeting.meeting_title,
        Meeting.meeting_body,
        Meeting.display_name,
        Meeting.sync_status,
        func.coalesce(Meeting.item_count_actual, 0).label("item_count"),
        func.coalesce(Meeting.supporting_doc_count, 0).label("doc_count"),
    )

    if body and body.lower() != "all":
        if body.lower() in ("bz", "pz", "planning"):
            base_q = base_q.where(Meeting.body == "pz")
        elif body.lower() in ("adj", "board of adjustment"):
            base_q = base_q.where(Meeting.body == "adj")
        elif body.lower() in ("drain", "drainage", "drb"):
            base_q = base_q.where(Meeting.body == "drain")
        elif body.lower() in ("health", "board of health", "boh"):
            base_q = base_q.where(Meeting.body == "health")
        elif body.lower() in ("tab", "transportation advisory board"):
            base_q = base_q.where(Meeting.body == "tab")
        elif body.lower() in ("ida", "industrial development authority"):
            base_q = base_q.where(Meeting.body == "ida")
        else:
            base_q = base_q.where(Meeting.body == "bos")

    if meeting_type and meeting_type.lower() != "all":
        # Normalise the filter value: "planning" → "Planning & Zoning",
        # but pass through actual meeting_type values like
        # "Planning & Zoning - ZIPPOR Committee" exactly as-is.
        type_map = {
            "planning": "Planning & Zoning",
            "zippor": "ZIPPOR",
        }
        match_type = type_map.get(meeting_type.lower(), meeting_type)
        base_q = base_q.where(Meeting.meeting_type == match_type)

    if start_date:
        base_q = base_q.where(Meeting.meeting_date >= start_date)
    if end_date:
        base_q = base_q.where(Meeting.meeting_date <= end_date)

    # Get total count
    count_q = select(func.count()).select_from(base_q.subquery())
    total_count = session.execute(count_q).scalar() or 0

    # Apply pagination
    q = base_q.order_by(Meeting.meeting_date.desc(), Meeting.meeting_id.desc())
    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = session.execute(q).all()

    meetings_list = []
    for row in rows:
        is_pz = (row.body or "") == "pz"
        is_adj = (row.body or "") == "adj"
        is_drain = (row.body or "") == "drain"
        is_health = (row.body or "") == "health"
        is_tab = (row.body or "") == "tab"
        is_ida = (row.body or "") == "ida"
        meetings_list.append({
            "body": row.body or "bos",
            "meeting_id": row.meeting_id,
            "meeting_date": row.meeting_date or "",
            "meeting_type": row.meeting_type or "",
            "title": row.meeting_title or row.display_name or row.meeting_id,
            "source": "IDA" if is_ida else ("TAB" if is_tab else ("BOH" if is_health else ("DRB" if is_drain else ("ADJ" if is_adj else ("PZ" if is_pz else "BOS"))))),
            "source_badge": "light" if is_ida else ("warning" if is_tab else ("success" if is_health else ("info" if is_drain else ("dark" if is_adj else ("secondary" if is_pz else "primary"))))),
            "sync_status": row.sync_status or "pending",
            "badge_class": SYNC_STATUS_BADGES.get((row.sync_status or "").lower(), "secondary"),
            "item_count": row.item_count,
            "doc_count": row.doc_count,
        })
    session.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    return meetings_list, total_count, page, total_pages


@app.route("/meetings")
def meetings():
    body = request.args.get("body", "")
    meeting_type = request.args.get("type", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    try:
        per_page = int(request.args.get("per_page", "25"))
    except ValueError:
        per_page = 25
    # Clamp to reasonable range
    per_page = max(10, min(100, per_page))

    meetings_list, total_count, current_page, total_pages = get_filtered_meetings(
        body=body or None,
        meeting_type=meeting_type or None,
        start_date=start_date or None,
        end_date=end_date or None,
        page=page,
        per_page=per_page,
    )

    distinct_types = get_distinct_meeting_types(body=body or None)

    return render_template(
        "meetings.html",
        meetings=meetings_list,
        distinct_types=distinct_types,
        filter_body=body,
        filter_type=meeting_type,
        filter_start=start_date,
        filter_end=end_date,
        page=current_page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
    )


@app.route("/meetings/<meeting_id>")
@app.route("/meetings/<body>/<meeting_id>")
def meeting_detail(meeting_id, body=None):
    session = get_session()

    # --- Meeting header ---
    q = select(Meeting).where(Meeting.meeting_id == meeting_id)
    if body:
        q = q.where(Meeting.body == body.lower())
    meeting = session.execute(q).scalar_one_or_none()

    if not meeting:
        session.close()
        return render_template("meeting_detail.html", meeting_id=meeting_id, meeting=None)

    meeting_body_val = meeting.body or "bos"

    # --- Agenda items ---
    items = session.execute(
        select(AgendaItem)
        .where(
            AgendaItem.body == meeting_body_val,
            AgendaItem.meeting_id == meeting_id,
        )
        .order_by(AgendaItem.agenda_item_number)
    ).scalars().all()

    # --- Batch-load supporting docs per item ---
    docs_by_item: dict[int, list] = {}
    docs = session.execute(
        select(SupportingDocument)
        .where(
            SupportingDocument.body == meeting_body_val,
            SupportingDocument.meeting_id == meeting_id,
        )
        .order_by(SupportingDocument.agenda_item_number, SupportingDocument.id)
    ).scalars().all()
    for d in docs:
        docs_by_item.setdefault(d.agenda_item_number, []).append(d)

    # --- Batch-load votes per item ---
    votes_by_item: dict[int, dict] = {}
    item_votes = session.execute(
        select(AgendaItemVote)
        .where(
            AgendaItemVote.body == meeting_body_val,
            AgendaItemVote.meeting_id == meeting_id,
        )
    ).scalars().all()

    vote_ids = [v.id for v in item_votes]
    supervisor_votes_by_vote: dict[int, list] = {}
    if vote_ids:
        sv_rows = session.execute(
            select(SupervisorVote, Supervisor.name, Supervisor.normalized_name)
            .join(Supervisor, SupervisorVote.supervisor_id == Supervisor.id)
            .where(SupervisorVote.agenda_item_vote_id.in_(vote_ids))
        ).all()
        for sv, sname, snorm in sv_rows:
            slug = snorm.replace(" ", "-") if snorm else ""
            supervisor_votes_by_vote.setdefault(sv.agenda_item_vote_id, []).append(
                {"name": sname, "vote": sv.vote, "slug": slug,
                 "is_inferred": sv.raw_vote_text and sv.raw_vote_text.startswith("inferred abstention")}
            )

    for av in item_votes:
        votes_by_item[av.agenda_item_number] = {
            "motion_result": av.motion_result,
            "vote_text": (av.vote_text or "")[:500],
            "conditions": av.conditions,
            "supervisor_votes": supervisor_votes_by_vote.get(av.id, []),
        }

    vote_count = len(item_votes)

    # PZ item details
    pz_details: dict[int, dict] = {}
    is_pz = (meeting.body or "") == "pz"
    if is_pz:
        detail_rows = session.execute(
            select(PZItemDetail).where(
                PZItemDetail.body == meeting_body_val,
                PZItemDetail.meeting_id == meeting_id,
            )
        ).scalars().all()
        for d in detail_rows:
            pz_details[d.agenda_item_number] = {
                "case_number": d.case_number,
                "project_name": d.project_name,
                "applicant": d.applicant,
                "district": d.district,
                "request": d.request,
                "location": d.location,
                "recommendation": d.recommendation,
                "presented_by": d.presented_by,
                "staff_report_url": d.staff_report_url,
            }

    # Cross-link data for case numbers
    related_pz: dict[str, list] = {}
    related_bos: dict[str, list] = {}

    if items:
        for item in items:
            cn = (item.case_number or "").strip()
            if cn:
                if is_pz:
                    # PZ item → find related BOS events
                    bos_events = get_related_bos_items_for_case(cn)
                    if bos_events:
                        related_bos[cn] = bos_events
                else:
                    # BOS item → find related PZ events
                    pz_events = get_related_pz_items_for_case(cn)
                    if pz_events:
                        related_pz[cn] = pz_events

    session.close()

    badge = SYNC_STATUS_BADGES.get((meeting.sync_status or "").lower(), "secondary")

    return render_template(
        "meeting_detail.html",
        meeting=meeting,
        meeting_id=meeting_id,
        items=items,
        docs_by_item=docs_by_item,
        votes_by_item=votes_by_item,
        vote_count=vote_count,
        badge_class=badge,
        is_pz=is_pz,
        pz_details=pz_details,
        related_pz=related_pz,
        related_bos=related_bos,
    )


@app.route("/c-number/<c_number_base>")
def c_number_revisions(c_number_base):
    """Show all agenda items sharing the same c_number_base."""
    session = get_session()

    items = session.execute(
        select(
            AgendaItem.meeting_id,
            AgendaItem.agenda_item_number,
            AgendaItem.agenda_item_title,
            AgendaItem.c_number,
            AgendaItem.c_number_base,
            AgendaItem.c_number_revision,
            AgendaItem.vote_or_action,
            Meeting.meeting_date,
            Meeting.meeting_type,
            Meeting.meeting_body,
        )
        .join(Meeting, Meeting.meeting_id == AgendaItem.meeting_id)
        .where(
            or_(
                AgendaItem.c_number_base == c_number_base,
                AgendaItem.c_number == c_number_base,
            )
        )
        .order_by(Meeting.meeting_date, AgendaItem.agenda_item_number)
    ).all()

    session.close()

    return render_template(
        "c_number.html",
        c_number_base=c_number_base,
        items=items,
    )


def get_related_case_events(case_number):
    """Get all CaseEvents for a case number, with meeting metadata."""
    from db import Case as CaseModel
    session = get_session()
    case = session.execute(
        select(CaseModel).where(CaseModel.case_number == case_number.upper())
    ).scalar_one_or_none()
    if not case:
        session.close()
        return []
    events = session.execute(
        select(CaseEvent, Meeting.meeting_date, Meeting.meeting_type, Meeting.meeting_title)
        .outerjoin(Meeting, Meeting.meeting_id == CaseEvent.meeting_id)
        .where(CaseEvent.case_id == case.id)
        .order_by(CaseEvent.event_date)
    ).all()
    result = []
    for ev, mdate, mtype, mtitle in events:
        is_bos = (mtype or "") != "Planning & Zoning"
        result.append({
            "source": ev.source,
            "source_label": "BOS" if is_bos else "PZ",
            "meeting_id": ev.meeting_id,
            "meeting_date": mdate or ev.event_date,
            "meeting_type": mtype or "",
            "meeting_title": mtitle or "",
            "event_type": ev.event_type,
            "agenda_item_id": ev.agenda_item_id,
            "notes": ev.notes,
        })
    session.close()
    return result


def get_related_bos_items_for_case(case_number):
    """Get BOS agenda items related to a case number via case_events."""
    events = get_related_case_events(case_number)
    return [e for e in events if e.get("source_label") == "BOS"]


def get_related_pz_items_for_case(case_number):
    """Get PZ-related events for a case number."""
    events = get_related_case_events(case_number)
    return [e for e in events if e.get("source_label") == "PZ"]


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


@app.route("/members")
def members():
    """Member directory — list all BOS supervisors with high-level stats."""
    session = get_session()
    supervisors = get_bos_supervisors(session)

    member_rows = []
    for sup in supervisors:
        stats = get_supervisor_vote_stats(session, sup.id, body="bos")
        slug = get_supervisor_slug(sup)
        member_rows.append({
            "id": sup.id,
            "name": sup.name,
            "normalized_name": sup.normalized_name,
            "slug": slug,
            "district": sup.district or "",
            "role": "",
            "active": sup.active_to is None,
            "total_votes": stats["total_votes"],
            "split_votes": stats["split_votes_attended"],
            "dissents": stats["against_majority"],
            "abstentions": stats["abstain"],
            "absences": stats["absences"],
        })

    session.close()
    return render_template("members.html", members=member_rows)


@app.route("/api/members/<slug>/votes")
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


@app.route("/members/<slug>")
def member_detail(slug):
    """Supervisor profile page with voting history sections."""
    session = get_session()

    sup = get_supervisor_by_slug_or_name(session, slug)
    if not sup:
        session.close()
        return render_template(
            "member_detail.html",
            member=None,
            slug=slug,
        )

    slug_out = get_supervisor_slug(sup)
    stats = get_supervisor_vote_stats(session, sup.id, body="bos")
    split_votes = get_supervisor_split_votes(session, sup.id, body="bos")
    dissents = [s for s in split_votes if s.get("with_or_against_majority") == "against_majority"]
    abstentions = get_supervisor_abstentions(session, sup.id, body="bos")
    absences = get_supervisor_absences(session, sup.id, body="bos")
    full_record = get_supervisor_full_voting_record(session, sup.id, body="bos")
    full_record_count = len(full_record)
    # Only render the first 25 rows server-side; the rest are lazy-loaded via API
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
    )


@app.route("/debug/inferred-abstentions")
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


@app.route("/members/<slug>/analytics")
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


if __name__ == "__main__":
    app.run(debug=True)
