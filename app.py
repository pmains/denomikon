#!/usr/bin/env python3
"""
Poliscopic Meetings — Flask web app for browsing Maricopa County board meeting data.

Usage:
    cd /path/to/maricopa-agendas
    .venv/bin/python app.py    # or: python3 app.py  (if flask/sqlalchemy are installed)

Opens at http://127.0.0.1:5000/meetings
"""

import logging
import os
import sys
import time
from functools import wraps
from pathlib import Path

log = logging.getLogger(__name__)

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
        f"    .venv/bin/python scripts/agenda_scraper.py\n"
        "    --sync --start-date=2025-01-01 --end-date=2025-12-31",
        file=sys.stderr,
    )

print(f"Database URL: {_database_url}", file=sys.stderr)
print(f"Data path:    {_expected_db}", file=sys.stderr)
print(f"DB exists:    {_expected_db.exists()}", file=sys.stderr)

if _diag_ok:
    from flask import Flask, render_template, redirect, request, jsonify
    from datetime import date
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
    from db import Jurisdiction, PublicBody, PublicBodyMember, Permit, PermitReport
    from db import seed_default_jurisdictions, get_public_bodies_by_jurisdiction, get_body_members
    from sqlalchemy import select, func, or_, Float, text as sa_text
else:
    print("FATAL: Missing dependencies — cannot start.", file=sys.stderr)
    raise SystemExit(1)

app = Flask(__name__)

# ── Cache ───────────────────────────────────────────────────────────────────
try:
    from flask_caching import Cache
    cache = Cache(app, config={
        "CACHE_TYPE": "FileSystemCache",
        "CACHE_DIR": _here / ".cache" / "flask-cache",
        "CACHE_DEFAULT_TIMEOUT": 60,
        "CACHE_THRESHOLD": 200,
    })
    log.info("Flask-Caching enabled (FileSystemCache, 60s default)")
except ImportError:
    cache = None
    log.warning("Flask-Caching not installed — install with: pip install Flask-Caching")

# ── Conditional caching decorator ───────────────────────────────────────────
def _cache(timeout=60, query_string=False):
    """Apply Flask-Caching if available, otherwise no-op."""
    if cache:
        return cache.cached(timeout=timeout, query_string=query_string)
    return lambda f: f


# ── Seed default data on startup ───────────────────────────────────────────
seed_default_jurisdictions()


# ── Request timing ──────────────────────────────────────────────────────────
@app.before_request
def _start_timer():
    request._start_time = time.monotonic()


@app.after_request
def _log_timing(response):
    elapsed = time.monotonic() - getattr(request, "_start_time", time.monotonic())
    if elapsed > 1.0:
        log.warning("%s %.1fs", request.path, elapsed)
    return response


SYNC_STATUS_BADGES = {
    "complete": "success",
    "failed": "danger",
    "partial": "warning",
    "manual_review": "secondary",
    "pending": "info",
}


@app.route("/")
def index():
    return render_template("home.html")


