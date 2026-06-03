"""Meetings and index routes blueprint."""

import logging
from datetime import date
from typing import Optional

from flask import Blueprint, render_template, request, redirect, jsonify
from sqlalchemy import select, func, or_, text as sa_text, and_

from db import (
    get_session, Meeting, AgendaItem, SupportingDocument,
    AgendaItemVote, SupervisorVote, Supervisor, MeetingSupervisor,
    PZItemDetail, BodyMembership, Person, _enhance_member_for_template,
    Case, CaseEvent, Jurisdiction, PublicBody,
)
from routes import SYNC_STATUS_BADGES, _cache

log = logging.getLogger(__name__)

meetings_bp = Blueprint("meetings", __name__, url_prefix="")




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

    # Filter by body (now also handles tempe-* and mesa-* codes)
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
            # Tempe subcommittees
            "tempe-animal-welfare-subcommittee": "tempe-animal-welfare-subcommittee",
            "tempe-community-engagement-subcommittee": "tempe-community-engagement-subcommittee",
            "tempe-drink-spiking-subcommittee": "tempe-drink-spiking-subcommittee",
            "tempe-mixed-use-space-subcommittee": "tempe-mixed-use-space-subcommittee",
            "tempe-mobility-safety-subcommittee": "tempe-mobility-safety-subcommittee",
            "tempe-town-lake-subcommittee": "tempe-town-lake-subcommittee",
            "tempe-term-limits-subcommittee": "tempe-term-limits-subcommittee",
            "tempe-advocacy-review-subcommittee": "tempe-advocacy-review-subcommittee",
            # Mesa bodies (Legistar)
            "mesa-cc": "mesa-cc", "mesa city council": "mesa-cc",
            "mesa-pz": "mesa-pz", "mesa planning": "mesa-pz", "mesa planning zoning": "mesa-pz", "mesa-planning-zoning": "mesa-pz",
            "mesa-drb": "mesa-drb", "mesa design review board": "mesa-drb", "mesa-design-review-board": "mesa-drb",
            "mesa-boa": "mesa-boa", "mesa board of adjustment": "mesa-boa", "mesa-board-of-adjustment": "mesa-boa",
            "mesa-hpb": "mesa-hpb", "mesa historic preservation board": "mesa-hpb", "mesa-historic-preservation-board": "mesa-hpb",
            "mesa-cadence": "mesa-cadence", "mesa-cadence-cfd": "mesa-cadence",
            "mesa-eastmark1": "mesa-eastmark1", "mesa-eastmark-cfd-1": "mesa-eastmark1",
            "mesa-eastmark2": "mesa-eastmark2", "mesa-eastmark-cfd-2": "mesa-eastmark2",
            # Chandler bodies (AgendaQuick)
            "chandler-cc": "chandler-cc", "chandler city council": "chandler-cc",
            "chandler-pz": "chandler-pz", "chandler planning": "chandler-pz",
            "chandler-drc": "chandler-drc", "chandler development review": "chandler-drc",
            "chandler-boa": "chandler-boa", "chandler board of adjustment": "chandler-boa",
            "chandler-hpc": "chandler-hpc", "chandler historic preservation": "chandler-hpc",
            # Gilbert bodies (OnBase)
            # Peoria bodies (NovusAgenda)
            # Glendale bodies (AgendaQuick)
            "glendale-cc": "glendale-cc", "glendale city council": "glendale-cc",
            "glendale-pc": "glendale-pc", "glendale planning commission": "glendale-pc",
            "glendale-boa": "glendale-boa", "glendale board of adjustment": "glendale-boa",
            # Surprise bodies (CivicClerk)
            "surprise-cc": "surprise-cc", "surprise city council": "surprise-cc",
            "surprise-pz": "surprise-pz", "surprise planning and zoning": "surprise-pz",
            "peoria-cc": "peoria-cc", "peoria city council": "peoria-cc",
            "peoria-pz": "peoria-pz", "peoria planning": "peoria-pz", "peoria-planning-and-zoning-commission": "peoria-pz",
            "peoria-boa": "peoria-boa", "peoria board of adjustment": "peoria-boa", "peoria-board-of-adjustment": "peoria-boa",
            "peoria-sub": "peoria-sub", "peoria subcommittee": "peoria-sub", "peoria-subcommittee-meeting": "peoria-sub",

            "gilbert-tc": "gilbert-tc", "gilbert town council": "gilbert-tc",
            # MCACC bodies (Maricopa County AgendaCenter)
            "mc-audit": "mc-audit", "audit advisory committee": "mc-audit",
            "mc-benefit-trust": "mc-benefit-trust", "benefit board of trustees": "mc-benefit-trust",
            "mc-community-action": "mc-community-action", "community action commission": "mc-community-action",
            "mc-cdac": "mc-cdac", "community development advisory committee": "mc-cdac",
            "mc-eed-policy": "mc-eed-policy", "early education division policy council": "mc-eed-policy",
            "mc-flood-advisory": "mc-flood-advisory", "flood control advisory board": "mc-flood-advisory",
            "mc-home": "mc-home", "home consortium": "mc-home",
            "mc-mclepc": "mc-mclepc", "local emergency planning committee": "mc-mclepc",
            "mc-mcao-psprs": "mc-mcao-psprs", "mcao public safety personnel retirement": "mc-mcao-psprs",
            "mc-mcso-corp": "mc-mcso-corp", "mcso correctional officer retirement": "mc-mcso-corp",
            "mc-mcso-psprs": "mc-mcso-psprs", "mcso public safety personnel retirement": "mc-mcso-psprs",
            "mc-merit": "mc-merit", "merit systems commission": "mc-merit",
            "mc-psfc": "mc-psfc", "public safety funding committee": "mc-psfc",
            "mc-risk-trust": "mc-risk-trust", "self-insured risk trust fund": "mc-risk-trust",
            "mc-smart-savings": "mc-smart-savings", "smart savings committee": "mc-smart-savings",
            "mc-stadium": "mc-stadium", "stadium district board": "mc-stadium",
            "mc-trp": "mc-trp", "travel reduction program": "mc-trp",
            "mc-air-pollution": "mc-air-pollution", "air pollution hearing board": "mc-air-pollution",
            "mc-bcab": "mc-bcab", "building code advisory board": "mc-bcab",
            "mc-flood-stakeholder": "mc-flood-stakeholder", "flood control district stakeholder group": "mc-flood-stakeholder",
            # Scottsdale bodies
            "scottsdale-cc": "scottsdale-cc", "scottsdale city council": "scottsdale-cc",
            "scottsdale-pc": "scottsdale-pc", "scottsdale planning": "scottsdale-pc",
            "scottsdale-boa": "scottsdale-boa", "scottsdale board of adjustment": "scottsdale-boa",
            "scottsdale-drb": "scottsdale-drb", "scottsdale development review": "scottsdale-drb",
            "scottsdale-hpc": "scottsdale-hpc", "scottsdale historic preservation": "scottsdale-hpc",
            "scottsdale-baba": "scottsdale-baba", "scottsdale building appeals": "scottsdale-baba",
        }
        res = body_map.get(b)
        if res:
            q = q.where(Meeting.body == res)
        else:
            q = q.where(Meeting.body == body)

    rows = session.execute(q).scalars().all()
    session.close()
    return [r for r in rows if r]


