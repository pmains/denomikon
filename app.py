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
    from flask import Flask, render_template, redirect, request
    from db import get_session, Meeting, AgendaItem, SupportingDocument
    from db import AgendaItemVote, SupervisorVote, Supervisor, MeetingSupervisor, PZItemDetail
    from db import Case, CaseEvent
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
        for sv, sname, _snorm in sv_rows:
            supervisor_votes_by_vote.setdefault(sv.agenda_item_vote_id, []).append(
                {"name": sname, "vote": sv.vote}
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


if __name__ == "__main__":
    app.run(debug=True)
