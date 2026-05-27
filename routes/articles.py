"""Public article routes: front page, article detail, archive."""
import re
from datetime import datetime, timezone, date as date_cls, timedelta
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import joinedload

from flask import Blueprint, render_template, request, abort
from db.core import get_session
from db.newsroom import Article, Tag, search_articles, search_agenda_items

articles_bp = Blueprint("articles", __name__)


def _code_to_name(code: str) -> str:
    """Safely convert a body code to a human-readable name."""
    if not code:
        return code
    # Maricopa County boards
    _mc_names = {
        "bos": "Maricopa County Board of Supervisors",
        "pz": "Maricopa County Planning & Zoning",
        "adj": "Maricopa County Board of Adjustment",
        "health": "Maricopa County Board of Health",
        "tab": "Maricopa County Transportation Advisory Board",
        "ida": "Maricopa County Industrial Development Authority",
    }
    if code in _mc_names:
        return _mc_names[code]

    if code.startswith("mc-"):
        # Convert mc-audit → Audit Advisory, mc-mcso-corp → MCSO CORP, etc.
        rest = code[3:]  # strip "mc-"
        label = rest.replace("-", " ").upper()
        # Known MCACC board names
        _mcacc = {
            "audit": "Audit Advisory Committee",
            "benefit trust": "Benefit Board of Trustees",
            "community action": "Community Action Commission",
            "cdac": "Community Development Advisory Committee",
            "eed policy": "Early Education Division Policy Council",
            "flood advisory": "Flood Control Advisory Board",
            "home": "HOME Consortium",
            "mclepc": "Local Emergency Planning Committee",
            "mcao psprs": "MCAO PSPRS Local Board",
            "mcso corp": "MCSO CORP Local Board",
            "mcso psprs": "MCSO PSPRS Local Board",
            "merit": "Merit Systems Commission",
            "psfc": "Public Safety Funding Committee",
            "risk trust": "Self-Insured Risk Trust Fund",
            "smart savings": "Smart Savings Committee",
            "stadium": "Stadium District Board",
            "trp": "Travel Reduction Program",
            "air pollution": "Air Pollution Hearing Board",
            "bcab": "Building Code Advisory Board",
            "flood stakeholder": "Flood Control Stakeholder Group",
        }
        return _mcacc.get(rest.replace("-", " "), f"Maricopa County {label}")

    parts = code.split("-")
    if len(parts) >= 2:
        city = parts[0].title()
        suffix = parts[-1].upper()
        if suffix == "CC":
            return f"{city} City Council"
        if suffix == "PZ":
            return f"{city} Planning & Zoning"
        if suffix == "DRC" or suffix == "DRB":
            return f"{city} Development Review Commission"
        if suffix == "BOA":
            return f"{city} Board of Adjustment"
        if suffix == "HPC":
            return f"{city} Historic Preservation Commission"
        if suffix == "TC":
            return f"{city} Town Council"
        return f"{city} {suffix}"
    return code.title()


