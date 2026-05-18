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
from typing import Optional

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
    from db import AgendaItemVote, SupervisorVote, Supervisor, MeetingSupervisor, PZItemDetail, BodyMembership, Person, _enhance_member_for_template
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
    from db import Jurisdiction, PublicBody, Supervisor, Permit, PermitReport
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

# ── Cache version — bump to invalidate all cached pages ──────────────────
# Increment whenever data is reclassified or permit type mappings change.
# Included in the cache key so old cached pages are naturally stale.
_CACHE_VERSION = "v8"  # v3 = 2026-05-17 cache versioning + Maricopa units fix

# ── Conditional caching decorator ───────────────────────────────────────────
def _cache(timeout=60, query_string=False):
    """Apply Flask-Caching if available, otherwise no-op.

    Versions the cache key via _CACHE_VERSION so reclassification or
    data migrations naturally invalidate stale cached pages.
    """
    if cache:
        original_cached = cache.cached(timeout=timeout, query_string=query_string)
        def _wrapper(fn):
            @wraps(fn)
            def _versioned(*args, **kwargs):
                from flask import request
                if hasattr(request, 'args'):
                    old = dict(request.args)
                    request.args = request.args.copy()
                    request.args['_cv'] = _CACHE_VERSION
                try:
                    return original_cached(fn)(*args, **kwargs)
                finally:
                    if old:
                        request.args = type(request.args)(old)
            return _versioned
        return _wrapper
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
        .order_by(AgendaItem.sort_order.asc().nulls_last(), AgendaItem.agenda_item_number)
    ).scalars().all()

    # --- Batch-load supporting docs per item ---
    docs_by_item: dict[int, list] = {}
    meeting_docs: list = []
    docs = session.execute(
        select(SupportingDocument)
        .where(
            SupportingDocument.body == meeting_body_val,
            SupportingDocument.meeting_id == meeting_id,
        )
        .order_by(SupportingDocument.agenda_item_number, SupportingDocument.id)
    ).scalars().all()
    for d in docs:
        if not d.agenda_item_number or d.agenda_item_number == "0" or d.agenda_item_number == 0:
            meeting_docs.append(d)
        else:
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
        # Normalize to string key for lookup
        vote_key = str(av.agenda_item_number) if av.agenda_item_number is not None else ""
        votes_by_item[vote_key] = {
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
        meeting_docs=meeting_docs,
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

    today = date.today()

    # Get total count of distinct people with memberships in this body
    total = session.execute(
        select(func.count(BodyMembership.person_id.distinct()))
        .where(BodyMembership.public_body_id == body.id)
    ).scalar() or 0

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    # Get latest membership per person (for display), sorted by term_start desc
    # then name.  This shows current+past members, like the old query.
    members = session.execute(
        select(Person)
        .join(BodyMembership, BodyMembership.person_id == Person.id)
        .where(BodyMembership.public_body_id == body.id)
        .order_by(BodyMembership.term_start.desc().nullslast(), Person.name)
        .offset(offset).limit(per_page)
    ).scalars().all()

    # Deduplicate by person_id (a person might have multiple memberships)
    seen = set()
    deduped = []
    for m in members:
        if m.id not in seen:
            seen.add(m.id)
            deduped.append(m)
    members = deduped

    # Add computed fields for template compatibility
    # (active_from/active_to/role/body pulled from most recent membership)
    from db import _enhance_member_for_template
    members = [_enhance_member_for_template(m, body.id) for m in members]

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
        today=today,
    )