def _strip_jurisdiction(body_name: str) -> str:
    """Strip the jurisdiction prefix from a body name.

    Examples:
        "Avondale City Council" → "City Council"
        "Maricopa County Board of Supervisors" → "Board of Supervisors"
        "Chandler Planning & Zoning Commission" → "Planning & Zoning Commission"
        "Industrial Development Authority" → unchanged (no jurisdiction prefix)
    """
    if not body_name:
        return ""
    # Known jurisdiction name prefixes (longest first to match greedily)
    jurisdiction_names = [
        "Maricopa County", "Paradise Valley", "Queen Creek", "El Mirage",
        # Single-word city names — strip only if followed by a space and another word
        "Avondale", "Buckeye", "Chandler", "Gilbert", "Glendale",
        "Goodyear", "Mesa", "Peoria", "Phoenix", "Scottsdale",
        "Surprise", "Tempe",
    ]
    for jur in jurisdiction_names:
        prefix = jur + " "
        if body_name.startswith(prefix):
            return body_name[len(prefix):]
    return body_name


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
        PublicBody.name.label("body_name"),
        PublicBody.body_type,
    ).outerjoin(PublicBody, or_(
        PublicBody.id == Meeting.public_body_id,
        PublicBody.body_code == Meeting.body,
    ))

    # Normalize body code (now also handles tempe-* and mesa-* codes)
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
            # Tempe subcommittees
            "tempe-animal-welfare-subcommittee": "tempe-animal-welfare-subcommittee",
            "tempe-community-engagement-subcommittee": "tempe-community-engagement-subcommittee",
            "tempe-drink-spiking-subcommittee": "tempe-drink-spiking-subcommittee",
            "tempe-mixed-use-space-subcommittee": "tempe-mixed-use-space-subcommittee",
            "tempe-mobility-safety-subcommittee": "tempe-mobility-safety-subcommittee",
            "tempe-town-lake-subcommittee": "tempe-town-lake-subcommittee",
            "tempe-term-limits-subcommittee": "tempe-term-limits-subcommittee",
            "tempe-advocacy-review-subcommittee": "tempe-advocacy-review-subcommittee",
            # Mesa bodies (Legistar)
            "mesa-cc": "mesa-cc", "mesa city council": "mesa-cc",
            "mesa-pz": "mesa-pz", "mesa planning": "mesa-pz", "mesa planning zoning": "mesa-pz", "mesa-planning-zoning": "mesa-pz",
            "mesa-drb": "mesa-drb", "mesa design review board": "mesa-drb", "mesa-design-review-board": "mesa-drb",
            "mesa-boa": "mesa-boa", "mesa board of adjustment": "mesa-boa", "mesa-board-of-adjustment": "mesa-boa",
            "mesa-hpb": "mesa-hpb", "mesa historic preservation board": "mesa-hpb", "mesa-historic-preservation-board": "mesa-hpb",
            "mesa-cadence": "mesa-cadence", "mesa-cadence-cfd": "mesa-cadence",
            "mesa-eastmark1": "mesa-eastmark1", "mesa-eastmark-cfd-1": "mesa-eastmark1",
            "mesa-eastmark2": "mesa-eastmark2", "mesa-eastmark-cfd-2": "mesa-eastmark2",
            # Chandler bodies (AgendaQuick)
            "chandler-cc": "chandler-cc", "chandler city council": "chandler-cc",
            "chandler-pz": "chandler-pz", "chandler planning": "chandler-pz",
            "chandler-drc": "chandler-drc", "chandler development review": "chandler-drc",
            "chandler-boa": "chandler-boa", "chandler board of adjustment": "chandler-boa",
            "chandler-hpc": "chandler-hpc", "chandler historic preservation": "chandler-hpc",
            # Gilbert bodies (OnBase)
            # Peoria bodies (NovusAgenda)
            # Glendale bodies (AgendaQuick)
            "glendale-cc": "glendale-cc", "glendale city council": "glendale-cc",
            "glendale-pc": "glendale-pc", "glendale planning commission": "glendale-pc",
            "glendale-boa": "glendale-boa", "glendale board of adjustment": "glendale-boa",
            # Surprise bodies (CivicClerk)
            "surprise-cc": "surprise-cc", "surprise city council": "surprise-cc",
            "surprise-pz": "surprise-pz", "surprise planning and zoning": "surprise-pz",
            "peoria-cc": "peoria-cc", "peoria city council": "peoria-cc",
            "peoria-pz": "peoria-pz", "peoria planning": "peoria-pz", "peoria-planning-and-zoning-commission": "peoria-pz",
            "peoria-boa": "peoria-boa", "peoria board of adjustment": "peoria-boa", "peoria-board-of-adjustment": "peoria-boa",
            "peoria-sub": "peoria-sub", "peoria subcommittee": "peoria-sub", "peoria-subcommittee-meeting": "peoria-sub",

            "gilbert-tc": "gilbert-tc", "gilbert town council": "gilbert-tc",
            # MCACC bodies (Maricopa County AgendaCenter)
            "mc-audit": "mc-audit",
            "mc-benefit-trust": "mc-benefit-trust",
            "mc-community-action": "mc-community-action",
            "mc-cdac": "mc-cdac",
            "mc-eed-policy": "mc-eed-policy",
            "mc-flood-advisory": "mc-flood-advisory",
            "mc-home": "mc-home",
            "mc-mclepc": "mc-mclepc",
            "mc-mcao-psprs": "mc-mcao-psprs",
            "mc-mcso-corp": "mc-mcso-corp",
            "mc-mcso-psprs": "mc-mcso-psprs",
            "mc-merit": "mc-merit",
            "mc-psfc": "mc-psfc",
            "mc-risk-trust": "mc-risk-trust",
            "mc-smart-savings": "mc-smart-savings",
            "mc-stadium": "mc-stadium",
            "mc-trp": "mc-trp",
            "mc-air-pollution": "mc-air-pollution",
            "mc-bcab": "mc-bcab",
            "mc-flood-stakeholder": "mc-flood-stakeholder",
            # Scottsdale bodies
            "scottsdale-cc": "scottsdale-cc", "scottsdale city council": "scottsdale-cc",
            "scottsdale-pc": "scottsdale-pc", "scottsdale planning": "scottsdale-pc",
            "scottsdale-boa": "scottsdale-boa", "scottsdale board of adjustment": "scottsdale-boa",
            "scottsdale-drb": "scottsdale-drb", "scottsdale development review": "scottsdale-drb",
            "scottsdale-hpc": "scottsdale-hpc", "scottsdale historic preservation": "scottsdale-hpc",
            "scottsdale-baba": "scottsdale-baba", "scottsdale building appeals": "scottsdale-baba",
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

    # ── Badge color scheme by body_type ──
    # Colors come from public_bodies.body_type classification,
    # not from per-jurisdiction elif trees.
    _BODY_TYPE_BADGE = {
        "primary": "primary",            # City Council, BOS, Town Council
        "land_use": "secondary",          # P&Z, DRC, BOA, HPC
        "community_services": "success",  # Health, housing, human services
        "fiscal_oversight": "danger",     # Audit, PSPRS, risk, benefits
        "culture_recreation": "info",     # Arts, museums, parks, libraries
        "infrastructure": "warning",      # Transportation, flood, water, airport
        "advisory_general": "dark",       # Neighborhood, economic dev, misc
    }

    meetings_list = []
    for row in rows:
        body_val = row.body or "bos"
        body_type = row.body_type or "advisory_general"

        # Badge text: full body name with jurisdiction stripped (shown in separate column)
        body_name = row.body_name or row.meeting_type or body_val
        source = _strip_jurisdiction(body_name)
        source_badge = _BODY_TYPE_BADGE.get(body_type, "dark")

        # Resolve jurisdiction name from meeting.jurisdiction_id
        jur_map = {
            1: ("Maricopa County", "maricopa-county"),
            2: ("Tempe", "tempe"),
            3: ("Chandler", "chandler"),
            4: ("Phoenix", "phoenix"),
            5: ("Mesa", "mesa"),
            6: ("Gilbert", "gilbert"),
            7: ("Scottsdale", "scottsdale"),
            10: ("Peoria", "peoria"),
            11: ("Glendale", "glendale"),
            12: ("Surprise", "surprise"),
            13: ("Buckeye", "buckeye"),
            14: ("Avondale", "avondale"),
            15: ("El Mirage", "el-mirage"),
            16: ("Goodyear", "goodyear"),
        }
        jur_name, jur_slug = jur_map.get(row.jurisdiction_id, ("Maricopa County", "maricopa-county"))

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


