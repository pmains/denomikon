"""Persistent analytics database — survives sync.sh overwrites.

Uses ``data/analytics.sqlite``, which is EXCLUDED from sync.sh.
This separates runtime data (page views, social posts) from deployable
content data (meetings, articles, sources).

Usage::

    from db.analytics import track_page_view, get_trending
    track_page_view(article_id)
    trending = get_trending()  # last 24 hours
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Analytics database path — alongside maricopa.sqlite but never synced
_ANALYTICS_DIR = Path(__file__).resolve().parent.parent / "data"
_ANALYTICS_DB = str(_ANALYTICS_DIR / "analytics.sqlite")

# Track whether we've initialized
_initialized = False


def _get_conn() -> sqlite3.Connection:
    """Get a connection to the analytics database, creating it if needed."""
    global _initialized
    _ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_ANALYTICS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not _initialized:
        _init_schema(conn)
        _initialized = True
    return conn


def _init_schema(conn: sqlite3.Connection):
    """Create analytics tables if they don't exist."""
    conn.executescript("""
        -- Page views for trending calculation
        CREATE TABLE IF NOT EXISTS page_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            viewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_page_views_article_time
            ON page_views(article_id, viewed_at);

        -- Article view count (denormalized for fast reads)
        CREATE TABLE IF NOT EXISTS article_view_counts (
            article_id INTEGER PRIMARY KEY,
            view_count INTEGER NOT NULL DEFAULT 0
        );

        -- Social media post tracking
        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            platform VARCHAR(32) NOT NULL DEFAULT 'bluesky',
            post_url VARCHAR(512),
            posted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_social_posts_article
            ON social_posts(article_id, platform);
    """)
    conn.commit()


# ── Page view tracking ──


def track_page_view(article_id: int):
    """Record a page view for an article."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO page_views (article_id) VALUES (?)",
            (article_id,),
        )
        conn.execute("""
            INSERT INTO article_view_counts (article_id, view_count)
            VALUES (?, 1)
            ON CONFLICT(article_id) DO UPDATE SET
                view_count = view_count + 1
        """, (article_id,))
        conn.commit()
    except Exception as e:
        log.warning("Failed to track page view: %s", e)
        conn.rollback()
    finally:
        conn.close()


def get_view_count(article_id: int) -> int:
    """Get the lifetime view count for an article."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT view_count FROM article_view_counts WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        return row["view_count"] if row else 0
    finally:
        conn.close()


def get_trending(limit: int = 5) -> list[dict]:
    """Get trending articles from the last 24 hours."""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT article_id, COUNT(*) as recent_views
            FROM page_views
            WHERE viewed_at >= datetime('now', '-1 day')
            GROUP BY article_id
            ORDER BY recent_views DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Social media post tracking ──


def mark_social_posted(article_id: int, platform: str = "bluesky",
                       post_url: str = ""):
    """Record that an article was posted to a social platform."""
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO social_posts (article_id, platform, post_url)
            VALUES (?, ?, ?)
        """, (article_id, platform, post_url))
        conn.commit()
    except Exception as e:
        log.warning("Failed to mark social post: %s", e)
        conn.rollback()
    finally:
        conn.close()


def is_social_posted(article_id: int, platform: str = "bluesky") -> bool:
    """Check if an article has been posted to a platform."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM social_posts WHERE article_id = ? AND platform = ?",
            (article_id, platform),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_unposted_articles(article_ids: list[int],
                          platform: str = "bluesky") -> list[int]:
    """Filter to articles that haven't been posted yet."""
    if not article_ids:
        return []
    conn = _get_conn()
    try:
        placeholders = ",".join("?" for _ in article_ids)
        posted = conn.execute(
            f"SELECT article_id FROM social_posts "
            f"WHERE article_id IN ({placeholders}) AND platform = ?",
            [*article_ids, platform],
        ).fetchall()
        posted_ids = {r["article_id"] for r in posted}
        return [aid for aid in article_ids if aid not in posted_ids]
    finally:
        conn.close()
