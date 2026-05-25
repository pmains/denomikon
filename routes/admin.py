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


# ── Narrative angle templates for each topic ──
_ANGLE_TEMPLATES = {
    "housing": (
        "Housing supply remains a flashpoint across the Valley. This agenda item could signal "
        "new units coming online — or a fight over density, affordability, and neighborhood character."
    ),
    "zoning": (
        "Zoning changes are where land-use battles are won and lost. This item involves "
        "rewriting the rules for a specific piece of property — who benefits and who pushes back?"
    ),
    "data-centers": (
        "Data centers are reshaping Maricopa County's industrial landscape, bringing "
        "massive investment but also concerns about water use, power demand, and job quality."
    ),
    "enforcement": (
        "Code enforcement actions reveal where cities are drawing lines — and who gets "
        "cited. This item suggests regulatory friction worth watching."
    ),
    "health": (
        "Public health decisions at the county level have downstream effects on every "
        "resident. This item touches on health policy, access, or emerging risks."
    ),
    "environment": (
        "Environmental policy in the desert is never neutral — water, solar, conservation, "
        "and contamination battles all have high stakes. This item is worth a closer look."
    ),
    "development": (
        "Development proposals tell you where a city is headed. This item involves "
        "new construction or commercial plans that could change the character of an area."
    ),
    "transportation": (
        "Transportation decisions shape commute times, safety, and economic access for "
        "years. This agenda item has real implications for how people move through the county."
    ),
    "economy": (
        "Economic development incentives are essentially bets with taxpayer money. This "
        "item involves dollars, jobs, and the question of whether the promised returns materialize."
    ),
    "water": (
        "In the arid Southwest, water is the underlying story beneath almost every "
        "development decision. This item engages water rights, conservation, or infrastructure."
    ),
}


def _generate_pitch(suggestion: dict) -> dict:
    """Generate a narrative pitch, score by impact."""
    import urllib.parse
    import re as _re

    topic = suggestion["topic"]
    title = suggestion["title"] or ""
    body = suggestion["body"] or ""
    text = suggestion["text"] or ""
    matched = suggestion["matched_keywords"] or []
    meeting_date = suggestion["meeting_date"] or ""

    # ── Impact scoring ──
    score = 0
    text_lower = (title + " " + text).lower()
    dollar_matches = _re.findall(r'\$[\d,]+', text_lower)

    for d in dollar_matches:
        val = _re.sub(r'[\$,]', '', d)
        try:
            n = int(val)
            if n >= 10000000: score += 50
            elif n >= 1000000: score += 20
            elif n >= 100000: score += 10
            elif n >= 10000: score += 5
        except ValueError: pass

    signal_scores = {"bonds-finance": 40, "policy-planning": 35, "infrastructure": 25,
                     "homelessness": 30, "development-pipeline": 15, "annexation": 20,
                     "enforcement-signals": 10}
    score += signal_scores.get(topic, 0)

    if body == "bos": score += 10

    bundle_kws = ["homeless", "shelter", "amendment", "iga with"]
    for kw in bundle_kws:
        if text_lower.count(kw) > 3:
            score += 15
            break

    suggestion["score"] = score

    # ── Context ──
    angle = _ANGLE_TEMPLATES.get(topic, "This agenda item could have significant local impact.")

    jurisdiction_map = {"bos": "Maricopa County", "pz": "Maricopa County PZ",
        "adj": "Maricopa County ADJ", "mesa-cc": "Mesa", "mesa-pz": "Mesa PZ",
        "chandler-cc": "Chandler", "chandler-pz": "Chandler PZ",
        "tempe-cc": "Tempe", "scottsdale-cc": "Scottsdale", "gilbert-tc": "Gilbert"}
    location = ""
    for prefix, name in jurisdiction_map.items():
        if body.startswith(prefix): location = name; break
    if not location: location = body

    search_terms = f"{location} {topic.replace('-', ' ')} {matched[0] if matched else ''}"
    ddg_url = f"https://duckduckgo.com/?q={urllib.parse.quote(search_terms)}&ia=news"
    azcentral_url = f"https://www.azcentral.com/search/?q={urllib.parse.quote(search_terms)}"
    newslookup_url = f"https://newslookup.com/results?q={urllib.parse.quote(search_terms)}"

    headline = f"{topic.replace('-', ' ').title()}: {matched[0].title() if matched else ''} at {location}"

    # ── Narrative pitch HTML ──
    parts = [f'<span class="badge bg-{"danger" if score >= 30 else "warning" if score >= 15 else "secondary"}">Impact {score}</span>']
    for kw in matched[:3]:
        parts.append(f'<span class="badge bg-info text-dark me-1">{kw}</span>')
    parts.append('<br>')
    parts.append(f'<strong>Why it matters:</strong> {angle}')
    if meeting_date: parts.append(f'<strong>When:</strong> {meeting_date}')
    parts.append(f'<strong>Where:</strong> {location}')
    if title: parts.append(f'<strong>Item:</strong> &ldquo;{title}&rdquo;')
    parts.append(f'<strong>Suggested headline:</strong> {headline}')
    parts.append(f'<a href="{ddg_url}" target="_blank">DuckDuckGo News</a> | <a href="{newslookup_url}" target="_blank">NewsLookup</a> | <a href="{azcentral_url}" target="_blank">AZ Central</a>')

    suggestion["pitch"] = "<br>".join(parts)
    suggestion["headline"] = headline
    suggestion["location"] = location
    suggestion["angle"] = angle
    suggestion["ddg_url"] = ddg_url
    suggestion["azcentral_url"] = azcentral_url
    suggestion["newslookup_url"] = newslookup_url

    editorial_notes = []
    if score >= 30:
        editorial_notes.append("High-impact item — consider front-page feature.")
    if topic in ("bonds-finance", "policy-planning"):
        editorial_notes.append("Policy/finance story — may benefit from context about past actions.")
    if dollar_matches:
        editorial_notes.append(f"Dollar figures: {', '.join(dollar_matches[:3])}. Verify against meeting docs.")
    suggestion["editorial_notes"] = editorial_notes

    return suggestion


