"""Meetings and index routes blueprint."""

import logging
from datetime import date
from typing import Optional

from flask import Blueprint, render_template, request, redirect, jsonify
from sqlalchemy import select, func, or_, text as sa_text

from db import (
    get_session, Meeting, AgendaItem, SupportingDocument,
    AgendaItemVote, SupervisorVote, Supervisor, MeetingSupervisor,
    PZItemDetail, BodyMembership, Person, _enhance_member_for_template,
    Case, CaseEvent, Jurisdiction,
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

    # ── Consistent source badge color scheme ──
    # Blue (primary)      = City Councils, primary legislative bodies
    # Orange (secondary)  = P&Z, DRC, BOA, HPC — land use / development
    # Teal (success)      = Health, housing, community services
    # Yellow (warning)    = Transportation, infrastructure, flood control
    # Brick red (danger)  = Fiscal, pension, audit, enforcement
    # Medium blue (info)  = Culture, museums, parks, stadiums, misc boards
    _BODY_BADGE = {
        # Maricopa County
        "bos": "primary",
        "pz": "secondary", "adj": "secondary",
        "health": "success", "tab": "warning", "ida": "danger", "drain": "info",
        # Tempe
        "tempe-cc": "primary",
        "tempe-drc": "secondary", "tempe-boa": "secondary", "tempe-hpc": "secondary",
        "tempe-ha": "success", "tempe-rio": "warning",
        "tempe-rmt": "danger", "tempe-jrc": "info",
        # Mesa
        "mesa-cc": "primary", "mesa-city-council": "primary",
        "mesa-pz": "secondary", "mesa-drb": "secondary",
        "mesa-boa": "secondary", "mesa-hpb": "secondary",
        "mesa-cadence": "info", "mesa-eastmark1": "info", "mesa-eastmark2": "info",
        # Chandler
        "chandler-cc": "primary",
        "chandler-pz": "secondary", "chandler-drc": "secondary",
        "chandler-boa": "secondary", "chandler-hpc": "secondary",
        "chandler-ida": "danger",
        "chandler-prb": "info", "chandler-lb": "info",
        "chandler-mf": "info", "chandler-cf": "info", "chandler-arts": "info",
        "chandler-tc": "warning",
        "chandler-mvc": "info", "chandler-hhsc": "success", "chandler-hrc": "info",
        "chandler-dvc": "info", "chandler-pha": "success",
        "chandler-nac": "info", "chandler-yc": "info", "chandler-pdc": "info",
        "chandler-eda": "info", "chandler-psprs-f": "danger", "chandler-psprs-p": "danger",
        "chandler-hcc": "success", "chandler-cpr": "info",
        "chandler-hct": "danger", "chandler-wct": "danger", "chandler-air": "warning",
        # Scottsdale
        "scottsdale-cc": "primary",
        "scottsdale-pc": "secondary", "scottsdale-boa": "secondary",
        "scottsdale-drb": "secondary", "scottsdale-hpc": "secondary",
        "scottsdale-baba": "secondary",
        # Glendale
        "glendale-cc": "primary",
        # Gilbert
        "gilbert-tc": "primary",
        # Peoria
        "peoria-cc": "primary", "peoria-pz": "secondary",
        "peoria-boa": "secondary",
        # Surprise
        "surprise-cc": "primary",
        # MCACC boards
        "mc-audit": "danger", "mc-benefit-trust": "danger",
        "mc-community-action": "success", "mc-cdac": "info",
        "mc-eed-policy": "success", "mc-flood-advisory": "warning",
        "mc-home": "success", "mc-mclepc": "warning",
        "mc-mcao-psprs": "danger", "mc-mcso-corp": "danger",
        "mc-mcso-psprs": "danger", "mc-merit": "danger",
        "mc-psfc": "danger", "mc-risk-trust": "danger",
        "mc-smart-savings": "danger", "mc-stadium": "info",
        "mc-trp": "warning", "mc-air-pollution": "info",
        "mc-bcab": "secondary", "mc-flood-stakeholder": "warning",
    }

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
        is_mesa = body_val.startswith("mesa-")
        is_chandler = body_val.startswith("chandler-")
        is_gilbert = body_val.startswith("gilbert-")
        is_scottsdale = body_val.startswith("scottsdale-")
        is_peoria = body_val.startswith("peoria-")
        is_buckeye = body_val.startswith("buckeye-")
        is_goodyear = body_val.startswith("goodyear-")
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
        elif is_mesa:
            source_labels = {
                "mesa-cc": "City Council",
                "mesa-pz": "Planning & Zoning",
                "mesa-drb": "Design Review",
                "mesa-boa": "Board of Adj",
                "mesa-hpb": "Hist Preserv",
                "mesa-cadence": "Cadence CFD",
                "mesa-eastmark1": "Eastmark CFD 1",
                "mesa-eastmark2": "Eastmark CFD 2",
            }
            source = source_labels.get(body_val, "Mesa")
            source_badge = "warning"
        elif is_chandler:
            source_labels = {
                "chandler-cc": "City Council",
                "chandler-pz": "Planning & Zoning",
                "chandler-drc": "Dev Review",
                "chandler-boa": "Board of Adj",
                "chandler-hpc": "Hist Preserv",
                "chandler-ida": "Ind Dev Auth",
                "chandler-prb": "Parks & Rec",
                "chandler-lb": "Library Board",
                "chandler-mf": "Museum Fndtn",
                "chandler-cf": "Cultural Fndtn",
                "chandler-arts": "Arts Comm",
                "chandler-tc": "Transpo Comm",
                "chandler-mvc": "Mil & Vet",
                "chandler-hhsc": "Housing & HS",
                "chandler-hrc": "Human Rel",
                "chandler-dvc": "Dom Violence",
                "chandler-pha": "Pub Housing",
                "chandler-nac": "Neighbor Adv",
                "chandler-yc": "Youth Comm",
                "chandler-pdc": "Disabilities",
                "chandler-eda": "Econ Dev",
                "chandler-psprs-f": "PSPRS Fire",
                "chandler-psprs-p": "PSPRS Police",
                "chandler-hcc": "Housing Corp",
                "chandler-cpr": "Citizens Rev",
                "chandler-hct": "Health Trust",
                "chandler-wct": "Workers Comp",
                "chandler-air": "Airport Comm",
            }
            source = source_labels.get(body_val, "Chandler")
            source_badge = "success"
        elif is_gilbert:
            source = "Town Council" if body_val == "gilbert-tc" else "Gilbert"
            source_badge = "dark"
        elif body_val.startswith("glendale-"):
            glendale_labels = {
                "glendale-cc": "City Council",
                "glendale-pc": "Planning Comm",
                "glendale-boa": "Board of Adj",
            }
            source = glendale_labels.get(body_val, "Glendale")
            source_badge = "info"
        elif body_val.startswith("surprise-"):
            source = "City Council" if body_val == "surprise-cc" else "Surprise"
            source_badge = "dark"

        elif is_buckeye:
            buckeye_labels = {
                "buckeye-cc": "City Council",
                "buckeye-pz": "Planning elif is_peoria: Zoning",
                "buckeye-boa": "Board of Adj",
                "buckeye-prc": "Parks elif is_peoria: Rec",
                "buckeye-hpc": "Hist Preserv",
                "buckeye-library": "Library Board",
                "buckeye-psprs": "PSPRS",
            }
            source = buckeye_labels.get(body_val, "Buckeye")
            source_badge = "dark"
        elif is_goodyear:
            goodyear_labels = {
                "goodyear-cc": "City Council",
                "goodyear-pz": "Planning elif is_peoria: Zoning",
                "goodyear-acc": "Arts elif is_peoria: Culture",
                "goodyear-wac": "Water Advisory",
                "goodyear-yc": "Youth Comm",
                "goodyear-audit": "Audit",
                "goodyear-psprs-f": "Fire PSPRS",
                "goodyear-psprs-p": "Police PSPRS",
            }
            source = goodyear_labels.get(body_val, "Goodyear")
            source_badge = "success"
        elif is_peoria:
            peoria_labels = {
                "peoria-cc": "City Council",
                "peoria-pz": "Planning & Zoning",
                "peoria-boa": "Board of Adj",
                "peoria-sub": "Subcommittee",
            }
            source = peoria_labels.get(body_val, "Peoria")
            source_badge = "warning"

        elif body_val.startswith("scottsdale-"):
            scottsdale_labels = {
                "scottsdale-cc": "City Council",
                "scottsdale-pc": "Planning Comm",
                "scottsdale-boa": "Board of Adj",
                "scottsdale-drb": "Dev Review",
                "scottsdale-hpc": "Hist Preserv",
                "scottsdale-baba": "Bldg Appeals",
            }
            source = scottsdale_labels.get(body_val, "Scottsdale")
            source_badge = "info"
        elif body_val.startswith("mc-"):
            mc_labels = {
                "mc-audit": "Audit", "mc-benefit-trust": "Benefits",
                "mc-community-action": "Comm Action", "mc-cdac": "CDAC",
                "mc-eed-policy": "EED Policy", "mc-flood-advisory": "Flood Adv",
                "mc-home": "HOME", "mc-mclepc": "MCLEPC",
                "mc-mcao-psprs": "MCAO PSPRS", "mc-mcso-corp": "MCSO CORP",
                "mc-mcso-psprs": "MCSO PSPRS", "mc-merit": "Merit",
                "mc-psfc": "PSFC", "mc-risk-trust": "Risk Trust",
                "mc-smart-savings": "Savings", "mc-stadium": "Stadium",
                "mc-trp": "TRP", "mc-air-pollution": "Air Pollution",
                "mc-bcab": "BCAB", "mc-flood-stakeholder": "Flood Stake",
            }
            source = mc_labels.get(body_val, "Maricopa")
        else:
            source = "IDA" if is_ida else ("TAB" if is_tab else ("BOH" if is_health else ("DRB" if is_drain else ("ADJ" if is_adj else ("PZ" if is_pz else "BOS")))))
            source_badge = "light" if is_ida else ("warning" if is_tab else ("success" if is_health else ("info" if is_drain else ("dark" if is_adj else ("secondary" if is_pz else "primary")))))
        # Resolve jurisdiction name from meeting.jurisdiction_id
        jur_id = row.jurisdiction_id or 1
        if jur_id == 2:
            jur_name = "Tempe"
            jur_slug = "tempe"
        elif jur_id == 3:
            jur_name = "Chandler"
            jur_slug = "chandler"
        elif jur_id == 5:
            jur_name = "Mesa"
            jur_slug = "mesa"
        elif jur_id == 6:
            jur_name = "Gilbert"
            jur_slug = "gilbert"
        elif jur_id == 7:
            jur_name = "Scottsdale"
            jur_slug = "scottsdale"
        elif jur_id == 10:
            jur_name = "Peoria"
            jur_slug = "peoria"
        elif jur_id == 11:
            jur_name = "Glendale"
            jur_slug = "glendale"
        elif jur_id == 12:
            jur_name = "Surprise"
            jur_slug = "surprise"
        elif jur_id == 13:
            jur_name = "Buckeye"
            jur_slug = "buckeye"
        elif jur_id == 14:
            jur_name = "Goodyear"
            jur_slug = "goodyear"
        elif jur_id == 11:
            jur_name = "Glendale"
            jur_slug = "glendale"
        elif jur_id == 12:
            jur_name = "Surprise"
            jur_slug = "surprise"
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
            "source_badge": _BODY_BADGE.get(body_val, source_badge),
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


@meetings_bp.route("/meetings/<meeting_id>")
@meetings_bp.route("/meetings/<body>/<meeting_id>")
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

    # Resolve jurisdiction slug from meeting
    from db.models import Jurisdiction as _Jur
    _jur = session.execute(
        select(_Jur).where(_Jur.id == (meeting.jurisdiction_id or 1))
    ).scalar_one_or_none()
    jurisdiction_slug = _jur.slug if _jur else "maricopa-county"

    session.close()

    badge = SYNC_STATUS_BADGES.get((meeting.sync_status or "").lower(), "secondary")

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
            .join(Meeting, Meeting.meeting_id == _SD.meeting_id)
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


