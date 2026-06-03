"""Newsroom models: articles, tags, admin users, FTS search."""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (Column, Integer, String, Text, DateTime, Boolean,
                        ForeignKey, Table, select, func, or_, and_)
from sqlalchemy.orm import relationship

from db.core import get_engine, get_session
from db.models import Base, AgendaItem

log = logging.getLogger(__name__)

# ── Association tables ──

article_tags = Table(
    "article_tags", Base.metadata,
    Column("article_id", Integer, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

# ── Models ──

class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=False, default="")
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="editor")  # admin, editor
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))

    def get_id(self):
        return str(self.id)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(256), nullable=False)
    slug = Column(String(256), unique=True, nullable=False, index=True)
    summary = Column(Text, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="draft", index=True)
    # draft, published, archived
    author_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    author = relationship("AdminUser", backref="articles")
    featured_image = Column(String(512), nullable=False, default="")
    image_credit = Column(String(256), nullable=True, default=None)
    is_featured = Column(Boolean, nullable=False, default=False)
    priority = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    published_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)

    tags = relationship("Tag", secondary=article_tags, backref="articles",
                        lazy="selectin")
    sources = relationship("ArticleSource", backref="article",
                           lazy="selectin", cascade="all, delete-orphan")


class ArticleSource(Base):
    __tablename__ = "article_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    body = Column(String(16), nullable=False, default="")
    meeting_id = Column(String(32), nullable=False, default="")
    agenda_item_number = Column(String(32), nullable=False, default="")
    source_url = Column(String(512), nullable=False, default="")
    source_type = Column(String(32), nullable=False, default="agenda")
    item_title = Column(String(512), nullable=False, default="")


# ── Dismissed Suggestions ──

class Notification(Base):
    """In-app admin notification."""
    __tablename__ = "admin_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message = Column(Text, nullable=False)
    url = Column(String(512), nullable=False, default="")
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class MediaImage(Base):
    """Uploaded media item (image) for use in articles and pages."""
    __tablename__ = "media_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(256), nullable=False)
    original_name = Column(String(256), nullable=False, default="")
    alt_text = Column(String(512), nullable=False, default="")
    tags = Column(String(512), nullable=False, default="")
    file_size = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=0)
    height = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))

    @property
    def url(self) -> str:
        return f"/static/uploads/{self.filename}"

    def __repr__(self):
        return f"<MediaImage #{self.id}: {self.filename}>"


class DismissedSuggestion(Base):
    __tablename__ = "dismissed_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="")
    meeting_id = Column(String(32), nullable=False, default="")
    agenda_item_number = Column(String(32), nullable=False, default="")
    reason = Column(String(32), nullable=False, default="dismissed")
    dismissed_by = Column(Integer, ForeignKey("admin_users.id"), nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))


class SkeetDraft(Base):
    """Curated Bluesky post draft linked to an article.

    The editor reviews the auto-generated draft, tweaks the text,
    optionally replaces the link-card image, and approves or skips.
    A separate cron posts approved drafts.
    """
    __tablename__ = "skeet_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    article = relationship("Article", backref="skeet_drafts")
    draft_text = Column(String(300), nullable=False, default="")
    status = Column(String(16), nullable=False, default="draft", index=True)
    # draft, approved, posted, skipped
    image_path = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    posted_at = Column(DateTime(timezone=True), nullable=True)
    bluesky_post_uri = Column(String(256), nullable=False, default="")


# ── FTS Setup ──

FTS_TABLES = {}


def init_fts():
    """Create FTS5 virtual tables for full-text search if they don't exist."""
    engine = get_engine()
    conn = engine.raw_connection()

    # Articles FTS (standalone — no content-sync, tags are in association table)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
            title, summary, body, tags,
            tokenize='porter unicode61'
        )
    """)

    # Agenda items FTS (standalone)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS agenda_items_fts USING fts5(
            agenda_item_title, agenda_item_text,
            tokenize='porter unicode61'
        )
    """)

    conn.commit()
    conn.close()
    log.info("FTS tables initialized")


def rebuild_fts():
    """Rebuild FTS indexes from current data."""
    engine = get_engine()
    conn = engine.raw_connection()
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO agenda_items_fts(agenda_items_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


def sync_article_fts(article_id: int):
    """Update FTS index for a single article."""
    engine = get_engine()
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        return
    tags_str = " ".join(t.name for t in article.tags)
    conn = engine.raw_connection()
    conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (article_id,))
    conn.execute(
        "INSERT INTO articles_fts(rowid, title, summary, body, tags) VALUES (?, ?, ?, ?, ?)",
        (article_id, article.title, article.summary, article.body, tags_str),
    )
    conn.commit()
    conn.close()
    session.close()