@articles_bp.route("/")
def front_page():
    """Main front page — published news feed."""
    session = get_session()
    featured = session.execute(
        select(Article).where(Article.status == "published", Article.is_featured == True)
        .order_by(desc(Article.published_at))
        .limit(3)
    ).scalars().all()

    articles = session.execute(
        select(Article).where(Article.status == "published", Article.is_featured == False)
        .order_by(desc(Article.published_at))
        .limit(20)
    ).scalars().all()

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()

    # Upcoming meetings this week
    from db.models import Meeting
    today = date_cls.today()
    end_date = today + timedelta(days=7)
    today_str = today.isoformat()
    end_str = end_date.isoformat()

    upcoming = session.execute(
        select(Meeting)
        .where(
            and_(
                Meeting.meeting_date >= today_str,
                Meeting.meeting_date <= end_str,
                Meeting.sync_status.in_(["complete", "pending"]),
            )
        )
        .order_by(Meeting.meeting_date, Meeting.body)
        .limit(15)
    ).scalars().all()

    # Body code to human-readable name lookup
    _body_names = {
        # Maricopa County
        "bos": "Maricopa County Board of Supervisors",
        "mc-audit": "Maricopa County Audit Advisory Committee",
        "mc-benefit-trust": "Maricopa County Benefit Board of Trustees",
        "mc-community-action": "Maricopa County Community Action Commission",
        "mc-cdac": "Maricopa County Community Development Advisory Committee",
        "mc-eed-policy": "Maricopa County Early Education Division Policy Council",
        "mc-flood-advisory": "Maricopa County Flood Control Advisory Board",
        "mc-home": "Maricopa County HOME Consortium",
        "mc-mclepc": "Maricopa County Local Emergency Planning Committee",
        "mc-mcao-psprs": "Maricopa County MCAO PSPRS Local Board",
        "mc-mcso-corp": "Maricopa County MCSO CORP Local Board",
        "mc-mcso-psprs": "Maricopa County MCSO PSPRS Local Board",
        "mc-merit": "Maricopa County Merit Systems Commission",
        "mc-psfc": "Maricopa County Public Safety Funding Committee",
        "mc-risk-trust": "Maricopa County Self-Insured Risk Trust Fund Board of Trustees",
        "mc-smart-savings": "Maricopa County Smart Savings Committee",
        "mc-stadium": "Maricopa County Stadium District Board",
        "mc-trp": "Maricopa County Travel Reduction Program",
        "mc-air-pollution": "Maricopa County Air Pollution Hearing Board",
        "mc-bcab": "Maricopa County Building Code Advisory Board",
        "mc-flood-stakeholder": "Maricopa County Flood Control District Stakeholder Group",
        "pz": "Maricopa County Planning & Zoning",
        "adj": "Maricopa County Board of Adjustment",
        "drain": "Maricopa County Drainage Review",
        "health": "Maricopa County Board of Health",
        "tab": "Maricopa County Transportation Board",
        "ida": "Maricopa County IDA",
        # Tempe
        "tempe-cc": "Tempe City Council",
        "tempe-drc": "Tempe Development Review Commission",
        "tempe-pz": "Tempe Planning & Zoning Commission",
        # Mesa
        "mesa-cc": "Mesa City Council",
        "mesa-pz": "Mesa Planning & Zoning Board",
        "mesa-city-council": "Mesa City Council",
        "mesa-design-review-board": "Mesa Design Review Board",
        "mesa-board-of-adjustment": "Mesa Board of Adjustment",
        "mesa-historic-preservation-board": "Mesa Historic Preservation Board",
        # Chandler (from BODY_MAP)
        "chandler-cc": "Chandler City Council",
        "chandler-pz": "Chandler Planning & Zoning Commission",
        "chandler-drc": "Chandler Development Review Commission",
        "chandler-boa": "Chandler Board of Adjustment",
        "chandler-hpc": "Chandler Historic Preservation Commission",
        "chandler-ida": "Chandler Industrial Development Authority",
        "chandler-prb": "Chandler Parks and Recreation Board",
        "chandler-lb": "Chandler Library Board",
        "chandler-mf": "Chandler Museum Foundation",
        "chandler-cf": "Chandler Cultural Foundation",
        "chandler-arts": "Chandler Arts Commission",
        "chandler-tc": "Chandler Transportation Commission",
        "chandler-mvc": "Chandler Military and Veterans Affairs Commission",
        "chandler-hhsc": "Chandler Housing and Human Services Commission",
        "chandler-hrc": "Chandler Human Relations Commission",
        "chandler-dvc": "Chandler Domestic Violence Commission",
        "chandler-pha": "Chandler Public Housing Authority",
        "chandler-nac": "Chandler Neighborhood Advisory Committee",
        "chandler-yc": "Chandler Mayor's Youth Commission",
        "chandler-pdc": "Chandler Mayor's Committee for People with Disabilities",
        "chandler-eda": "Chandler Economic Development Advisory Board",
        "chandler-psprs-f": "Chandler PSPRS Board Fire",
        "chandler-psprs-p": "Chandler PSPRS Board Police",
        "chandler-hcc": "Chandler Housing and Community Services Corporation",
        "chandler-cpr": "Chandler Citizens' Panel Review",
        "chandler-hct": "Chandler Health Care Benefits Trust Board",
        "chandler-wct": "Chandler Workers' Compensation Trust Board",
        "chandler-air": "Chandler Airport Commission",
        # Scottsdale
        "scottsdale-cc": "Scottsdale City Council",
        # Gilbert
        "glendale-cc": "Glendale City Council",
        "peoria-cc": "Peoria City Council",
        # Buckeye
        "buckeye-cc": "Buckeye City Council",
        "buckeye-pz": "Buckeye Planning & Zoning",
        # Goodyear
        "goodyear-cc": "Goodyear City Council",
        "goodyear-pz": "Goodyear Planning & Zoning",
        "peoria-pz": "Peoria Planning & Zoning Commission",
        "surprise-cc": "Surprise City Council",
        "gilbert-tc": "Gilbert Town Council",
        # MCACC bodies (Maricopa County AgendaCenter)
        "mc-audit": "Audit Advisory Committee",
        "mc-benefit-trust": "Benefit Board of Trustees",
        "mc-community-action": "Community Action Commission",
        "mc-cdac": "Community Development Advisory Committee",
        "mc-eed-policy": "Early Education Division Policy Council",
        "mc-flood-advisory": "Flood Control Advisory Board",
        "mc-home": "HOME Consortium",
        "mc-mclepc": "Maricopa County Local Emergency Planning Committee",
        "mc-mcao-psprs": "MCAO PSPRS Local Board",
        "mc-mcso-corp": "MCSO Correctional Officer Retirement Plan Local Board",
        "mc-mcso-psprs": "MCSO PSPRS Local Board",
        "mc-merit": "Merit Systems Commission",
        "mc-psfc": "Public Safety Funding Committee",
        "mc-risk-trust": "Self-Insured Risk Trust Fund Board of Trustees",
        "mc-smart-savings": "Smart Savings Committee (Deferred Compensation)",
        "mc-stadium": "Stadium District Board",
        "mc-trp": "Travel Reduction Program",
        "mc-air-pollution": "Air Pollution Hearing Board",
        "mc-bcab": "Building Code Advisory Board",
        "mc-flood-stakeholder": "Flood Control District Stakeholder Group",
    }
    upcoming_display = []
    seen_dedup: set[tuple[str, str, str]] = set()
    for m in upcoming:
        # Deduplicate by (date, body, meeting_type) — occasionally two
        # meeting_ids exist for the same meeting (rescheduled entries).
        key = (m.meeting_date or "", m.body, m.meeting_type or "")
        if key in seen_dedup:
            continue
        seen_dedup.add(key)
        display = _body_names.get(m.body) if _body_names.get(m.body) else _code_to_name(m.body)
        upcoming_display.append({
            "body": m.body,
            "display_name": display,
            "meeting_id": m.meeting_id,
            "meeting_date": m.meeting_date,
            "meeting_type": m.meeting_type,
        })

    session.close()
    return render_template("front_page.html", articles=articles,
                           featured=featured, tags=tags,
                           upcoming_meetings=upcoming_display)


