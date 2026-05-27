#!/usr/bin/env python3
"""Post unpublished articles to Bluesky.

Run this after syncing to production to announce new articles:

    python scripts/bluesky_sync.py --limit=5

Uses poliscopic.com URLs (not localhost). Tracks which articles have been
posted via a social_posts table so it never duplicates.

Flags:
  --limit=N    Max articles to post (default: 10)
  --article=N  Post a specific article by ID
  --force      Re-post even if previously posted
  --dry-run    Show what would be posted without actually posting
  --url=BASE   Override base URL (default: https://poliscopic.com)
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure scripts/importable
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bluesky_sync")

from db import get_session, init_db
from db.newsroom import Article
from sqlalchemy import select, desc
from social import post_to_bluesky


# ── Social post tracking table ──


def ensure_tracking_table():
    """Create social_posts tracking table if it doesn't exist."""
    from sqlalchemy import text as _sql
    from db.core import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(_sql("""
            CREATE TABLE IF NOT EXISTS social_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                platform VARCHAR(32) NOT NULL DEFAULT 'bluesky',
                post_url VARCHAR(512),
                posted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.commit()


def get_unposted_articles(session, limit=10):
    """Get published articles that haven't been posted to Bluesky yet."""
    from sqlalchemy import text as _sql

    posted_ids = session.execute(
        _sql("SELECT DISTINCT article_id FROM social_posts WHERE platform = 'bluesky'")
    ).scalars().all()

    articles = session.execute(
        select(Article)
        .where(
            Article.status == "published",
            Article.id.notin_(posted_ids) if posted_ids else True,
        )
        .order_by(desc(Article.published_at))
        .limit(limit)
    ).scalars().all()
    return articles


def mark_posted(session, article_id, platform="bluesky", post_url=""):
    """Record that an article was posted to a platform."""
    from sqlalchemy import text as _sql
    session.execute(
        _sql("INSERT INTO social_posts (article_id, platform, post_url) VALUES (:aid, :plat, :url)"),
        {"aid": article_id, "plat": platform, "url": post_url},
    )
    session.flush()


def post_article(session, article, base_url, dry_run=False):
    """Post a single article to Bluesky."""
    article_url = f"{base_url}/articles/{article.slug}"
    log.info("Posting: %s → %s", article.title[:60], article_url)

    if dry_run:
        log.info("  [DRY RUN] Would post: %s", article.title)
        return True

    success = post_to_bluesky(
        title=article.title,
        summary=article.summary or "",
        url=article_url,
    )

    if success:
        mark_posted(session, article.id, post_url=article_url)
        log.info("  ✓ Posted")
    else:
        log.error("  ✗ Failed to post")

    return success


def main():
    parser = argparse.ArgumentParser(description="Post articles to Bluesky")
    parser.add_argument("--limit", type=int, default=10, help="Max articles")
    parser.add_argument("--article", type=int, default=None, help="Specific article ID")
    parser.add_argument("--force", action="store_true", help="Re-post even if already posted")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted")
    parser.add_argument("--url", default="https://poliscopic.com", help="Base URL")
    args = parser.parse_args()

    init_db()
    ensure_tracking_table()

    session = get_session()

    if args.article:
        articles = session.execute(
            select(Article).where(Article.id == args.article)
        ).scalars().all()
        if not articles:
            log.error("Article %d not found.", args.article)
            session.close()
            return 1
    else:
        if args.force:
            from sqlalchemy import select as _select
            articles = session.execute(
                select(Article)
                .where(Article.status == "published")
                .order_by(desc(Article.published_at))
                .limit(args.limit)
            ).scalars().all()
        else:
            articles = get_unposted_articles(session, limit=args.limit)

    if not articles:
        log.info("No articles to post.")
        session.close()
        return 0

    log.info("Found %d article(s) to post", len(articles))
    posted = 0
    failed = 0

    for article in articles:
        if post_article(session, article, args.url, dry_run=args.dry_run):
            posted += 1
        else:
            failed += 1

    if not args.dry_run:
        session.commit()

    session.close()
    log.info("Done: %d posted, %d failed", posted, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