def get_distinct_meeting_types(body=None, jurisdiction=None):
    """Get all distinct meeting_type values from the database, optionally filtered by body."""
    session = get_session()
    q = select(Meeting.meeting_type).distinct().order_by(Meeting.meeting_type)

    # Filter by jurisdiction if given
    if jurisdiction and jurisdiction.lower() not in ("", "all"):
        jur_slug = jurisdiction.strip().lower()
        jur = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == jur_slug)
        ).scalar_one_or_none()
        if jur:
            q = q.where(Meeting.jurisdiction_id == jur.id)

    # Filter by body (now also handles tempe-* codes)
    if body and body.lower() != "all":
        b = body.lower()
        body_map = {
            "bz": "pz", "pz": "pz", "planning": "pz",
            "adj": "adj", "board of adjustment": "adj",
            "drain": "drain", "drainage": "drain", "drb": "drain",
            "health": "health", "board of health": "health", "boh": "health",
            "tab": "tab", "transportation advisory board": "tab",
            "ida": "ida", "industrial development authority": "ida",
            "bos": "bos", "board of supervisors": "bos",
            "tempe-cc": "tempe-cc", "tempe city council": "tempe-cc",
            "tempe-drc": "tempe-drc", "development review commission": "tempe-drc",
            "tempe-boa": "tempe-boa", "tempe board of adjustment": "tempe-boa",
            "tempe-hpc": "tempe-hpc", "tempe historic preservation": "tempe-hpc",
            "tempe-ha": "tempe-ha", "tempe housing authority": "tempe-ha",
            "tempe-rio": "tempe-rio", "rio salado": "tempe-rio",
            "tempe-rmt": "tempe-rmt", "risk management trust": "tempe-rmt",
            "tempe-jrc": "tempe-jrc", "joint review committee": "tempe-jrc",
        }
        res = body_map.get(b)
        if res:
            q = q.where(Meeting.body == res)
        else:
            q = q.where(Meeting.body == body)

    rows = session.execute(q).scalars().all()
    session.close()
    return [r for r in rows if r]


def get_filtered_meetings(body=None, meeting_type=None, start_date=None, end_date=None, page=1, per_page=25, jurisdiction=None, hide_upcoming=False):
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
        Meeting.jurisdiction_id,
        func.coalesce(Meeting.item_count_actual, 0).label("item_count"),
        func.coalesce(Meeting.supporting_doc_count, 0).label("doc_count"),
    )

    # Normalize body code (now also handles tempe-* codes)
    if body and body.lower() != "all":
        b = body.lower()
        body_map = {
            "bz": "pz", "pz": "pz", "planning": "pz",
            "adj": "adj", "board of adjustment": "adj",
            "drain": "drain", "drainage": "drain", "drb": "drain",
            "health": "health", "board of health": "health", "boh": "health",
            "tab": "tab", "transportation advisory board": "tab",
            "ida": "ida", "industrial development authority": "ida",
            "bos": "bos", "board of supervisors": "bos",
            "tempe-cc": "tempe-cc", "tempe city council": "tempe-cc",
            "tempe-drc": "tempe-drc", "development review commission": "tempe-drc",
            "tempe-boa": "tempe-boa", "tempe board of adjustment": "tempe-boa",
            "tempe-hpc": "tempe-hpc", "tempe historic preservation": "tempe-hpc",
            "tempe-ha": "tempe-ha", "tempe housing authority": "tempe-ha",
            "tempe-rio": "tempe-rio", "rio salado": "tempe-rio",
            "tempe-rmt": "tempe-rmt", "risk management trust": "tempe-rmt",
            "tempe-jrc": "tempe-jrc", "joint review committee": "tempe-jrc",
        }
        res = body_map.get(b)
        if res:
            base_q = base_q.where(Meeting.body == res)
        else:
            base_q = base_q.where(Meeting.body == body)

    # Filter by jurisdiction
    if jurisdiction and jurisdiction.lower() not in ("", "all"):
        jur_slug = jurisdiction.strip().lower()
        jur = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == jur_slug)
        ).scalar_one_or_none()
        if jur:
            base_q = base_q.where(Meeting.jurisdiction_id == jur.id)

    # Hide upcoming/future meetings
    if hide_upcoming:
        base_q = base_q.where(Meeting.meeting_date <= str(date.today()))

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
        body_val = row.body or "bos"
        is_pz = body_val == "pz"
        is_adj = body_val == "adj"
        is_drain = body_val == "drain"
        is_health = body_val == "health"
        is_tab = body_val == "tab"
        is_ida = body_val == "ida"
        is_tempe = body_val.startswith("tempe-")
        # Derive source label and badge from body value
        if is_tempe:
            source_labels = {
                "tempe-cc": "City Council",
                "tempe-drc": "Dev Review",
                "tempe-boa": "Board of Adj",
                "tempe-hpc": "Hist Preserv",
                "tempe-ha": "Housing Auth",
                "tempe-rio": "Rio Salado",
                "tempe-rmt": "Risk Mgmt",
                "tempe-jrc": "Joint Review",
            }
            source = source_labels.get(body_val, "Tempe")
            source_badge = "info"
        else:
            source = "IDA" if is_ida else ("TAB" if is_tab else ("BOH" if is_health else ("DRB" if is_drain else ("ADJ" if is_adj else ("PZ" if is_pz else "BOS")))))
            source_badge = "light" if is_ida else ("warning" if is_tab else ("success" if is_health else ("info" if is_drain else ("dark" if is_adj else ("secondary" if is_pz else "primary")))))
        # Resolve jurisdiction name from meeting.jurisdiction_id
        jur_id = row.jurisdiction_id or 1
        if jur_id == 2:
            jur_name = "Tempe"
            jur_slug = "tempe"
        else:
            jur_name = "Maricopa County"
            jur_slug = "maricopa-county"

        meetings_list.append({
            "body": body_val,
            "meeting_id": row.meeting_id,
            "meeting_date": row.meeting_date or "",
            "meeting_type": row.meeting_type or "",
            "title": row.meeting_title or row.display_name or row.meeting_id,
            "jurisdiction": jur_name,
            "jurisdiction_slug": jur_slug,
            "source": source,
            "source_badge": source_badge,
            "sync_status": row.sync_status or "pending",
            "badge_class": SYNC_STATUS_BADGES.get((row.sync_status or "").lower(), "secondary"),
            "item_count": row.item_count,
            "doc_count": row.doc_count,
        })
    session.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    return meetings_list, total_count, page, total_pages


