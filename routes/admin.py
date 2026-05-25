"""Admin routes: dashboard, article CRUD, tag management, AI suggestions."""
import re, json, time
from datetime import datetime, timezone
from typing import Optional

from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, jsonify, current_app)
from flask_login import login_required, current_user
from sqlalchemy import select, desc, func, or_, and_
from sqlalchemy.orm import joinedload

from db.core import get_session
from db.newsroom import (
    Article, ArticleSource, Tag, article_tags, AdminUser,
    sync_article_fts, search_agenda_items,
)
from db.models import AgendaItem, Meeting
from db import get_session as get_db_session

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:200]


# ── Dashboard / Editorial Queue ──

@admin_bp.route("/")
@login_required
def dashboard():
    session = get_session()
    drafts = session.execute(
        select(Article).where(Article.status == "draft")
        .order_by(desc(Article.created_at))
    ).scalars().all()
    published = session.execute(
        select(Article).where(Article.status == "published")
        .order_by(desc(Article.published_at))
    ).scalars().all()
    archived = session.execute(
        select(Article).where(Article.status == "archived")
        .order_by(desc(Article.archived_at))
    ).scalars().all()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()
    return render_template(
        "admin/dashboard.html",
        drafts=drafts, published=published, archived=archived,
        tags=tags, now=datetime.now(timezone.utc),
    )


# ── AI Story Suggestions ──

@admin_bp.route("/suggestions")
@login_required
def suggestions():
    """Scan agenda items for newsworthy topics and return AI-generated pitch suggestions."""
    session = get_db_session()

    # Define interesting topics with keyword patterns
    topics = [
        ("housing", ["housing", "apartment", "residential", "multi-family",
                      "affordable", "townhome", "subdivision", "dwelling unit"]),
        ("zoning", ["rezon", "zone change", "zoning", "general plan amendment",
                     "land use", "zoning ordinance", "zoning code"]),
        ("data-centers", ["data center", "server farm", "hyperscale",
                          "data centre", "computing facility"]),
        ("enforcement", ["enforcement", "citation", "violation", "penalty",
                         "fine", "compliance", "cease", "desist", "revocation"]),
        ("health", ["health", "hospital", "clinic", "medical", "disease",
                    "pandemic", "vaccine", "opioid"]),
        ("environment", ["environmental", "sustainability", "solar", "renewable",
                         "emission", "climate", "conservation", "contamination"]),
        ("development", ["development", "construction", "building", "commercial",
                         "mixed-use", "retail", "office park", "industrial"]),
        ("transportation", ["transportation", "road", "highway", "transit",
                            "light rail", "bike lane", "pedestrian", "traffic"]),
        ("economy", ["economic development", "incentive", "tax", "job creation",
                     "workforce", "business park", "enterprise zone"]),
        ("water", ["water", "drought", "groundwater", "reclaimed", "wastewater",
                   "water rights", "aquifer", "conservation"]),
    ]

    suggestions = []
    seen_combos = set()

    for topic_slug, keywords in topics:
        like_clauses = [AgendaItem.agenda_item_title.ilike(f"%{kw}%")
                        for kw in keywords]
        like_clauses += [AgendaItem.agenda_item_text.ilike(f"%{kw}%")
                         for kw in keywords]

        items = session.execute(
            select(AgendaItem, Meeting.meeting_date, Meeting.body)
            .join(Meeting, and_(
                Meeting.meeting_id == AgendaItem.meeting_id,
                Meeting.body == AgendaItem.body,
            ))
            .where(or_(*like_clauses))
            .where(Meeting.sync_status == "complete")
            .order_by(desc(Meeting.meeting_date))
            .limit(5)
        ).all()

        for (item, meeting_date, body), _ in [(r, 0) for r in items]:
            title = (item.agenda_item_title or "")[:120]
            text = (item.agenda_item_text or "")[:300]
            key = (item.meeting_id, item.agenda_item_number)
            if key in seen_combos:
                continue
            seen_combos.add(key)

            # Gather matching keywords for the pitch
            matched = [kw for kw in keywords
                       if kw in title.lower() or kw in text.lower()]

            suggestions.append({
                "topic": topic_slug,
                "agenda_item_id": item.id,
                "body": body,
                "meeting_id": item.meeting_id,
                "meeting_date": meeting_date,
                "agenda_item_number": item.agenda_item_number,
                "title": title,
                "text": text,
                "matched_keywords": matched[:5],
                "source_url": item.source_url,
            })

    session.close()

    # Group by topic for display
    grouped = {}
    for s in suggestions:
        grouped.setdefault(s["topic"], []).append(s)

    tags = get_session().execute(select(Tag).order_by(Tag.name)).scalars().all()
    return render_template(
        "admin/suggestions.html",
        grouped=grouped, tags=tags,
    )


# ── Article CRUD ──

