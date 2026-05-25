"""Public article routes: front page, article detail, archive."""
import re
from datetime import datetime, timezone
from sqlalchemy import select, desc

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
    session.close()
    return render_template("front_page.html", articles=articles,
                           featured=featured, tags=tags)


@articles_bp.route("/articles/<slug>")
def article_detail(slug):
    session = get_session()
    article = session.execute(
        select(Article).where(Article.slug == slug)
    ).scalar_one_or_none()
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
