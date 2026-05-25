"""Public bodies routes blueprint."""

import logging
from datetime import date
from typing import Optional

from flask import Blueprint, render_template, request
from sqlalchemy import select, func

from db import get_session, Jurisdiction, PublicBody, BodyMembership, Person, _enhance_member_for_template, get_public_bodies_by_jurisdiction

from routes import SYNC_STATUS_BADGES, _cache

log = logging.getLogger(__name__)

bodies_bp = Blueprint("bodies", __name__, url_prefix="")

# ---------------------------------------------------------------------------
# Public Bodies / Members — Routes
# ---------------------------------------------------------------------------

@bodies_bp.route("/bodies")
def bodies_index():
    """List all known public bodies grouped by jurisdiction."""
    session = get_session()
    jurisdictions = session.execute(
        select(Jurisdiction).order_by(Jurisdiction.name)
    ).scalars().all()

    filter_jurisdiction = request.args.get("jurisdiction", "").strip()

    # Slug patterns that identify primary bodies (Council / Board of Supervisors)
    _PRIMARY_SLUGS = {
        "board-of-supervisors",
        "phoenix-city-council", "tempe-city-council", "mesa-city-council",
        "chandler-city-council", "gilbert-town-council", "scottsdale-city-council",
    }

    result = []
    for j in jurisdictions:
        q = select(PublicBody).where(PublicBody.jurisdiction_id == j.id)
        if filter_jurisdiction and j.slug != filter_jurisdiction:
            continue
        bodies = session.execute(q).scalars().all()
        # Sort: primary body first, then alphabetical
        bodies = sorted(bodies, key=lambda b: (b.slug not in _PRIMARY_SLUGS, b.name))
        result.append((j, bodies))
    session.close()
    return render_template(
        "bodies_index.html",
        jurisdictions=result,
        all_jurisdictions=jurisdictions,
        filter_jurisdiction=filter_jurisdiction,
    )


@bodies_bp.route("/bodies/<slug>")
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