def search_agenda_items(query: str, limit: int = 50, _fetch_extra: bool = True) -> tuple[list[dict], bool]:
    """Full-text search across agenda items.

    Returns (results, truncated) where truncated is True when more
    results exist beyond the limit.
    """
    engine = get_engine()
    conn = engine.raw_connection()
    fetch_limit = limit + 1 if _fetch_extra else limit
    try:
        rows = conn.execute(
            """SELECT f.rowid, a.body, a.meeting_id, a.agenda_item_number,
                      a.agenda_item_title, a.agenda_item_text,
                      a.source_url,
                      rank
               FROM agenda_items_fts f
               JOIN agenda_items a ON a.id = f.rowid
               WHERE agenda_items_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, fetch_limit),
        ).fetchall()
    except Exception:
        rows = []
    conn.close()

    truncated = len(rows) > limit
    rows = rows[:limit]

    results = []
    for r in rows:
        results.append({
            "id": r[0], "body": r[1], "meeting_id": r[2],
            "agenda_item_number": r[3], "title": r[4], "text": r[5][:300],
            "source_url": r[6], "rank": r[7],
        })
    return results, truncated


def search_articles(query: str, limit: int = 50, _fetch_extra: bool = True) -> tuple[list[dict], bool]:
    """Full-text search across published articles.

    Returns (results, truncated) where truncated is True when more
    results exist beyond the limit.
    """
    engine = get_engine()
    conn = engine.raw_connection()
    fetch_limit = limit + 1 if _fetch_extra else limit
    try:
        rows = conn.execute(
            """SELECT a.id, a.title, a.summary, a.status, a.published_at,
                      a.slug, rank
               FROM articles_fts f
               JOIN articles a ON a.id = f.rowid
               WHERE articles_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, fetch_limit),
        ).fetchall()
    except Exception:
        rows = []
    conn.close()

    truncated = len(rows) > limit
    rows = rows[:limit]

    results = []
    for r in rows:
        results.append({
            "id": r[0], "title": r[1], "summary": r[2],
            "status": r[3], "published_at": r[4], "slug": r[5], "rank": r[6],
        })
    return results, truncated


def init_newsroom_db():
    """Create all newsroom tables."""
    Base.metadata.create_all(get_engine(), tables=[
        AdminUser.__table__, Tag.__table__, Article.__table__,
        ArticleSource.__table__, article_tags, DismissedSuggestion.__table__,
    ])
    init_fts()


def seed_default_tags():
    """Seed the default tag vocabulary."""
    session = get_session()
    defaults = [
        ("housing", "Housing", "Housing developments, affordable housing, residential projects"),
        ("zoning", "Zoning", "Zoning changes, rezonings, land use amendments"),
        ("data-centers", "Data Centers", "Data center developments and related infrastructure"),
        ("enforcement", "Enforcement", "Code enforcement, citations, penalties"),
        ("health", "Health", "Public health issues, health board actions"),
        ("environment", "Environment", "Environmental impact, sustainability, conservation"),
        ("transportation", "Transportation", "Roads, transit, bike/pedestrian infrastructure"),
        ("budget", "Budget", "Budgets, taxes, fees, financial decisions"),
        ("development", "Development", "General development proposals and projects"),
        ("economy", "Economy", "Economic development, incentives, job creation"),
        ("public-safety", "Public Safety", "Police, fire, emergency services"),
        ("education", "Education", "Schools, libraries, educational programs"),
        ("parks", "Parks", "Parks, recreation, open space"),
        ("water", "Water", "Water resources, utilities, infrastructure"),
        ("government", "Government", "Council operations, policy, governance"),
    ]
    existing = {t.name for t in session.execute(select(Tag)).scalars().all()}
    for slug, name, desc in defaults:
        if name not in existing:
            session.add(Tag(name=name, slug=slug, description=desc))
    session.commit()
    session.close()


def seed_default_users():
    """Seed default admin users."""
    from flask_bcrypt import generate_password_hash
    session = get_session()
    existing = {u.username for u in session.execute(select(AdminUser)).scalars().all()}
    users = [
        ("poston", "Poston", "admin", "changeme"),  # placeholder hash, set on first login
        ("editor", "Editor", "editor", "changeme"),
    ]
    for username, display, role, pw in users:
        if username not in existing:
            ph = generate_password_hash(pw).decode("utf-8")
            session.add(AdminUser(
                username=username, display_name=display,
                password_hash=ph, role=role,
            ))
    session.commit()
    session.close()