@app.route("/meetings")
@_cache(timeout=60, query_string=True)
def meetings():
    body = request.args.get("body", "")
    meeting_type = request.args.get("type", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    jurisdiction = request.args.get("jurisdiction", "")
    hide_upcoming = request.args.get("hide_upcoming", "") == "1"

    # Default end_date to today when hide_upcoming is active
    if hide_upcoming and not end_date:
        end_date = str(date.today())

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
        jurisdiction=jurisdiction or None,
        hide_upcoming=hide_upcoming,
        page=page,
        per_page=per_page,
    )

    distinct_types = get_distinct_meeting_types(body=body or None, jurisdiction=jurisdiction or None)

    return render_template(
        "meetings.html",
        meetings=meetings_list,
        distinct_types=distinct_types,
        filter_body=body,
        filter_type=meeting_type,
        filter_start=start_date,
        filter_end=end_date,
        filter_jurisdiction=jurisdiction,
        hide_upcoming=hide_upcoming,
        page=current_page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
    )


@app.route("/meetings/<meeting_id>")
@app.route("/meetings/<body>/<meeting_id>")
@_cache(timeout=120)
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
# Public Bodies / Members — Routes
# ---------------------------------------------------------------------------

@app.route("/bodies")
def bodies_index():
    """List all known public bodies grouped by jurisdiction."""
    session = get_session()
    jurisdictions = session.execute(
        select(Jurisdiction).order_by(Jurisdiction.name)
    ).scalars().all()

    result = []
    for j in jurisdictions:
        bodies = session.execute(
            select(PublicBody).where(PublicBody.jurisdiction_id == j.id).order_by(PublicBody.name)
        ).scalars().all()
        result.append((j, bodies))
    session.close()
    return render_template("bodies_index.html", jurisdictions=result)


