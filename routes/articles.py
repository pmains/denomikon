"""Public article routes: front page, article detail, archive."""
import re
from datetime import datetime, timezone, date as date_cls, timedelta
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import joinedload

from flask import Blueprint, render_template, request, abort
from db.core import get_session
from db.newsroom import Article, Tag, search_articles, search_agenda_items

articles_bp = Blueprint("articles", __name__)


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
        select(Article).where(Article.status == "published")
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
        "gilbert-tc": "Gilbert Town Council",
    }
    upcoming_display = []
    for m in upcoming:
        display = _body_names.get(m.body, m.body)
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