@admin_bp.route("/articles/new", methods=["GET", "POST"])
@login_required
def article_new():
    """Create a new article, optionally pre-populated from a suggestion."""
    session = get_session()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("admin.article_new"))

        article = Article(
            title=title,
            slug=_slugify(title),
            summary=request.form.get("summary", "").strip(),
            body=request.form.get("body", "").strip(),
            status=request.form.get("status", "draft"),
            author_id=current_user.id,
        )

        # Set timestamps based on status
        now = datetime.now(timezone.utc)
        if article.status == "published":
            article.published_at = now
        if article.status == "archived":
            article.archived_at = now

        session.add(article)
        session.flush()

        # Attach tags
        tag_ids = request.form.getlist("tags")
        for tid in tag_ids:
            tag = session.get(Tag, int(tid))
            if tag:
                article.tags.append(tag)

        # Attach sources
        source_bodies = request.form.getlist("source_body[]")
        source_meetings = request.form.getlist("source_meeting_id[]")
        source_items = request.form.getlist("source_item_number[]")
        source_urls = request.form.getlist("source_url[]")
        for i in range(len(source_bodies)):
            if source_bodies[i] and source_meetings[i]:
                src = ArticleSource(
                    article_id=article.id,
                    body=source_bodies[i],
                    meeting_id=source_meetings[i],
                    agenda_item_number=source_items[i] if i < len(source_items) else "",
                    source_url=source_urls[i] if i < len(source_urls) else "",
                )
                session.add(src)

        session.commit()
        sync_article_fts(article.id)
        session.close()
        flash("Article created.", "success")
        return redirect(url_for("admin.dashboard"))

    # GET — pre-fill from suggestion query params
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    preselected_tags = []
    if request.args.get("topic"):
        tag = session.execute(
            select(Tag).where(Tag.slug == request.args.get("topic"))
        ).scalar_one_or_none()
        if tag:
            preselected_tags.append(str(tag.id))

    session.close()
    return render_template(
        "admin/article_form.html",
        article=None, tags=tags, preselected_tags=preselected_tags,
        source_body=request.args.get("body", ""),
        source_meeting=request.args.get("meeting_id", ""),
        source_item=request.args.get("agenda_item_number", ""),
        source_url=request.args.get("source_url", ""),
    )


@admin_bp.route("/articles/<int:article_id>/edit", methods=["GET", "POST"])
@login_required
def article_edit(article_id):
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        flash("Article not found.", "error")
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        article.title = request.form.get("title", article.title).strip()
        article.slug = _slugify(article.title)
        article.summary = request.form.get("summary", "").strip()
        article.body = request.form.get("body", "").strip()
        article.status = request.form.get("status", article.status)
        article.updated_at = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        if article.status == "published" and not article.published_at:
            article.published_at = now
        if article.status == "archived" and not article.archived_at:
            article.archived_at = now

        # Update tags
        tag_ids = [int(t) for t in request.form.getlist("tags") if t]
        article.tags = [
            session.get(Tag, tid) for tid in tag_ids
            if session.get(Tag, tid)
        ]

        # Update sources — replace all
        session.query(ArticleSource).filter_by(article_id=article.id).delete()
        source_bodies = request.form.getlist("source_body[]")
        source_meetings = request.form.getlist("source_meeting_id[]")
        source_items = request.form.getlist("source_item_number[]")
        source_urls = request.form.getlist("source_url[]")
        for i in range(len(source_bodies)):
            if source_bodies[i] and source_meetings[i]:
                session.add(ArticleSource(
                    article_id=article.id,
                    body=source_bodies[i],
                    meeting_id=source_meetings[i],
                    agenda_item_number=source_items[i] if i < len(source_items) else "",
                    source_url=source_urls[i] if i < len(source_urls) else "",
                ))

        session.commit()
        sync_article_fts(article.id)
        session.close()
        flash("Article updated.", "success")
        return redirect(url_for("admin.dashboard"))

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()
    return render_template(
        "admin/article_form.html",
        article=article, tags=tags,
        preselected_tags=[str(t.id) for t in article.tags],
    )


@admin_bp.route("/articles/<int:article_id>/delete", methods=["POST"])
@login_required
def article_delete(article_id):
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        session.delete(article)
        session.commit()
    session.close()
    flash("Article deleted.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/articles/<int:article_id>/promote", methods=["POST"])
@login_required
def article_promote(article_id):
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        article.status = "published"
        article.published_at = datetime.now(timezone.utc)
        session.commit()
        sync_article_fts(article.id)
    session.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/articles/<int:article_id>/archive", methods=["POST"])
@login_required
def article_archive(article_id):
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        article.status = "archived"
        article.archived_at = datetime.now(timezone.utc)
        session.commit()
    session.close()
    return redirect(url_for("admin.dashboard"))


# ── Tag Management ──

@admin_bp.route("/tags", methods=["GET", "POST"])
@login_required
def manage_tags():
    session = get_session()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                existing = session.execute(
                    select(Tag).where(Tag.name == name)
                ).scalar_one_or_none()
                if not existing:
                    tag = Tag(name=name, slug=_slugify(name),
                              description=request.form.get("description", ""))
                    session.add(tag)
                    session.commit()
                    flash(f"Tag '{name}' added.", "success")
        elif action == "delete":
            tag_id = request.form.get("tag_id")
            if tag_id:
                tag = session.get(Tag, int(tag_id))
                if tag:
                    session.delete(tag)
                    session.commit()
                    flash(f"Tag deleted.", "success")
        elif action == "rename":
            tag_id = request.form.get("tag_id")
            name = request.form.get("name", "").strip()
            if tag_id and name:
                tag = session.get(Tag, int(tag_id))
                if tag:
                    tag.name = name
                    tag.slug = _slugify(name)
                    session.commit()
                    flash(f"Tag renamed.", "success")

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    # Get article counts per tag
    from sqlalchemy import func
    from db.newsroom import article_tags
    counts = {}
    rows = session.execute(
        select(article_tags.c.tag_id, func.count(article_tags.c.article_id))
        .group_by(article_tags.c.tag_id)
    ).all()
    for tag_id, cnt in rows:
        counts[tag_id] = cnt
    session.close()
    return render_template("admin/tags.html", tags=tags, tag_counts=counts)