@admin_bp.route("/suggestions")
@login_required
def suggestions():
    """Scan agenda items for newsworthy topics and return AI-generated pitch suggestions."""
    session = get_db_session()

    jurisdiction_filter = request.args.get("jurisdiction", "")

    # Exclude dismissed suggestions
    from db.newsroom import DismissedSuggestion
    dismissed = set()
    dismissed_rows = session.execute(
        select(DismissedSuggestion)
    ).scalars().all()
    for ds in dismissed_rows:
        dismissed.add((ds.body, ds.meeting_id, ds.agenda_item_number))

    # Build jurisdiction map for filtering
    _JUR_BODY_PREFIXES = {
        "maricopa-county": "bos",
        "tempe": "tempe",
        "mesa": "mesa",
        "chandler": "chandler",
        "gilbert": "gilbert",
        "scottsdale": "scottsdale",
    }

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

    _JUR_PREFIX = _JUR_BODY_PREFIXES.get(jurisdiction_filter, "")

    for topic_slug, keywords in topics:
        like_clauses = [AgendaItem.agenda_item_title.ilike(f"%{kw}%")
                        for kw in keywords]
        like_clauses += [AgendaItem.agenda_item_text.ilike(f"%{kw}%")
                         for kw in keywords]

        # Use a date-sortable expression: convert all date formats to ISO
        # Chandler uses "September 17, 2025" which sorts above ISO dates lexicographically.
        from sqlalchemy import case as sa_case, literal_column as _L
        _date_expr = sa_case(
            (Meeting.meeting_date.like("____-__-__"), Meeting.meeting_date),
            (Meeting.meeting_date.like("%/%"),
             func.substr(Meeting.meeting_date, -4) + "-" +
             func.substr(Meeting.meeting_date, 1, 2) + "-" +
             func.substr(Meeting.meeting_date, 4, 2)),
            else_=func.substr(Meeting.meeting_date, -4) + 
                   "-" + func.substr(Meeting.meeting_date, 1, 2) +
                   "-" + func.substr(Meeting.meeting_date, 4, 2),
        )

        # Build filters: keywords OR'd + optional jurisdiction AND
        _filters = [or_(*like_clauses)]
        if _JUR_PREFIX:
            _filters.append(Meeting.body.like(f"{_JUR_PREFIX}%"))

        items = session.execute(
            select(AgendaItem, Meeting.meeting_date, Meeting.body)
            .join(Meeting, and_(
                Meeting.meeting_id == AgendaItem.meeting_id,
                Meeting.body == AgendaItem.body,
            ))
            .where(and_(*_filters))
            .where(Meeting.sync_status == "complete")
            .order_by(desc(_date_expr))
            .limit(30)
        ).all()

        for (item, meeting_date, body), _ in [(r, 0) for r in items]:

            title = (item.agenda_item_title or "")[:120]
            text = (item.agenda_item_text or "")[:300]
            key = (item.meeting_id, item.agenda_item_number)
            if key in seen_combos:
                continue
            if key in dismissed:
                continue
            seen_combos.add(key)

            # Gather matching keywords for the pitch
            matched = [kw for kw in keywords
                       if kw in title.lower() or kw in text.lower()]

            suggestion = {
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
            }
            suggestions.append(_generate_pitch(suggestion))

    # ── High-significance signal search ──
    # Prioritize items the keyword approach misses: bonds, comp plans, large-dollar,
    # irrigation districts, homelessness service bundles, and policy frameworks.
    _SIGNAL_PATTERNS = {
        "bonds-finance": ["bond", "revenue", "financing", "appropriation",
                          "not to exceed $", "allocation", "budget",
                          "multifamily housing revenue"],
        "policy-planning": ["comprehensive plan", "framework 2040", "framework",
                            "general plan", "annual action plan",
                            "master plan", "area plan"],
        "infrastructure": ["irrigation water delivery", "water delivery district",
                           "road abandonment", "road file",
                           "hearing for the proposed", "setting of hearing"],
        "homelessness": ["homeless", "shelter", "rapid rehousing",
                         "emergency shelter", "homelessness outreach"],
        "development-pipeline": ["preliminary development plan", "preliminary plat",
                                 "subdivision", "planned area development",
                                 "development plan review"],
        "annexation": ["annex", "pre-annexation", "county island"],
        "enforcement-signals": ["litigation", "violation", "code", "citation",
                                "penalty", "cease", "desist"],
    }

    for signal_slug, signal_kws in _SIGNAL_PATTERNS.items():
        signal_likes = [AgendaItem.agenda_item_title.ilike(f"%{kw}%") for kw in signal_kws]
        signal_likes += [AgendaItem.agenda_item_text.ilike(f"%{kw}%") for kw in signal_kws]

        _signal_filter = [or_(*signal_likes)]
        if _JUR_PREFIX:
            _signal_filter.append(Meeting.body.like(f"{_JUR_PREFIX}%"))

        signal_items = session.execute(
            select(AgendaItem, Meeting.meeting_date, Meeting.body)
            .join(Meeting, and_(
                Meeting.meeting_id == AgendaItem.meeting_id,
                Meeting.body == AgendaItem.body,
            ))
            .where(and_(*_signal_filter))
            .where(Meeting.sync_status == "complete")
            .order_by(desc(_date_expr))
            .limit(20)
        ).all()

        session_count = {}
        for (item, meeting_date, body), _ in [(r, 0) for r in signal_items]:
            key = (item.meeting_id, item.agenda_item_number)
            if key in seen_combos:
                continue
            if key in dismissed:
                continue
            seen_combos.add(key)

            title = (item.agenda_item_title or "")[:120]
            # Give signal items a higher weight by matching their keywords
            matched = [kw for kw in signal_kws if kw in title.lower() or
                       (item.agenda_item_text or "").lower().count(kw) > 0]

            sug = {
                "topic": signal_slug,
                "agenda_item_id": item.id,
                "body": body,
                "meeting_id": item.meeting_id,
                "meeting_date": meeting_date,
                "agenda_item_number": item.agenda_item_number,
                "title": title,
                "text": (item.agenda_item_text or "")[:300],
                "matched_keywords": matched[:5],
                "source_url": item.source_url,
            }
            suggestions.append(_generate_pitch(sug))

    # ── Explicit high-value items that signal patterns may miss ──
    _HIGH_VALUE_SEARCHES = [
        ("bonds-finance", ["bond", "multifamily housing revenue", "not to exceed"]),
        ("policy-planning", ["comprehensive plan", "framework 2040", "annual action plan"]),
        ("infrastructure", ["irrigation water delivery", "hearing for the proposed",
                           "road file", "palomino acres"]),
    ]
    for hv_topic, hv_kws in _HIGH_VALUE_SEARCHES:
        hv_likes = [AgendaItem.agenda_item_title.ilike(f"%{kw}%") for kw in hv_kws]
        hv_filter = [or_(*hv_likes)]
        if _JUR_PREFIX:
            hv_filter.append(Meeting.body.like(f"{_JUR_PREFIX}%"))

        try:
            hv_items = session.execute(
                select(AgendaItem, Meeting.meeting_date, Meeting.body)
                .join(Meeting, and_(
                    Meeting.meeting_id == AgendaItem.meeting_id,
                    Meeting.body == AgendaItem.body,
                ))
                .where(and_(*hv_filter))
                .where(Meeting.sync_status == "complete")
                .order_by(desc(Meeting.meeting_date))
                .limit(10)
            ).all()
            for (item, meeting_date, body), _ in [(r, 0) for r in hv_items]:
                key = (item.meeting_id, item.agenda_item_number)
                if key in seen_combos:
                    continue
                seen_combos.add(key)
                title = (item.agenda_item_title or "")[:120]
                matched = [kw for kw in hv_kws if kw in title.lower() or
                           (item.agenda_item_text or "").lower().count(kw) > 0]
                sug = {
                    "topic": hv_topic, "agenda_item_id": item.id,
                    "body": body, "meeting_id": item.meeting_id,
                    "meeting_date": meeting_date,
                    "agenda_item_number": item.agenda_item_number,
                    "title": title, "text": (item.agenda_item_text or "")[:300],
                    "matched_keywords": matched[:5],
                    "source_url": item.source_url,
                }
                suggestions.append(_generate_pitch(sug))
        except Exception as e:
            log.warning("High-value search failed for %s: %s", hv_topic, e)

    session.close()

    # Sort: signal topics first, then keyword-matched, by date
    def _sort_key(s):
        priority = 0 if s["topic"] in _SIGNAL_PATTERNS else 1
        return (priority, s.get("meeting_date", "") or "")
    suggestions.sort(key=_sort_key, reverse=True)

    # Group by topic for display (no jurisdiction cap — let a thousand flowers bloom)
    grouped = {}
    for s in suggestions:
        grouped.setdefault(s["topic"], []).append(s)

    # Remove jurisdiction fairness cap — let a thousand flowers bloom
    tags = get_session().execute(select(Tag).order_by(Tag.name)).scalars().all()
    return render_template(
        "admin/suggestions.html", grouped=grouped, tags=tags,
        filter_jurisdiction=jurisdiction_filter,
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


# ── Suggestion Dismissal ──

@admin_bp.route("/suggestions/dismiss", methods=["POST"])
@login_required
def dismiss_suggestion():
    """Dismiss a story suggestion so it doesn't reappear."""
    from db.newsroom import DismissedSuggestion
    session = get_session()
    ds = DismissedSuggestion(
        body=request.form.get("body", ""),
        meeting_id=request.form.get("meeting_id", ""),
        agenda_item_number=request.form.get("agenda_item_number", ""),
        reason=request.form.get("reason", "dismissed"),
        dismissed_by=current_user.id,
    )
    session.add(ds)
    session.commit()
    session.close()
    flash("Suggestion dismissed.", "success")
    return redirect(url_for("admin.suggestions"))


@admin_bp.route("/suggestions/draft", methods=["POST"])
@login_required
def draft_from_suggestion():
    """Generate a full draft article from a suggestion and redirect to edit."""
    session = get_session()
    body_code = request.form.get("body", "")
    meeting_id = request.form.get("meeting_id", "")
    item_number = request.form.get("agenda_item_number", "")
    topic = request.form.get("topic", "")
    source_url = request.form.get("source_url", "")

    # Look up the agenda item for full text
    from db.models import AgendaItem, Meeting
    from sqlalchemy import and_
    item = session.execute(
        select(AgendaItem)
        .join(Meeting, and_(Meeting.meeting_id == AgendaItem.meeting_id,
                           Meeting.body == AgendaItem.body))
        .where(AgendaItem.body == body_code)
        .where(AgendaItem.meeting_id == meeting_id)
        .where(AgendaItem.agenda_item_number == item_number)
        .limit(1)
    ).scalar_one_or_none()

    title = (item.agenda_item_title or "Untitled")[:120] if item else "Untitled"
    item_text = (item.agenda_item_text or "")[:2000] if item else ""

    # Generate draft body from the agenda item text
    from datetime import date
    draft_body = f"**Background**\n\nThe {body_code} considered this item recently.\n\n**Details**\n\n{item_text}\n\n**What's Next**\n\nThis item was on the agenda. Check the meeting page for the outcome."
    draft_summary = title[:200]

    # Create the draft article
    article = Article(
        title=title,
        slug=_slugify(title),
        summary=draft_summary,
        body=draft_body,
        status="draft",
        author_id=current_user.id,
    )
    session.add(article)
    session.flush()

    # Attach source
    if source_url:
        src = ArticleSource(
            article_id=article.id,
            body=body_code,
            meeting_id=meeting_id,
            agenda_item_number=item_number,
            source_url=source_url,
        )
        session.add(src)

    session.commit()
    sync_article_fts(article.id)
    session.close()

    flash("Draft created from suggestion. Edit, add sources, and publish.", "success")
    return redirect(url_for("admin.article_edit", article_id=article.id))


@admin_bp.route("/suggestions/split", methods=["POST"])
@login_required
def split_suggestion():
    """Create a parent article placeholder for a multi-part story."""
    from db.newsroom import DismissedSuggestion
    session = get_session()
    parent = Article(
        title=request.form.get("title", "Multi-part story").strip(),
        slug=_slugify(request.form.get("title", "multi-part-story")),
        summary="A series of related stories on this topic.",
        status="draft",
        author_id=current_user.id,
    )
    session.add(parent)
    session.commit()
    # Mark the original suggestion as dismissed with a split reason
    ds = DismissedSuggestion(
        body=request.form.get("body", ""),
        meeting_id=request.form.get("meeting_id", ""),
        agenda_item_number=request.form.get("agenda_item_number", ""),
        reason="split",
        dismissed_by=current_user.id,
    )
    session.add(ds)
    session.commit()
    session.close()
    flash(f"Parent article created. Sub-articles can be added.", "success")
    return redirect(url_for("admin.article_edit", article_id=parent.id))


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