@app.route("/permits")
@_cache(timeout=604800, query_string=True)  # 7 days — invalidated on sync
def permits_index():
    """Permit overview — aggregate summaries by default, raw list on request."""
    session = get_session()
    view = request.args.get("view", "aggregate")
    jurisdiction_filter = request.args.get("jurisdiction", "")
    category_filter = request.args.get("category", "")  # legacy single-category filter
    year_filter = request.args.get("year", "")
    native_type_filter = request.args.get("native_type", "").strip()
    units_filter = request.args.get("_units", "").strip().lower() == "true"

    # ── New positive inclusion filters ──
    categories_filter = request.args.get("categories", "").strip()
    work_types_filter = request.args.get("work_types", "").strip()

    from sqlalchemy import cast, Float, func

    # ── Legacy exclusion filters (backward compat) ──
    exclude_filter = request.args.get("exclude", "").strip()
    exclude_wt_filter = request.args.get("exclude_work_type", "").strip()

    # Convert legacy exclusion to inclusion when possible
    from sqlalchemy import text as _sa_text
    all_categories = sorted(set(
        r[0] for r in session.execute(
            _sa_text("SELECT DISTINCT normalized_category FROM permits WHERE normalized_category IS NOT NULL")
        ).all()
    ))
    all_work_types = sorted(set(
        r[0] for r in session.execute(
            _sa_text("SELECT DISTINCT work_type FROM permits WHERE work_type IS NOT NULL AND work_type != ''")
        ).all()
    ))

    if exclude_filter and not categories_filter:
        excluded = set(c.strip() for c in exclude_filter.split(",") if c.strip())
        included = [c for c in all_categories if c not in excluded]
        if included:
            categories_filter = ",".join(included)
        exclude_filter = ""  # clear so template doesn't show old UI

    if exclude_wt_filter and not work_types_filter:
        excluded = set(w.strip() for w in exclude_wt_filter.split(",") if w.strip())
        included = [w for w in all_work_types if w not in excluded]
        if included:
            work_types_filter = ",".join(included)
        exclude_wt_filter = ""  # clear

    # Parse inclusion lists
    selected_categories = [c.strip() for c in categories_filter.split(",") if c.strip()]
    selected_work_types = [w.strip() for w in work_types_filter.split(",") if w.strip()]

    # Gather distinct work_types for filter UI
    work_types_all = all_work_types

    # ── Helper: build inclusion-based WHERE clause parts ────────────────
    def _build_parts():
        parts = []
        params = {}
        if jurisdiction_filter:
            parts.append("p.jurisdiction = :jur")
            params["jur"] = jurisdiction_filter
        if selected_categories:
            phs = ",".join(f":cat_{i}" for i in range(len(selected_categories)))
            parts.append(f"p.normalized_category IN ({phs})")
            for i, c in enumerate(selected_categories):
                params[f"cat_{i}"] = c
        if selected_work_types:
            phs = ",".join(f":wt_{i}" for i in range(len(selected_work_types)))
            parts.append(f"p.work_type IN ({phs})")
            for i, w in enumerate(selected_work_types):
                params[f"wt_{i}"] = w
        if year_filter:
            parts.append("p.permit_issue_date LIKE :yr")
            params["yr"] = f"{year_filter}%"
        if native_type_filter:
            parts.append("p.native_type = :nt")
            params["nt"] = native_type_filter
        return parts, params

    # ── Filter builder for raw-list mode (non-deduped) ──────────────────
    def _base_filter(q):
        if jurisdiction_filter:
            q = q.where(Permit.jurisdiction == jurisdiction_filter)
        if selected_categories:
            q = q.where(Permit.normalized_category.in_(selected_categories))
        if selected_work_types:
            q = q.where(Permit.work_type.in_(selected_work_types))
        if year_filter:
            q = q.where(Permit.permit_issue_date.startswith(year_filter))
        if native_type_filter:
            q = q.where(Permit.native_type == native_type_filter)
        if units_filter:
            # Show only permits that carry housing units
            q = q.filter(
                func.coalesce(
                    func.cast(Permit.units, Float),
                    func.cast(Permit.no_units, Float),
                    0.0
                ) > 0
            )
        return q

    # ── Single dedup CTE with SQL GROUP BY ────────────────────────────────
    # Weekly reports are cumulative snapshots — the same permit can appear
    # in many reports.  A single dedup pass removes duplicates, then SQL
    # GROUP BY collapses the result into ~hundreds of aggregate rows that
    # Python reshapes for the template/chart structures.
    from sqlalchemy import text as _sa_text
    from collections import defaultdict

    parts, params = _build_parts()
    where = " AND ".join(parts) if parts else "1=1"

    sql = _sa_text(f"""
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
               COALESCE(d.normalized_category, 'Other') AS category,
               d.native_type,
               d.work_type AS wt,
               SUBSTR(d.permit_issue_date, 1, 4) AS yr,
               COUNT(*) AS cnt,
               SUM(CAST(NULLIF(d.permit_valuation, '') AS REAL)) AS tot_val,
               SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)) AS tot_sqft,
               COALESCE(SUM(CAST(NULLIF(COALESCE(d.units, d.no_units, ''), '') AS REAL)), 0) AS tot_units,
               SUM(CASE WHEN LOWER(d.permit_status) IN ('finaled','final','completed','closed') THEN 1 ELSE 0 END) AS completed_cnt,
               SUM(CASE WHEN d.certificate_of_occupancy_date IS NOT NULL AND d.certificate_of_occupancy_date != '' THEN 1 ELSE 0 END) AS co_cnt
        FROM deduped d
        WHERE d.rn = 1
          AND d.permit_issue_date IS NOT NULL
        GROUP BY d.jurisdiction, d.normalized_category, d.native_type, d.work_type, yr
    """)

    jur_tot: dict = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0, "units": 0.0, "completed": 0, "co_issued": 0})
    cat_tot: dict = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0})
    type_cnt: dict = defaultdict(int)
    sqft_by_year: dict = defaultdict(lambda: defaultdict(float))
    cnt_by_year: dict = defaultdict(lambda: defaultdict(int))
    val_by_year: dict = defaultdict(lambda: defaultdict(float))
    all_cats: set = set()
    # Track new housing units (Residential + New Construction) per jurisdiction per year
    residential_units_cache: dict = defaultdict(lambda: defaultdict(int))

    for r in session.execute(sql, params):
        j = r.jurisdiction
        cat = r.category
        t = r.native_type
        yr = r.yr
        cnt = r.cnt or 0
        v = r.tot_val or 0.0
        s = r.tot_sqft or 0.0
        u = r.tot_units or 0.0
        comp = r.completed_cnt or 0
        co = r.co_cnt or 0

        wt = r.wt
        # Track residential new-construction units in the same pass
        is_new_housing = (
            cat == "Residential" and wt == "New Construction" and u > 0
        )

        if j:
            jt = jur_tot[j]
            jt["count"] += cnt
            jt["sqft"] += s
            jt["val"] += v
            jt["units"] += u
            jt["completed"] += comp
            jt["co_issued"] += co

        all_cats.add(cat)
        ct = cat_tot[cat]
        ct["count"] += cnt
        ct["sqft"] += s
        ct["val"] += v
        if yr:
            sqft_by_year[yr][cat] += s
            cnt_by_year[yr][cat] += cnt
            val_by_year[yr][cat] += v
        if t:
            type_cnt[t] += cnt

    # ── Efficient housing unit query (separate per-jurisdiction) ─────────
    # The main CTE over all jurisdictions is too slow for unit tracking.
    # Use two focused sub-queries per jurisdiction:
    #   (A) Standard: normalized_category='Residential' + work_type='New Construction'
    #   (B) Phoenix PDD: housing-capable types (BLD, TCO, COND, LPRN, etc.)
    #       that carry residential units under Commercial/Plan Review codes.
    #       In the PDD system, a 250-unit apartment building gets permit type
    #       BLD (commercial building), not RSF.
    _HOUSING_CAPABLE_PDD = ['BLD','TCO','COND','LPRN','LPRR','LPRM','LPRT','LPRX','CSIT','PRLM','PAPP','PHAS','SCMJ','SCSU']

    _jur_list = session.execute(
        _sa_text("SELECT DISTINCT jurisdiction FROM permits")
    ).scalars().all()

    for _jur in _jur_list:
        if jurisdiction_filter and _jur != jurisdiction_filter:
            continue

        jur_cache = residential_units_cache[_jur]
        is_phoenix = "phoenix" in _jur.lower()
        is_tempe = "tempe" in _jur.lower()
        params = {"j": _jur}
        yr_clause = f"AND p.permit_issue_date LIKE '{year_filter}%'" if year_filter else ""

        # Sub-query A: Standard residential + new construction (legacy ArcGIS codes, RSF, etc.)
        # Skipped for Tempe and Maricopa — handled by sub-query C with smart address dedup
        if not (is_tempe or "maricopa" in _jur.lower()) and not (selected_categories and "Residential" not in selected_categories):
            a_where = f"""p.jurisdiction = :j AND p.normalized_category = 'Residential'
                       AND p.work_type = 'New Construction' {yr_clause}"""
            a_sql = _sa_text(f"""
                WITH deduped AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(p.permit_number, p.row_hash), COALESCE(p.permit_square_feet, '')
                               ORDER BY p.permit_issue_date
                           ) AS rn
                    FROM permits p WHERE {a_where}
                )
                SELECT SUBSTR(d.permit_issue_date, 1, 4) AS yr,
                       SUM(CAST(NULLIF(COALESCE(d.units, d.no_units, ''), '') AS REAL))
                FROM deduped d
                WHERE d.rn = 1 AND d.permit_issue_date IS NOT NULL
                  AND CAST(NULLIF(COALESCE(d.units, d.no_units, ''), '') AS REAL) > 0
                GROUP BY yr ORDER BY yr
            """)
            for row in session.execute(a_sql, params):
                if row[1]: jur_cache[row[0]] += row[1]

        # Sub-query B: Phoenix PDD housing-capable types — smart address dedup
        # Uses same logic as sub-query C: identical unit counts > 1 = stage
        # overcount (plan review → building permit → CO), take MAX.
        # Different counts or identical counts of 1 = separate units, use SUM.
        if is_phoenix and not (selected_work_types and "New Construction" not in selected_work_types):
            ht_list = ",".join(f"'{t}'" for t in _HOUSING_CAPABLE_PDD)
            yr_where = f"AND p.permit_issue_date LIKE '{year_filter}%'" if year_filter else ""
            b_sql = _sa_text(f"""
                SELECT yr, SUM(corrected_units) FROM (
                    SELECT SUBSTR(MIN(permit_issue_date), 1, 4) AS yr,
                           job_address,
                           CASE
                               WHEN MIN(u) = MAX(u) AND MIN(u) > 1 AND COUNT(*) > 1 THEN MAX(u)
                               ELSE SUM(u)
                           END AS corrected_units
                    FROM (
                        SELECT job_address, permit_issue_date,
                               CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) AS u
                        FROM permits
                        WHERE jurisdiction = :j
                          AND normalized_category NOT IN ('Residential','Demolition')
                          AND source_system = 'phoenix_pdd'
                          AND native_type IN ({ht_list})
                          AND job_address IS NOT NULL
                          AND CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) > 0
                          {yr_where.replace('p.','')}
                    )
                    GROUP BY job_address
                    HAVING SUM(u) > 0
                ) GROUP BY yr ORDER BY yr
            """)
            for row in session.execute(b_sql, params):
                if row[1]: jur_cache[row[0]] += int(row[1])

        # Sub-query C: Tempe & Maricopa — smart address dedup
        # Preserves separate 1-unit permits (townhouses, manufactured homes)
        # while collapsing stage overcount (same building, multiple permits).
        if (is_tempe or "maricopa" in _jur.lower()) and not (selected_work_types and "New Construction" not in selected_work_types):
            c_sql = _sa_text(f"""
                SELECT yr, SUM(corrected_units) FROM (
                    SELECT SUBSTR(MIN(permit_issue_date), 1, 4) AS yr,
                           job_address,
                           CASE
                               WHEN MIN(u) = MAX(u) AND MIN(u) > 1 AND COUNT(*) > 1 THEN MAX(u)
                               ELSE SUM(u)
                           END AS corrected_units
                    FROM (
                        SELECT job_address, permit_issue_date,
                               CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) AS u
                        FROM permits
                        WHERE jurisdiction = :j
                          AND normalized_category = 'Residential'
                          AND work_type = 'New Construction'
                          AND job_address IS NOT NULL
                          AND CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) > 0
                          {yr_clause.replace('p.','')}
                    )
                    GROUP BY job_address
                    HAVING SUM(u) > 0
                ) GROUP BY yr ORDER BY yr
            """)
            for row in session.execute(c_sql, params):
                if row[1]: jur_cache[row[0]] += int(row[1])

    # Ensure explicitly selected categories appear in chart data even with zero records
    if selected_categories:
        for c in selected_categories:
            all_cats.add(c)
            if c not in cat_tot:
                cat_tot[c] = {"count": 0, "sqft": 0.0, "val": 0.0}

    years = sorted(sqft_by_year.keys())

    # Build chart-data structures inline (no extra API round-trip)
    cats_ordered = sorted(all_cats, key=lambda x: -cat_tot[x]["count"])
    chart_sqft_by_year = {y: {c: sqft_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_cnt_by_year = {y: {c: cnt_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_val_by_year = {y: {c: val_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_cat_totals = [
        {"category": c, "sqft": cat_tot[c]["sqft"],
         "valuation": cat_tot[c]["val"], "count": cat_tot[c]["count"]}
        for c in cats_ordered
    ]

    _EXCLUDED_JURISDICTIONS = {"City of Chandler"}
    by_jurisdiction = sorted(
        [{"jurisdiction": k, "count": v["count"],
          "total_valuation": v["val"], "total_sqft": v["sqft"],
          "avg_valuation": v["val"] / v["count"] if v["count"] else 0,
          "total_units": v["units"],
          "completed_count": v["completed"],
          "co_issued_count": v["co_issued"]}
         for k, v in jur_tot.items() if k not in _EXCLUDED_JURISDICTIONS],
        key=lambda r: r["count"], reverse=True,
    )

    # all_categories already queried above for backward-compat conversion
    by_category = sorted(
        [{"normalized_category": c,
          "count": cat_tot[c]["count"] if c in cat_tot else 0,
          "total_valuation": cat_tot[c]["val"] if c in cat_tot else 0,
          "total_sqft": cat_tot[c]["sqft"] if c in cat_tot else 0}
         for c in all_categories],
        key=lambda r: r["count"], reverse=True,
    )

    # ── Cross-jurisdiction type label normalization ─────────────────────
    # Phoenix uses short codes (RSF, BLD, SGNP). Tempe and Maricopa use
    # descriptive labels (Building (Residential), New Commercial).
    # Consolidate them into meaningful labels for the Top Types table.
    def _type_label(nt: str) -> str:
        """Map raw native_type to a consolidated, human-readable label."""
        if not nt:
            return "Other"
        code = nt.upper().strip()
        # Phoenix R-prefix codes → Residential
        if code.startswith("RSF") or code.startswith("RSME") or code == "RSP":
            return "Single-Family Home"
        if code.startswith("RS"):
            return "Single-Family Home"
        if code.startswith("RV"):
            return "Residential (Multi-Unit)"
        if code.startswith("RM") and not code.startswith("RMC"):
            return "Multi-Family"
        if code.startswith("RMC") or code.startswith("REC"):
            return "Residential (Commercial)"
        if code == "RPV" or code == "RPBI":
            return "Residential Patio Villa"
        if code == "RE" or code == "REM":
            return "Residential Alteration"
        if code == "RSE":
            return "Residential Alteration"
        if code.startswith("RPSC") or code.startswith("RPR"):
            return "Residential Alteration"
        if code.startswith("RWH") or code.startswith("RFEN"):
            return "Residential Alteration"
        if code.startswith("RNSP") or code == "RDEM":
            return "Residential Demolition"
        if code.startswith("RCIT") or code.startswith("RSTD"):
            return "Residential Addition"
        if code.startswith("R"):
            return "Residential (Other)"
        # Phoenix C-prefix and BLD → Commercial
        if code == "BLD" or code.startswith("BLDS") or code.startswith("BLDA") or code.startswith("BLSC"):
            return "Commercial Building"
        if code.startswith("CSW") or code.startswith("CSL"):
            return "Commercial Shell"
        if code.startswith("CSIT") or code.startswith("CSE") or code.startswith("CSLC"):
            return "Commercial Interior"
        if code.startswith("CCO") or code.startswith("CPR") or code.startswith("CES"):
            return "Commercial Alteration"
        if code.startswith("CGD") or code.startswith("CDW"):
            return "Commercial Grading"
        if code.startswith("CLS") or code.startswith("CLT") or code.startswith("CMC"):
            return "Commercial Construction"
        if code.startswith("CDF") or code.startswith("CPA"):
            return "Commercial Plan/Design"
        if code.startswith("CP") and code != "CPGD":
            return "Commercial Plan/Design"
        if code == "CPGD":
            return "Commercial Grading"
        if code.startswith("C"):
            return "Commercial (Other)"
        # Phoenix trade codes
        if code == "ELEC" or code.startswith("EL") or code == "PLMB" or code == "MECH":
            return "Trade (Elec/Plumb/Mech)"
        if code.startswith("ELEV") or code.startswith("ELFT"):
            return "Trade (Elevator)"
        if code.startswith("EHYD"):
            return "Trade (Hydronic)"
        if code.startswith("ENVR"):
            return "Trade (Environmental)"
        if code.startswith("ETRC"):
            return "Trade (Electrical Tr.)"
        # Phoenix FENCE permits
        if code == "FEN":
            return "Fence/Wall"
        # Phoenix fire codes → Trade
        if code.startswith("F") and len(code) >= 2 and code[1:].isdigit():
            return "Fire System"
        if code.startswith("FPP") or code.startswith("FPS") or code.startswith("FP"):
            return "Fire Protection"
        if code.startswith("FBB") or code.startswith("FITM"):
            return "Fire Protection"
        if code.startswith("FLRV"):
            return "Fire Protection"
        if code.startswith("FLSR") or code.startswith("FOCS") or code.startswith("FPAP"):
            return "Fire Protection"
        # Phoenix sign codes
        if code.startswith("SGN") or code == "S":
            return "Sign"
        # Phoenix SE, SME, SCSR, SP, etc.
        if code.startswith("SE") or code.startswith("SME") or code == "SM":
            return "Service Existing"
        if code.startswith("SP") or code.startswith("SPE") or code.startswith("SPM"):
            return "Trade (Other)"
        if code.startswith("SC"):
            return "Trade (Other)"
        # Phoenix LP/LS codes → Land Use / Plan Review
        if code.startswith("LPRM") or code.startswith("LPRR") or code.startswith("LPRS"):
            return "Plan Review"
        if code.startswith("LP"):
            return "Plan Review"
        if code.startswith("LS"):
            return "Plan Review"
        # Phoenix infrastructure
        if code.startswith("WS"):
            return "Infrastructure (Water/Sewer)"
        if code.startswith("TRFN"):
            return "Infrastructure (Traffic)"
        # Phoenix demolition
        if code.startswith("DEM") or code.startswith("ABND"):
            return "Demolition"
        # Phoenix pool
        if code.startswith("POOL"):
            return "Pool"
        # Phoenix other/existing
        if code.startswith("OE") or code.startswith("OP") or code.startswith("OS"):
            return "Other Existing"
        if code.startswith("OBLD"):
            return "Other Existing"
        if code.startswith("OM"):
            return "Other Existing"
        if code.startswith("PHAS") or code.startswith("PLAT") or code.startswith("PLZA"):
            return "Plans/Zoning"
        if code.startswith("PAPP") or code.startswith("PR"):
            return "Plans/Zoning"
        if code.startswith("COFO") or code.startswith("COFC"):
            return "Certificate of Occupancy"
        if code.startswith("TCO"):
            return "Temp Certificate of Occupancy"
        if code.startswith("MHZ") or code.startswith("MDHM"):
            return "Manufactured/Mobile Home"
        if code.startswith("INSP"):
            return "Inspection"
        if code.startswith("AMND"):
            return "Amendment"
        if code.startswith("EXTR"):
            return "Excavation/Trench"
        if code.startswith("CAT"):
            return "Catenary/Telecom"
        if code.startswith("CHA"):
            return "Change of Use"
        if code.startswith("CHG"):
            return "Change"
        if code.startswith("DAPP") or code.startswith("DEDI"):
            return "Design/Development"
        if code.startswith("SC") or code == "SM" or code == "SP" or code.startswith("SPE"):
            return "Trade (Other)"
        if code.startswith("BLD-") and "RESIDENTIAL" in nt.upper():
            return "Single-Family Home"
        if code.startswith("BLD-") and "COMMERCIAL" in nt.upper():
            return "Commercial Building"
        # Tempe/Maricopa descriptive labels — normalize these too
        low = nt.lower()
        if "residential" in low and ("new" in low or "build" in low):
            return "Single-Family Home"
        if "residential" in low and ("alter" in low or "addition" in low):
            return "Residential Alteration"
        if "commercial" in low and ("new" in low or "build" in low):
            return "Commercial Building"
        if "commercial" in low and "alter" in low:
            return "Commercial Alteration"
        if "trade" in low or "electrical" in low or "plumbing" in low or "mechanical" in low:
            return "Trade (General)"
        if "demolition" in low:
            return "Demolition"
        if "infrastructure" in low or "grading" in low:
            return "Infrastructure"
        if "standard" in low or "plan" in low:
            return "Standard Plan"
        if "sign" in low or "awning" in low:
            return "Sign"
        if "pool" in low or "spa" in low:
            return "Pool/Spa"
        if "fire" in low or "sprinkler" in low or "alarm" in low:
            return "Fire System"
        if "fence" in low or "wall" in low:
            return "Fence/Wall"
        if "roof" in low:
            return "Roof"
        if "solar" in low or "photovoltaic" in low:
            return "Solar/PV"
        if "foundation" in low:
            return "Foundation"
        if "occupancy" in low:
            return "Certificate of Occupancy"
        if "addition" in low:
            return "Addition"
        # Fallback: use the raw type but clean it up a bit
        return nt.strip()

    # Build type counts by consolidated label
    type_label_cnt: dict = defaultdict(int)
    for k, v in type_cnt.items():
        label = _type_label(k)
        type_label_cnt[label] += v

    by_type_top = sorted(
        [{"type": k, "count": v} for k, v in type_label_cnt.items()],
        key=lambda r: r["count"], reverse=True,
    )[:20]

    # Available filter options — respect jurisdiction for year list
    yr_q = select(Permit.permit_issue_date).distinct().where(
        Permit.permit_issue_date.isnot(None), Permit.permit_issue_date != ""
    )
    if jurisdiction_filter:
        yr_q = yr_q.where(Permit.jurisdiction == jurisdiction_filter)
    year_options = session.execute(
        yr_q.order_by(Permit.permit_issue_date.desc())
    ).scalars().all()
    # Extract unique years from ISO dates
    year_options = sorted(set(d[:4] for d in year_options if d and len(d) >= 4), reverse=True)

    # Compute zero-categories note: selected categories with zero matching records
    zero_categories = [c for c in selected_categories if cat_tot.get(c, {}).get("count", 0) == 0]

    jurisdictions = [
        j for j in session.execute(
            select(Permit.jurisdiction).distinct().where(Permit.jurisdiction.isnot(None)).order_by(Permit.jurisdiction)
        ).scalars().all()
        if j not in _EXCLUDED_JURISDICTIONS
    ]

    # When year is selected, also filter jurisdictions to those active that year
    if year_filter:
        jur_q = select(Permit.jurisdiction).distinct().where(
            Permit.jurisdiction.isnot(None),
            Permit.permit_issue_date.startswith(year_filter),
        )
        filtered_jurs = [r[0] for r in session.execute(jur_q.order_by(Permit.jurisdiction)).all()]
        if filtered_jurs:
            jurisdictions = [j for j in filtered_jurs if j not in _EXCLUDED_JURISDICTIONS]

    categories = all_categories  # from backward-compat query above

    # Raw list mode
    permits_raw = []
    page = 1
    total_pages = 1
    total = 0
    per_page = 25

    if view == "raw":
        page = request.args.get("page", 1, type=int)
        if units_filter:
            # Group related permits by address so multiple stages of the same
            # project appear together
            base_q = select(Permit).order_by(Permit.job_address.asc().nullslast(), Permit.permit_issue_date.desc().nullslast(), Permit.id.desc())
        else:
            base_q = select(Permit).order_by(Permit.permit_issue_date.desc().nullslast(), Permit.id.desc())
        count_q = select(func.count(Permit.id))
        base_q = _base_filter(base_q)
        count_q = _base_filter(count_q)
        total = session.execute(count_q).scalar() or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        permits_raw = session.execute(base_q.offset(offset).limit(per_page)).scalars().all()

    # ── Residential units by jurisdiction and year ────────────────────────
    # Data was accumulated from the same dedup pass above (no second CTE scan).
    # residential_units_cache[jur][year] = units
    units_by_jur_year: dict = defaultdict(list)
    all_unit_years: set = set()
    new_housing_by_jur: dict = defaultdict(int)
    for jur, yr_data in residential_units_cache.items():
        for yr, u in yr_data.items():
            if jur not in _EXCLUDED_JURISDICTIONS:
                units_by_jur_year[jur].append({"year": yr, "units": u})
                all_unit_years.add(yr)
                new_housing_by_jur[jur] += u

    # Override total_units in by_jurisdiction with new housing units
    for entry in by_jurisdiction:
        jur = entry["jurisdiction"]
        nh = new_housing_by_jur.get(jur, 0)
        entry["total_units"] = nh

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
        years=year_options,
        jurisdictions=jurisdictions,
        categories=categories,
        jurisdiction_filter=jurisdiction_filter,
        category_filter=category_filter,
        year_filter=year_filter,
        categories_filter=categories_filter,
        work_types_filter=work_types_filter,
        selected_categories=selected_categories,
        selected_work_types=selected_work_types,
        zero_categories=zero_categories,
        work_types_all=work_types_all,
        units_filter=units_filter,
        native_type_filter=native_type_filter,
        chart_data={
            "years": years,
            "sqft_by_year": chart_sqft_by_year,
            "permits_by_year": chart_cnt_by_year,
            "valuation_by_year": chart_val_by_year,
            "category_totals": chart_cat_totals,
            "residential_units": {
                "by_jurisdiction": dict(units_by_jur_year),
                "years": sorted(all_unit_years),
            },
        },
    )


@app.route("/api/permits/chart-data")
def permits_chart_data():
    """JSON endpoint with deduped chart data for the permits template.

    Returns sqft_by_year, permits_by_year, and category_totals,
    optionally filtered by jurisdiction, category, work type, or year.
    """
    from sqlalchemy import text
    session = get_session()
    jf = request.args.get("jurisdiction", "")
    cf = request.args.get("category", "")
    yf = request.args.get("year", "")

    # New positive inclusion filters
    categories_filter = request.args.get("categories", "").strip()
    work_types_filter = request.args.get("work_types", "").strip()

    # Legacy exclusion filters (backward compat)
    ef = request.args.get("exclude", "").strip()
    ewtf = request.args.get("exclude_work_type", "").strip()

    # Convert legacy exclusion to inclusion
    if ef and not categories_filter:
        all_cats = sorted(set(
            r[0] for r in session.execute(
                text("SELECT DISTINCT normalized_category FROM permits WHERE normalized_category IS NOT NULL")
            ).all()
        ))
        excluded = set(c.strip() for c in ef.split(",") if c.strip())
        included = [c for c in all_cats if c not in excluded]
        if included:
            categories_filter = ",".join(included)
        ef = ""

    if ewtf and not work_types_filter:
        all_wts = sorted(set(
            r[0] for r in session.execute(
                text("SELECT DISTINCT work_type FROM permits WHERE work_type IS NOT NULL AND work_type != ''")
            ).all()
        ))
        excluded = set(w.strip() for w in ewtf.split(",") if w.strip())
        included = [w for w in all_wts if w not in excluded]
        if included:
            work_types_filter = ",".join(included)
        ewtf = ""

    selected_cats = [c.strip() for c in categories_filter.split(",") if c.strip()]
    selected_wts = [w.strip() for w in work_types_filter.split(",") if w.strip()]

    parts = ["1=1"]
    params = {}
    if jf:
        parts.append("p.jurisdiction = :jur")
        params["jur"] = jf
    if cf:
        parts.append("p.normalized_category = :cat")
        params["cat"] = cf
    if selected_cats:
        phs = ",".join(f":cat_{i}" for i in range(len(selected_cats)))
        parts.append(f"p.normalized_category IN ({phs})")
        for i, c in enumerate(selected_cats):
            params[f"cat_{i}"] = c
    if selected_wts:
        phs = ",".join(f":wt_{i}" for i in range(len(selected_wts)))
        parts.append(f"p.work_type IN ({phs})")
        for i, w in enumerate(selected_wts):
            params[f"wt_{i}"] = w
    if yf:
        parts.append("p.permit_issue_date LIKE :yr")
        params["yr"] = f"{yf}%"
    where = " AND ".join(parts)

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
    exclude_filter = request.args.get("exclude", "").strip()
    exclude_cats = [c.strip() for c in exclude_filter.split(",") if c.strip()]

    parts = ["1=1", "d.rn = 1"]
    params = {"cat": category_name}
    if jurisdiction_filter:
        parts.append("d.jurisdiction = :jur")
        params["jur"] = jurisdiction_filter
    if exclude_cats:
        placeholders = ",".join(f":exc_{i}" for i in range(len(exclude_cats)))
        parts.append(f"d.normalized_category NOT IN ({placeholders})")
        for i, c in enumerate(exclude_cats):
            params[f"exc_{i}"] = c
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


# ── Permit Type Code Reference ────────────────────────────────────────────────
# The primary source for Phoenix is the PDD Online search page dropdown at:
#   https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit
# This is the canonical list of 483 codes with official descriptions.
# Secondary source: ArcGIS Planning_Permit MapServer (PER_TYPE_DESC) for older
# codes (RSF, RSME, etc.) not in the current PDD Online system.
# Last updated: 2026-05-17

_OFFICIAL_PHOENIX_CACHE: Optional[dict] = None  # {"permit_types": {...}, "structure_classes": {...}}
_OFFICIAL_FETCH_TIME: float = 0
_OFFICIAL_CACHE_TTL: int = 86400  # re-fetch every 24 hours


# Codes that belong to the structure-class dropdown (ddlStructureClass) on the
# PDD search page. These are numeric 001-997 series and a few alpha codes.
# Any code from this set is treated as a structure class, not a permit type.
_PHX_STRUCTURE_CLASS_CODES: set = set()


def _fetch_phoenix_official_codes() -> Optional[dict]:
    """Fetch the official Phoenix PDD codes from the PDD Online search page.

    The page at https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit has two
    dropdowns:
      - ddlPermitType (413 options): permit type codes (RVSN, LPRM, BLD, etc.)
      - ddlStructureClass (70 options): structure class codes (001-997 series)

    Returns a dict of {code: full_description} merging both lists.
    Codes from ddlStructureClass are noted in their description.
    """
    global _OFFICIAL_PHOENIX_CACHE, _OFFICIAL_FETCH_TIME, _PHX_STRUCTURE_CLASS_CODES
    now = time.time()
    if _OFFICIAL_PHOENIX_CACHE is not None and (now - _OFFICIAL_FETCH_TIME) < _OFFICIAL_CACHE_TTL:
        return _OFFICIAL_PHOENIX_CACHE

    try:
        import urllib.request, re
        req = urllib.request.Request(
            "https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8")

        # Parse structure class dropdown (ddlStructureClass)
        structure_classes = {}
        sc_match = re.search(
            r'<select[^>]*id="ddlStructureClass"[^>]*>(.*?)</select>',
            html, re.DOTALL
        )
        if sc_match:
            for m in re.finditer(r'<option[^>]*value="([^"]+)"[^>]*>([^<]+)</option>', sc_match.group(1)):
                code = m.group(1).strip()
                if not code or code.startswith("-ALL-"):
                    continue
                label = m.group(2).strip()
                if " - " in label:
                    desc = label.split(" - ", 1)[1].strip()
                else:
                    desc = label
                structure_classes[code] = desc

        # Parse permit type dropdown (ddlPermitType)
        permit_types = {}
        pt_match = re.search(
            r'<select[^>]*id="ddlPermitType"[^>]*>(.*?)</select>',
            html, re.DOTALL
        )
        if pt_match:
            for m in re.finditer(r'<option[^>]*value="([^"]+)"[^>]*>([^<]+)</option>', pt_match.group(1)):
                code = m.group(1).strip()
                if not code or code.startswith("-ALL-"):
                    continue
                label = m.group(2).strip()
                if " - " in label:
                    desc = label.split(" - ", 1)[1].strip()
                else:
                    desc = label
                permit_types[code] = desc

        # Merge: permit types first, then structure classes as a separate set
        all_codes = dict(permit_types)
        all_codes.update(structure_classes)

        # Store the structure class set for downstream use
        _PHX_STRUCTURE_CLASS_CODES = set(structure_classes.keys())

        _OFFICIAL_PHOENIX_CACHE = all_codes
        _OFFICIAL_FETCH_TIME = now
        log.info(
            f"Fetched Phoenix codes: {len(permit_types)} permit types, "
            f"{len(structure_classes)} structure classes"
        )
        return all_codes
    except Exception as e:
        log.warning(f"Failed to fetch official Phoenix codes: {e}")
        return _OFFICIAL_PHOENIX_CACHE or {}


# This old hard-coded reference is now replaced by the live fetch above.
# It is kept only for offline/fallback scenarios.
_PHOENIX_FALLBACK_TYPE_CODES = {
    "RSF": {
        "full_name": "RES STRUC",
        "category": "Residential",
        "work_type": "New Construction",
        "scope": "RETAINING WALL",
        "description": "Residential structural permit (e.g. retaining wall)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RSFC": {
        "full_name": "RESIDENTIAL SINGLE FAMILY - SELF CERT",
        "category": "Residential",
        "work_type": "New Construction",
        "scope": "CUSTOM RESIDENCE",
        "description": "Single-family home via self-certification path",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RSC": {
        "full_name": "RESIDENTIAL PERMIT - SELF CERT",
        "category": "Residential",
        "work_type": "New Construction",
        "scope": "ADDITION TO EXISTING RESIDENCE",
        "description": "Residential self-certification permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RSME": {
        "full_name": "RES STRUC/MECH OR PLMB/ELEC",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "ADDITION TO EXISTING RESIDENCE",
        "description": "Residential structural/mechanical/plumbing/electrical alteration on an existing home. Also used for fire rehabilitation remodels.",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC; Phoenix.gov inspections page",
        "verified": True,
    },
    "RSP": {
        "full_name": None,
        "category": "Residential",
        "work_type": "New Construction",
        "scope": None,
        "description": "Likely single-family patio home (zero-lot-line product)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "RM": {
        "full_name": None,
        "category": "Residential",
        "work_type": "New Construction",
        "scope": None,
        "description": "Residential multifamily — apartments, condos, townhomes",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "RPV": {
        "full_name": "RES PHOTOVOLTAIC SYSTEM",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "PHOTOVOLTAIC SYSTEM",
        "description": "Residential solar panel installation",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RE": {
        "full_name": "RES ELEC",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "PHOTOVOLTAIC SYSTEM",
        "description": "Residential electrical permit (often PV solar)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "REM": {
        "full_name": "RES ELEC/MECH OR PLMB",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "RESIDENTIAL MISCELLANEOUS",
        "description": "Residential electrical/mechanical/plumbing miscellaneous work",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RDEM": {
        "full_name": "RES DEMOLITION",
        "category": "Residential",
        "work_type": "Demolition",
        "scope": "RES AS-BUILT NON-PERMITTED CONSTRUCTION",
        "description": "Residential demolition",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── RV* prefix — STATUS: LIKELY MISCLASSIFIED ──
    "RVSN": {
        "full_name": None,
        "category": "Commercial (inferred)",
        "work_type": "Trade (inferred)",
        "scope": None,
        "description": "⚠️ Likely NOT residential. All known instances are fire-sprinkler permits at TSMC semiconductor fab (32200 N 43RD AVE). Needs official definition.",
        "source": "PDD CSV sample data — not present in ArcGIS",
        "verified": False,
    },
    "RVSX": {
        "full_name": None,
        "category": None,
        "work_type": None,
        "scope": None,
        "description": "⚠️ Unknown code — same RV prefix as RVSN; may not be residential",
        "source": "PDD CSV export only",
        "verified": False,
    },
    "RVSC": {
        "full_name": None,
        "category": None,
        "work_type": None,
        "scope": None,
        "description": "⚠️ Unknown code — same RV prefix as RVSN; may not be residential",
        "source": "PDD CSV export only",
        "verified": False,
    },
    "RVCA": {
        "full_name": None,
        "category": None,
        "work_type": None,
        "scope": None,
        "description": "⚠️ Unknown code — same RV prefix as RVSN; may not be residential",
        "source": "PDD CSV export only",
        "verified": False,
    },
    # ── Commercial / Building codes ──
    "BLD": {
        "full_name": "STRUC/ELEC/PLMB/MECH",
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": "COMMERCIAL REMODEL",
        "description": "General commercial building permit (structural/electrical/plumbing/mechanical)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "BLDS": {
        "full_name": "SHELL - STRUC/ELEC/PLMB/MECH",
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": "COMMERCIAL NEW",
        "description": "Commercial shell building (new construction)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "OBLD": {
        "full_name": "OTC STRUC/ELEC/PLMB/MECH",
        "category": "Other",
        "work_type": "Alteration",
        "scope": "COMMERCIAL REMODEL",
        "description": "Over-the-counter commercial building permit (small remodel)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── Sign permits ──
    "SGNP": {
        "full_name": "SIGN PERMIT",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": "COMMERCIAL SIGN APPLICATON REVIEW",
        "description": "Permanent sign permit (SGN = Sign, P = Permanent)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "SGNT": {
        "full_name": "SIGN TEMPORARY PERMIT",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": "TEMPORARY SIGN",
        "description": "Temporary sign permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "SGNV": {
        "full_name": "SIGN VIOLATION",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": "SIGN INSPECTION",
        "description": "Sign violation inspection",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── Land-use permits ──
    "LPRM": {
        "full_name": None,
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": None,
        "description": "Land-Use Permit, Plan Review (LP=Land-Use, RM=Review Major, hypothesized)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "LPRN": {
        "full_name": None,
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": None,
        "description": "Land-Use Permit, New (LP=Land-Use, RN=New, hypothesized)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "LPRS": {
        "full_name": None,
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": None,
        "description": "Land-Use Permit, Site Plan (LP=Land-Use, RS=Site, hypothesized)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    # ── Fire / Safety ──
    "FPSR": {
        "full_name": "FIRE PREVENTION SERVICE REQUEST",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": None,
        "description": "Fire prevention service request / inspection",
        "source": "ArcGIS PER_TYPE_DESC",
        "verified": True,
    },
    # ── Demolition ──
    "DEM": {
        "full_name": "DEMOLITION",
        "category": "Demolition",
        "work_type": "Demolition",
        "scope": "DEMO PERMIT ONLY",
        "description": "Standard demolition permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── Other ──
    "EXTR": {
        "full_name": "EXTENDED CONSTRUCTION WORK HOURS RENEWAL",
        "category": "Other",
        "work_type": "Alteration",
        "scope": "EXTENDED CONSTRUCTION WORK HOURS",
        "description": "Extended work hours permit (construction noise variance)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "ERES": {
        "full_name": "RESIDENTIAL ELEVATOR",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "PRIVATE RESIDENTIAL ELEVATOR--ELEVINSP",
        "description": "Private residential elevator installation",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "SHOR": {
        "full_name": "SHORING PERMIT",
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": "SHORING",
        "description": "Excavation shoring permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "BMR": {
        "full_name": "BUILDING MAINTENANCE REGISTRATION",
        "category": "Commercial",
        "work_type": "Alteration",
        "scope": "BUILDING MAINTENANCE REGISTRATION",
        "description": "Building maintenance registration (annual program)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
}


def _get_type_codes_for_jurisdiction(jurisdiction: str) -> dict:
    """Return type code reference for a given jurisdiction.

    For Phoenix, the primary source is the official PDD Online search page
    dropdown (483 codes). Falls back to permit data for codes not in that list.
    Returns a dict of {code: info} suitable for JSON serialization.
    """
    from scripts.scraper.phoenix_permits import (
        PHX_CATEGORY_MAP, PHX_WORK_TYPE_MAP, categorize_phoenix_type,
    )
    jur_lower = jurisdiction.lower().strip() if jurisdiction else ""

    if "phoenix" in jur_lower:
        official = _fetch_phoenix_official_codes()
        result = {}

        # Classify: figure out which codes are structure classes vs permit types
        is_structure_class = _PHX_STRUCTURE_CLASS_CODES

        # First pass: all codes from the official PDD Online dropdown
        for code, desc in official.items():
            cat, wt = categorize_phoenix_type(code)
            if code in is_structure_class:
                # Structure class codes (001-997) describe the building type,
                # not the permit type. They're used alongside permit types.
                result[code] = {
                    "full_name": desc,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": f"Structure class: {desc}. Used alongside a permit type code (e.g. BLD + structure class 330) to describe what kind of building the work is on.",
                    "source": "Phoenix PDD Online (official) — ddlStructureClass dropdown",
                    "verified": True,
                    "is_structure_class": True,
                }
            else:
                result[code] = {
                    "full_name": desc,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": desc,
                    "source": "Phoenix PDD Online (official)",
                    "verified": True,
                    "is_structure_class": False,
                }

        # Second pass: codes in permit data not in the official list
        # (older ArcGIS codes like RSF, RSME, etc.)
        session = get_session()
        from sqlalchemy import text
        known_types = session.execute(
            text("SELECT DISTINCT native_type FROM permits WHERE jurisdiction = :j AND native_type IS NOT NULL"),
            {"j": jurisdiction},
        ).scalars().all()
        session.close()

        for t in known_types:
            if t and t not in result:
                cat, wt = categorize_phoenix_type(t)
                result[t] = {
                    "full_name": None,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": f"Code from ArcGIS permit data — not in current PDD Online system",
                    "source": "ArcGIS permit data",
                    "verified": False,
                }

        return result

    if "tempe" in jur_lower:
        # Tempe uses Accela-based classification codes in raw_permit_class.
        # These are already descriptive labels like "330 - Commercial Buildings".
        from scripts.scraper.tempe_permits import categorize_permit, classify_work_type
        session = get_session()
        from sqlalchemy import text
        known_classes = session.execute(
            text("SELECT DISTINCT raw_permit_class FROM permits WHERE jurisdiction = :j AND raw_permit_class IS NOT NULL AND raw_permit_class != '' ORDER BY raw_permit_class"),
            {"j": jurisdiction},
        ).scalars().all()
        session.close()

        result = {}
        for pc in known_classes:
            if pc:
                cat = categorize_permit(raw_permit_class=pc)
                wt = classify_work_type(raw_permit_class=pc)
                result[pc] = {
                    "full_name": pc,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": pc,
                    "source": "Tempe Accela Civic Platform (raw_permit_class)",
                    "verified": True,
                }
        return result

    # Generic fallback for other jurisdictions
    import re
    session = get_session()
    from sqlalchemy import text
    known_types = session.execute(
        text("SELECT DISTINCT native_type FROM permits WHERE jurisdiction = :j AND native_type IS NOT NULL"),
        {"j": jurisdiction},
    ).scalars().all()
    session.close()

    result = {}
    for t in known_types:
        if t:
            # Check if the type is a descriptive label (contains spaces, parentheses,
            # mixed case) vs. a short code (all-caps, 2-8 chars, no spaces).
            # Descriptive labels ARE the full name already.
            is_short_code = bool(
                t.isupper() and len(t) <= 10 and " " not in t and "(" not in t
            )
            if is_short_code:
                result[t] = {
                    "full_name": None,
                    "category": None,
                    "work_type": None,
                    "scope": None,
                    "description": "Type code in use — no verified definition available",
                    "source": "From data",
                    "verified": False,
                }
            else:
                result[t] = {
                    "full_name": t,
                    "category": None,
                    "work_type": None,
                    "scope": None,
                    "description": t,
                    "source": "Self-describing label — this IS the full name",
                    "verified": True,
                }
    return result


@app.route("/permits/type-codes")
def permit_type_codes():
    """Reference page showing all permit type codes for a jurisdiction."""
    jurisdiction = request.args.get("jurisdiction", "").strip()
    code_filter = request.args.get("code", "").strip().upper()

    codes = _get_type_codes_for_jurisdiction(jurisdiction) if jurisdiction else {}

    # Always pass the full list — code_filter just sets an anchor target
    # for scrolling to that specific row
    anchor_code = code_filter if code_filter in codes else None

    # Sort: verified first, then alphabetical
    sorted_codes = sorted(codes.items(), key=lambda kv: (not kv[1]["verified"], kv[0]))

    # Get all jurisdictions for the dropdown (always, even when filtered)
    session = get_session()
    known_jurisdictions = [
        j for j in session.execute(
            select(Permit.jurisdiction).distinct().where(Permit.jurisdiction.isnot(None)).order_by(Permit.jurisdiction)
        ).scalars().all()
    ]
    session.close()

    return render_template(
        "permit_type_codes.html",
        codes=sorted_codes,
        jurisdiction=jurisdiction,
        anchor_code=anchor_code,
        jurisdictions=known_jurisdictions,
    )


@app.route("/permits/api/type-codes")
def permit_type_codes_api():
    """JSON endpoint returning type code reference for a jurisdiction."""
    jurisdiction = request.args.get("jurisdiction", "").strip()
    code_filter = request.args.get("code", "").strip().upper()

    codes = _get_type_codes_for_jurisdiction(jurisdiction) if jurisdiction else {}

    if code_filter and code_filter in codes:
        codes = {code_filter: codes[code_filter]}

    return jsonify(codes)


if __name__ == "__main__":
    app.run(debug=True)