@articles_bp.route("/articles/<slug>")
def article_detail(slug):
    session = get_session()
    article = session.execute(
        select(Article)
        .options(joinedload(Article.author), joinedload(Article.tags))
        .where(Article.slug == slug)
    ).unique().scalar_one_or_none()
    if not article or article.status not in ("published", "archived"):
        session.close()
        abort(404)
    session.close()
    return render_template("article.html", article=article)


@articles_bp.route("/articles/archive")
def archive():
    session = get_session()
    articles = session.execute(
        select(Article).where(Article.status == "archived")
        .order_by(desc(Article.archived_at))
    ).scalars().all()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()
    return render_template("archive.html", articles=articles, tags=tags)


@articles_bp.route("/articles/tag/<tag_slug>")
def by_tag(tag_slug):
    session = get_session()
    tag = session.execute(
        select(Tag).where(Tag.slug == tag_slug)
    ).scalar_one_or_none()
    if not tag:
        session.close()
        abort(404)
    articles = session.execute(
        select(Article).where(
            Article.tags.any(Tag.id == tag.id),
            Article.status.in_(["published", "archived"]),
        ).order_by(desc(Article.published_at))
    ).scalars().all()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()
    return render_template("by_tag.html", tag=tag, articles=articles, tags=tags)


@articles_bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    scope = request.args.get("scope", "all")  # all, articles, agendas
    articles = []
    agenda_items = []
    tags = []

    session = get_session()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()

    if q:
        if scope in ("all", "articles"):
            articles = search_articles(q)
        if scope in ("all", "agendas"):
            agenda_items = search_agenda_items(q)

    session.close()
    return render_template("search.html", q=q, scope=scope,
                           articles=articles, agenda_items=agenda_items, tags=tags)