@meetings_bp.route("/meetings")
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


@meetings_bp.route("/meetings/<path:meeting_id>")
@meetings_bp.route("/meetings/<body>/<path:meeting_id>")
@_cache(timeout=120, query_string=True)
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
    meeting_pk = meeting.id

    # Try to construct a minutes URL from the source URL
    # AgendaCenter: replace Agenda with Minutes, strip ?html=true
    # Destiny (Chandler/Glendale): look for chanddocs/glendocs in source
    minutes_url = ""
    if meeting.source_url:
        src = meeting.source_url
        if "AgendaCenter/ViewFile/Agenda" in src:
            minutes_url = src.replace("/Agenda/", "/Minutes/").replace("?html=true", "")
        elif "/chanddocs/" in src or "/glendocs/" in src:
            # These are already PDFs — minutes may be at a similar path
            pass

    # --- Agenda items ---
    items = session.execute(
        select(AgendaItem)
        .where(
            AgendaItem.meeting_db_id == meeting_pk,
        )
        .order_by(AgendaItem.sort_order.asc().nulls_last(), AgendaItem.agenda_item_number)
    ).scalars().all()

    # --- Batch-load supporting docs per item ---
    docs_by_item: dict[int, list] = {}
    meeting_docs: list = []
    docs = session.execute(
        select(SupportingDocument)
        .where(
            SupportingDocument.meeting_db_id == meeting_pk,
        )
        .order_by(SupportingDocument.agenda_item_number, SupportingDocument.id)
    ).scalars().all()
    # Build a lookup: agenda_item_number -> list of item PKs that share that number
    num_to_item_ids: dict[str, list[int]] = {}
    for ai in items:
        key = str(ai.agenda_item_number) if ai.agenda_item_number is not None else ""
        if key:
            num_to_item_ids.setdefault(key, []).append(ai.id)
    for d in docs:
        if not d.agenda_item_number or d.agenda_item_number == "0" or d.agenda_item_number == 0:
            meeting_docs.append(d)
        else:
            # Assign to the last item with this number (sub-items appear after parent)
            ids = num_to_item_ids.get(str(d.agenda_item_number), [])
            if ids:
                target_id = ids[-1]  # last item with this number
                docs_by_item.setdefault(target_id, []).append(d)
            else:
                meeting_docs.append(d)

    # --- Batch-load votes per item ---
    votes_by_item: dict[int, dict] = {}
    item_votes = session.execute(
        select(AgendaItemVote)
        .where(
            AgendaItemVote.meeting_db_id == meeting_pk,
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

        # For PZ meetings, also load MemberVote records
        if meeting_body_val == "pz":
            from db.models import MemberVote, Person as _Person
            mv_rows = session.execute(
                select(MemberVote, _Person.name, _Person.normalized_name)
                .join(_Person, MemberVote.member_id == _Person.id)
                .where(MemberVote.agenda_item_vote_id.in_(vote_ids))
            ).all()
            for mv, pname, pnorm in mv_rows:
                slug = pnorm.replace(" ", "-") if pnorm else ""
                supervisor_votes_by_vote.setdefault(mv.agenda_item_vote_id, []).append(
                    {"name": pname, "vote": mv.vote, "slug": slug, "is_inferred": False}
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
                PZItemDetail.meeting_db_id == meeting_pk,
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

    # Resolve jurisdiction slug from meeting
    from db.models import Jurisdiction as _Jur
    _jur = session.execute(
        select(_Jur).where(_Jur.id == (meeting.jurisdiction_id or 1))
    ).scalar_one_or_none()
    jurisdiction_slug = _jur.slug if _jur else "maricopa-county"

    session.close()

    badge = SYNC_STATUS_BADGES.get((meeting.sync_status or "").lower(), "secondary")

    # Item-specific deep link
    scroll_item = request.args.get("item", "").strip()

    return render_template(
        "meeting_detail.html",
        meeting=meeting,
        meeting_id=meeting_id,
        body_code=meeting_body_val,
        jurisdiction_slug=jurisdiction_slug,
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
        minutes_url=minutes_url,
        scroll_item=scroll_item,
    )


@meetings_bp.route("/c-number/<c_number_base>")
def c_number_revisions(c_number_base):
    """Show all agenda items sharing the same c_number_base."""
    session = get_session()

    items = session.execute(
        select(
            AgendaItem.meeting_id,
            AgendaItem.agenda_item_number,
            AgendaItem.agenda_item_title,
            AgendaItem.agenda_item_text,
            AgendaItem.c_number,
            AgendaItem.c_number_base,
            AgendaItem.c_number_revision,
            AgendaItem.vote_or_action,
            Meeting.meeting_date,
            Meeting.meeting_type,
            Meeting.meeting_body,
            Meeting.body,
        )
        .join(Meeting, Meeting.id == AgendaItem.meeting_db_id)
        .where(
            or_(
                AgendaItem.c_number_base == c_number_base,
                AgendaItem.c_number == c_number_base,
            )
        )
        .order_by(Meeting.meeting_date, AgendaItem.agenda_item_number)
    ).all()

    # Batch-load supporting docs and vote data
    from db.models import SupportingDocument as _SD, AgendaItemVote, SupervisorVote, Person as _Person, MemberVote

    doc_keys = [(r.meeting_id, r.agenda_item_number) for r in items]
    docs_by_item: dict[str, list] = {}
    if doc_keys:
        from sqlalchemy import or_ as _or
        conditions = [
            (_SD.body == Meeting.body) &
            (_SD.meeting_id == k[0]) &
            (_SD.agenda_item_number == k[1])
            for k in doc_keys
        ]
        all_docs = session.execute(
            select(_SD, Meeting.body)
            .join(Meeting, and_(Meeting.meeting_id == _SD.meeting_id, Meeting.body == _SD.body))
            .where(or_(*conditions))
        ).all()
        for sd, body_code in all_docs:
            key = f"{sd.meeting_id}:{sd.agenda_item_number}"
            docs_by_item.setdefault(key, []).append(sd)

    votes_by_item: dict[str, dict] = {}
    for r in items:
        key = f"{r.meeting_id}:{r.agenda_item_number}"
        aiv = session.execute(
            select(AgendaItemVote).where(
                AgendaItemVote.meeting_id == r.meeting_id,
                AgendaItemVote.body == r.body,
                AgendaItemVote.agenda_item_number == r.agenda_item_number,
            )
        ).scalar_one_or_none()
        if aiv:
            member_votes = []
            if aiv.body == "pz":
                mv_rows = session.execute(
                    select(MemberVote, _Person.name, _Person.normalized_name)
                    .join(_Person, MemberVote.member_id == _Person.id)
                    .where(MemberVote.agenda_item_vote_id == aiv.id)
                ).all()
                for mv, pname, pnorm in mv_rows:
                    member_votes.append({"name": pname, "vote": mv.vote,
                                         "slug": pnorm.replace(" ", "-") if pnorm else ""})
            else:
                sv_rows = session.execute(
                    select(SupervisorVote, _Person.name, _Person.normalized_name)
                    .join(_Person, SupervisorVote.supervisor_id == _Person.id)
                    .where(SupervisorVote.agenda_item_vote_id == aiv.id)
                ).all()
                for sv, pname, pnorm in sv_rows:
                    member_votes.append({"name": pname, "vote": sv.vote,
                                         "slug": pnorm.replace(" ", "-") if pnorm else ""})

            votes_by_item[key] = {
                "motion_result": aiv.motion_result,
                "vote_text": (aiv.vote_text or "")[:500],
                "is_split_vote": aiv.is_split_vote,
                "member_votes": member_votes,
            }

    session.close()

    return render_template(
        "c_number.html",
        c_number_base=c_number_base,
        items=items,
        docs_by_item=docs_by_item,
        votes_by_item=votes_by_item,
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
        .outerjoin(Meeting, Meeting.id == CaseEvent.meeting_db_id)
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