@app.route("/bodies/<slug>")
def body_detail(slug):
    """Show members of a public body with pagination."""
    session = get_session()
    body = session.execute(
        select(PublicBody).where(PublicBody.slug == slug)
    ).scalar_one_or_none()
    if not body:
        session.close()
        return "Body not found", 404

    jurisdiction = session.execute(
        select(Jurisdiction).where(Jurisdiction.id == body.jurisdiction_id)
    ).scalar_one_or_none()

    page = request.args.get("page", 1, type=int)
    per_page = 10

    total = session.execute(
        select(func.count(PublicBodyMember.id))
        .where(PublicBodyMember.body == body.body_code)
    ).scalar() or 0

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    members = session.execute(
        select(PublicBodyMember)
        .where(PublicBodyMember.body == body.body_code)
        .order_by(PublicBodyMember.active_from.desc().nullslast(), PublicBodyMember.name)
        .offset(offset).limit(per_page)
    ).scalars().all()

    tomorrow = date.today()

    session.close()
    return render_template(
        "body_detail.html",
        body=body,
        jurisdiction=jurisdiction,
        members=members,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
        today=tomorrow,
    )


@app.route("/permits")
@_cache(timeout=604800, query_string=True)  # 7 days — invalidated on sync
def permits_index():
    """Permit overview — aggregate summaries by default, raw list on request."""
    session = get_session()
    view = request.args.get("view", "aggregate")
    jurisdiction_filter = request.args.get("jurisdiction", "")
    category_filter = request.args.get("category", "")
    year_filter = request.args.get("year", "")

    # ── Filter builder for raw-list mode (non-deduped) ──────────────────
    def _base_filter(q):
        if jurisdiction_filter:
            q = q.where(Permit.jurisdiction == jurisdiction_filter)
        if category_filter:
            q = q.where(Permit.normalized_category == category_filter)
        if year_filter:
            q = q.where(Permit.permit_issue_date.startswith(year_filter))
        return q

    # ── Single-pass deduped aggregate ────────────────────────────────────
    # Weekly reports are cumulative snapshots — the same permit can appear
    # in many reports.  We run the dedup CTE ONCE and compute all three
    # aggregate tables in Python to avoid 3x table scans.
    from sqlalchemy import text
    from collections import defaultdict

    parts = []
    params = {}
    if jurisdiction_filter:
        parts.append("p.jurisdiction = :jur")
        params["jur"] = jurisdiction_filter
    if category_filter:
        parts.append("p.normalized_category = :cat")
        params["cat"] = category_filter
    if year_filter:
        parts.append("p.permit_issue_date LIKE :yr")
        params["yr"] = f"{year_filter}%"
    where = " AND ".join(parts) if parts else "1=1"

    sql = f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE {where}
        )
        SELECT d.jurisdiction,
               d.normalized_category,
               d.native_type,
               CAST(NULLIF(d.permit_valuation, '') AS REAL) AS val,
               CAST(NULLIF(d.permit_square_feet, '') AS REAL) AS sqft,
               SUBSTR(d.permit_issue_date, 1, 4) AS yr
        FROM deduped d
        WHERE d.rn = 1
          AND d.permit_issue_date IS NOT NULL
    """

    jur_tot: dict = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0})
    cat_tot: dict = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0})
    type_cnt: dict = defaultdict(int)
    sqft_by_year: dict = defaultdict(lambda: defaultdict(float))
    cnt_by_year: dict = defaultdict(lambda: defaultdict(int))
    all_cats: set = set()

    for r in session.execute(text(sql), params).all():
        j, c, t, v, s, yr = r
        v = v or 0.0
        s = s or 0.0
        if j:
            jt = jur_tot[j]
            jt["count"] += 1
            jt["sqft"] += s
            jt["val"] += v
        cat = c or "Other"
        if cat:
            all_cats.add(cat)
            ct = cat_tot[cat]
            ct["count"] += 1
            ct["sqft"] += s
            ct["val"] += v
            if yr:
                sqft_by_year[yr][cat] += s
                cnt_by_year[yr][cat] += 1
        if t:
            type_cnt[t] += 1

    years = sorted(sqft_by_year.keys())

    # Build chart-data structures inline (no extra API round-trip)
    cats_ordered = sorted(all_cats, key=lambda x: -cat_tot[x]["count"])
    chart_sqft_by_year = {y: {c: sqft_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_cnt_by_year = {y: {c: cnt_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_cat_totals = [
        {"category": c, "sqft": cat_tot[c]["sqft"],
         "valuation": cat_tot[c]["val"], "count": cat_tot[c]["count"]}
        for c in cats_ordered
    ]

    by_jurisdiction = sorted(
        [{"jurisdiction": k, "count": v["count"],
          "total_valuation": v["val"], "total_sqft": v["sqft"],
          "avg_valuation": v["val"] / v["count"] if v["count"] else 0}
         for k, v in jur_tot.items()],
        key=lambda r: r["count"], reverse=True,
    )

    by_category = sorted(
        [{"normalized_category": k, "count": v["count"],
          "total_valuation": v["val"], "total_sqft": v["sqft"]}
         for k, v in cat_tot.items()],
        key=lambda r: r["count"], reverse=True,
    )

    by_type_top = sorted(
        [{"native_type": k, "count": v} for k, v in type_cnt.items()],
        key=lambda r: r["count"], reverse=True,
    )[:20]

    # Available filter options (non-deduped — detection is fine with full set)
    years = session.execute(
        select(Permit.permit_issue_date)
        .distinct()
        .where(Permit.permit_issue_date.isnot(None), Permit.permit_issue_date != "")
        .order_by(Permit.permit_issue_date.desc())
    ).scalars().all()
    # Extract unique years from ISO dates
    years = sorted(set(d[:4] for d in years if d and len(d) >= 4), reverse=True)

    jurisdictions = session.execute(
        select(Permit.jurisdiction).distinct().where(Permit.jurisdiction.isnot(None)).order_by(Permit.jurisdiction)
    ).scalars().all()

    categories = session.execute(
        select(Permit.normalized_category).distinct().where(Permit.normalized_category.isnot(None), Permit.normalized_category != "").order_by(Permit.normalized_category)
    ).scalars().all()

    # Raw list mode
    permits_raw = []
    page = 1
    total_pages = 1
    total = 0
    per_page = 25

    if view == "raw":
        page = request.args.get("page", 1, type=int)
        base_q = select(Permit).order_by(Permit.permit_issue_date.desc().nullslast(), Permit.id.desc())
        count_q = select(func.count(Permit.id))
        base_q = _base_filter(base_q)
        count_q = _base_filter(count_q)
        total = session.execute(count_q).scalar() or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        permits_raw = session.execute(base_q.offset(offset).limit(per_page)).scalars().all()

    session.close()
    return render_template(
        "permits.html",
        view=view,
        by_jurisdiction=by_jurisdiction,
        by_category=by_category,
        by_type_top=by_type_top,
        permits_raw=permits_raw,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
        years=years,
        jurisdictions=jurisdictions,
        categories=categories,
        jurisdiction_filter=jurisdiction_filter,
        category_filter=category_filter,
        year_filter=year_filter,
        chart_data={
            "years": years,
            "sqft_by_year": chart_sqft_by_year,
            "permits_by_year": chart_cnt_by_year,
            "category_totals": chart_cat_totals,
        },
    )


@app.route("/api/permits/chart-data")
def permits_chart_data():
    """JSON endpoint with deduped chart data for the permits template.

    Returns sqft_by_year, permits_by_year, and category_totals,
    optionally filtered by jurisdiction, category, or year.
    """
    session = get_session()
    jf = request.args.get("jurisdiction", "")
    cf = request.args.get("category", "")
    yf = request.args.get("year", "")

    parts = ["1=1"]
    params = {}
    if jf:
        parts.append("p.jurisdiction = :jur")
        params["jur"] = jf
    if cf:
        parts.append("p.normalized_category = :cat")
        params["cat"] = cf
    if yf:
        parts.append("p.permit_issue_date LIKE :yr")
        params["yr"] = f"{yf}%"
    where = " AND ".join(parts)

    from sqlalchemy import text

    # Years that have data, sorted
    years_sql = text(f"""
        SELECT DISTINCT SUBSTR(p.permit_issue_date, 1, 4) AS yr
        FROM permits p
        WHERE p.permit_issue_date IS NOT NULL AND {where}
        ORDER BY yr
    """)
    years = [r[0] for r in session.execute(years_sql, params).all()]

    # Sqft per year per category
    sqft_sql = text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE {where}
        )
        SELECT SUBSTR(d.permit_issue_date, 1, 4) AS yr,
               COALESCE(d.normalized_category, 'Other') AS cat,
               COALESCE(SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)), 0) AS sqft,
               COUNT(*) AS cnt
        FROM deduped d
        WHERE d.rn = 1 AND d.permit_issue_date IS NOT NULL
        GROUP BY yr, cat
        ORDER BY yr, cat
    """)
    sqft_by_year: dict[str, dict[str, float]] = {}
    permits_by_year: dict[str, dict[str, int]] = {}
    for r in session.execute(sqft_sql, params).all():
        yr, cat, sqft, cnt = r
        sqft_by_year.setdefault(yr, {})[cat] = sqft
        permits_by_year.setdefault(yr, {})[cat] = cnt

    # Category totals (all years)
    cat_totals_sql = text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE {where}
        )
        SELECT COALESCE(d.normalized_category, 'Other') AS cat,
               COALESCE(SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)), 0) AS sqft,
               COALESCE(SUM(CAST(NULLIF(d.permit_valuation, '') AS REAL)), 0) AS valuation,
               COUNT(*) AS cnt
        FROM deduped d
        WHERE d.rn = 1
        GROUP BY cat
        ORDER BY cnt DESC
    """)
    category_totals: list[dict] = []
    for r in session.execute(cat_totals_sql, params).all():
        category_totals.append({"category": r[0], "sqft": r[1], "valuation": r[2], "count": r[3]})

    session.close()

    return {
        "years": years,
        "sqft_by_year": sqft_by_year,
        "permits_by_year": permits_by_year,
        "category_totals": category_totals,
    }


@app.route("/permits/category/<category_name>")
def permit_category_detail(category_name):
    """Year-over-year breakdown for a single permit category.

    Shows a line chart and data table of sqft / count / valuation
    across all available years, optionally filtered by jurisdiction.
    """
    session = get_session()
    jurisdiction_filter = request.args.get("jurisdiction", "")

    parts = ["1=1", "d.rn = 1"]
    params = {"cat": category_name}
    if jurisdiction_filter:
        parts.append("d.jurisdiction = :jur")
        params["jur"] = jurisdiction_filter
    where = " AND ".join(parts)

    from sqlalchemy import text

    sql = text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE COALESCE(p.normalized_category, 'Other') = :cat
              AND p.permit_issue_date IS NOT NULL
        )
        SELECT SUBSTR(d.permit_issue_date, 1, 4) AS yr,
               COUNT(*) AS cnt,
               COALESCE(SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)), 0) AS sqft,
               COALESCE(SUM(CAST(NULLIF(d.permit_valuation, '') AS REAL)), 0) AS valuation
        FROM deduped d
        WHERE {where}
        GROUP BY yr
        ORDER BY yr
    """)

    yearly = [
        {"year": r[0], "count": r[1], "sqft": r[2], "valuation": r[3]}
        for r in session.execute(sql, params).all()
    ]

    session.close()
    return render_template(
        "permit_category.html",
        category=category_name,
        yearly=yearly,
        jurisdiction_filter=jurisdiction_filter,
    )


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
    """Member directory — redirect to the unified bodies index."""
    return redirect("/bodies")


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


@app.route("/members/<jurisdiction_slug>/<body_code>/<slug>")
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


@app.route("/members/<slug>")
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
