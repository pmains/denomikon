#!/usr/bin/env python3
"""Post approved Bluesky draft skeets, or create drafts for unposted articles.

Run this to post approved drafts:

    python scripts/bluesky_sync.py --post-drafts

Or to auto-generate drafts for new articles:

    python scripts/bluesky_sync.py --create-drafts

Uses poliscopic.com URLs (not localhost). Tracks which articles have been
posted via social_posts table and skeet_drafts table.

Flags:
  --create-drafts  Auto-generate skeet_drafts for published articles without one
  --post-drafts    Post approved skeet_drafts to Bluesky
  --post-approved  Alias for --post-drafts
  --limit=N        Max items to process (default: 10)
  --article=N      Process a specific article by ID
  --force          Create drafts even if already posted
  --dry-run        Show what would be done without actually posting
  --url=BASE       Override base URL (default: https://poliscopic.com)
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bluesky_sync")

from db import get_session, init_db
from db.newsroom import Article, SkeetDraft
from sqlalchemy import select, desc, not_
from social import post_draft_to_bluesky


# ── Social post tracking ──

TRACKING_DB = Path(__file__).resolve().parent.parent / "data" / "bluesky_tracking.sqlite"


def _get_tracking_conn():
    """Get a connection to the tracking database.

    This lives in a separate SQLite file so it persists across sync.sh deploys
    (which replace maricopa.sqlite). The path sits alongside maricopa.sqlite.
    """
    import sqlite3
    TRACKING_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TRACKING_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            bluesky_post_uri TEXT NOT NULL DEFAULT '',
            posted_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def is_already_posted(article_id: int) -> bool:
    """Check if an article has been posted to Bluesky.

    Checks both the skeet_drafts table (new system) and the legacy
    social_posts tracking DB (persistent across deploys).
    """
    # Check persistent tracking DB
    conn = _get_tracking_conn()
    row = conn.execute(
        "SELECT 1 FROM social_posts WHERE article_id = ?", (article_id,)
    ).fetchone()
    conn.close()
    if row:
        return True

    # Check skeet_drafts table
    session = get_session()
    draft = session.execute(
        select(SkeetDraft).where(
            SkeetDraft.article_id == article_id,
            SkeetDraft.status == "posted",
        )
    ).scalar_one_or_none()
    session.close()
    if draft:
        return True

    # Check legacy social_posts in main DB (may have been wiped by deploy)
    return False


def mark_posted(article_id: int, bluesky_post_uri: str = ""):
    """Record that an article was posted, in the persistent tracking DB."""
    conn = _get_tracking_conn()
    conn.execute(
        "INSERT INTO social_posts (article_id, bluesky_post_uri, posted_at) VALUES (?, ?, ?)",
        (article_id, bluesky_post_uri, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


# ── Create drafts ──

import re

_DOLLAR_RE = re.compile(r'\$[\d,]+(?:\.\d+)?(?:[MmKkBb]illion|[MmBb]n)?')
# Patterns for detecting article type from title
_POLICY_ACTION_RE = re.compile(
    r'(moves to|crack down|regulat(e|ory|ion)|restructures|adopts|approves?|creates?|passes?|launches)',
    re.IGNORECASE,
)
_DEVELOPMENT_RE = re.compile(
    r'(rezoning|development plan|preliminary plat|hotel|apartment|condo|subdivision|mixed.use|master plan|surf park)',
    re.IGNORECASE,
)


def _shorten_title(title: str, max_len: int = 150) -> str:
    """Truncate a title cleanly at a word boundary."""
    if len(title) <= max_len:
        return title
    cut = title.rfind(" ", 0, max_len)
    return title[:cut] + "..."


def generate_draft_text(article: Article) -> str:
    """Generate a social-media hook from the article.

    Strategy by article type:
    - **Dollar/spending** (e.g. "Chandler renews … for $153,400"):
      Lead with the figure. Append the "so what" from the summary.
    - **Policy/ordinance** (e.g. "moves to crack down on bird feeding"):
      Question hook that creates a knowledge gap.
    - **Development** (e.g. "hotel at Cannon Beach surf park"):
      Announce the proposal, frame as conditional.
    - **Everything else**: Clean, specific headline.
    """
    title = article.title or ""
    summary = article.summary or ""

    dollars = _DOLLAR_RE.findall(title + " " + summary)
    title_lower = title.lower()

    # ── Detect article type ──
    is_policy = bool(_POLICY_ACTION_RE.search(title_lower))
    is_development = bool(_DEVELOPMENT_RE.search(title_lower))
    is_dollar = bool(dollars)

    # ── Dollar-first hooks ──
    if is_dollar:
        prefix = f"{dollars[0]} — "
        short_title = _shorten_title(title, 200 - len(prefix))
        line = prefix + short_title
        line = line[:250]  # leave room for summary append

        # Append the "so what" sentence from the summary
        if summary:
            so_what = summary.replace("\n", " ").strip()
            # Try to find the sentence that explains significance
            sentences = [s.strip() for s in so_what.split(". ") if s.strip()]
            for s in sentences:
                s = s.strip()
                if any(kw in s.lower() for kw in ["but", "however", "without", "no law",
                                                    "no state", "no city", "leaves",
                                                    "means", "remains", "question"]):
                    so_what = s
                    break
            remaining = 290 - len(line)
            if remaining > 20:
                line += "\n\n" + so_what[:remaining - 2]

        return line[:297].rstrip(",.; ")

    # ── Policy hooks (question format creates curiosity gap) ──
    if is_policy:
        short = _shorten_title(title, 200)
        return short[:297]

    # ── Development hooks (announce + condition frame) ──
    if is_development:
        short = _shorten_title(title, 200)
        return short[:297]

    # ── Fallback: clean, specific headline ──
    return _shorten_title(title, 295)


def create_drafts(session, limit=10, force=False, base_url="https://poliscopic.com"):
    """Create skeet_drafts for published articles that don't have one."""
    # Find published articles without a skeet draft
    subq = select(SkeetDraft.article_id).where(
        SkeetDraft.status.in_(["draft", "approved", "posted"])
    ).subquery()

    articles = session.execute(
        select(Article)
        .where(
            Article.status == "published",
            not_(Article.id.in_(select(subq))),
        )
        .order_by(desc(Article.published_at))
        .limit(limit)
    ).scalars().all()

    if not articles:
        log.info("No new articles to draft.")
        return 0

    count = 0
    for article in articles:
        if not force and is_already_posted(article.id):
            log.info("  Skipping %d (%s) — already posted", article.id, article.title[:50])
            continue

        draft = SkeetDraft(
            article_id=article.id,
            draft_text=generate_draft_text(article),
            status="draft",
            image_path=article.featured_image or "",
        )
        session.add(draft)
        session.flush()
        log.info("  Created draft %d for article %d: %s", draft.id, article.id, article.title[:60])
        count += 1

    session.commit()
    log.info("Created %d new skeet drafts.", count)
    return count


# ── Post pending drafts ──

# ── List drafts (pitches) ──

def list_drafts(session, status_filter=None, limit=20):
    """Display skeet drafts and their current status as a readable pitch list."""
    q = select(SkeetDraft).order_by(SkeetDraft.created_at.desc()).limit(limit)
    if status_filter:
        q = q.where(SkeetDraft.status == status_filter)
    drafts = session.execute(q).scalars().all()

    if not drafts:
        log.info("No drafts found%s.",
                 f" with status='{status_filter}'" if status_filter else "")
        return

    article_ids = list(set(d.article_id for d in drafts))
    articles = {}
    if article_ids:
        for a in session.execute(
            select(Article).where(Article.id.in_(article_ids))
        ).scalars():
            articles[a.id] = a

    print(f"{'ID':>4} {'Status':<10} {'Article':<60} {'Draft Text'}")
    print(f"{'──':>4} {'──────':<10} {'───────':<60} {'──────────'}")
    for d in drafts:
        article = articles.get(d.article_id)
        art_title = (article.title[:57] + "...") if article and len(article.title) > 57 else (article.title or "(deleted)")
        print(f"{d.id:>4} {d.status:<10} {art_title:<60} {d.draft_text[:80]}")
        if len(d.draft_text) > 80:
            print(f"{'':>76} {d.draft_text[80:160]}")
        if article:
            posted = is_already_posted(article.id)
            print(f"{'':>4} {'':10} {'':<60} (article posted to Bluesky: {posted})")
        print()

    return drafts


def approve_drafts(session, draft_ids: list[int]):
    """Mark one or more drafts as approved (ready to post)."""
    rows = 0
    for did in draft_ids:
        draft = session.get(SkeetDraft, did)
        if not draft:
            log.warning("Draft %d not found", did)
            continue
        if draft.status == "posted":
            log.info("Draft %d already posted", did)
            continue
        draft.status = "approved"
        log.info("Approved draft %d for article %d", did, draft.article_id)
        rows += 1
    session.commit()
    return rows


def post_pending_drafts(session, limit=10, dry_run=False, base_url="https://poliscopic.com"):
    """Post skeet drafts that have been reviewed and approved.

    Only posts drafts with status="approved" — the human-in-the-loop
    gate. Use --approve <id> first, then --post-drafts.
    """
    drafts = session.execute(
        select(SkeetDraft)
        .where(SkeetDraft.status == "approved")
        .order_by(SkeetDraft.created_at)
        .limit(limit)
    ).scalars().all()

    if not drafts:
        log.info("No approved drafts to post. Use --list-drafts to see pending drafts, then --approve <id> to approve them.")
        return 0

    count = 0
    for draft in drafts:
        article = session.get(Article, draft.article_id)
        if not article:
            log.warning("  Draft %d: article %d not found", draft.id, draft.article_id)
            draft.status = "skipped"
            continue

        # Dedup against persistent tracking DB
        if is_already_posted(article.id):
            log.info("  Draft %d: article %d already posted, skipping", draft.id, article.id)
            draft.status = "skipped"
            continue

        article_url = f"{base_url}/articles/{article.slug}"
        log.info("  Posting draft %d: %s", draft.id, draft.draft_text[:60])

        if dry_run:
            continue

        uri = post_draft_to_bluesky(
            title=article.title, summary=article.summary,
            url=article_url, draft_text=draft.draft_text,
            image_path=draft.image_path,
        )

        if uri:
            draft.status = "posted"
            draft.bluesky_post_uri = uri
            draft.posted_at = datetime.now(timezone.utc)
            session.flush()
            mark_posted(article.id, bluesky_post_uri=uri)
            log.info("    ✓ Posted")
            count += 1
        else:
            log.error("    ✗ Failed")

    session.commit()
    log.info("Posted %d approved drafts.", count)
    return count


# ── Main ──

def post_article(session, article_id, draft_text=None, base_url="https://poliscopic.com"):
    """Post a single article to Bluesky directly, bypassing the draft queue."""
    article = session.get(Article, article_id)
    if not article:
        log.error("Article %d not found", article_id)
        return None

    if is_already_posted(article.id):
        log.info("Article %d already posted to Bluesky, skipping", article_id)
        return None

    url = f"{base_url}/articles/{article.slug}"
    text = draft_text if draft_text else f"{article.title} {url}"

    log.info("Posting article %d: %s", article_id, article.title[:60])
    uri = post_draft_to_bluesky(
        title=article.title, summary=article.summary,
        url=url, draft_text=text,
        image_path=article.featured_image,
    )

    if uri:
        from datetime import datetime, timezone
        mark_posted(article.id, bluesky_post_uri=uri)
        draft = SkeetDraft(
            article_id=article.id, draft_text=text,
            status="posted", bluesky_post_uri=uri,
            posted_at=datetime.now(timezone.utc),
        )
        session.add(draft)
        session.commit()
        log.info("  ✓ Posted: https://bsky.app/profile/polisopic.bsky.social/post/%s", uri.split('/')[-1])
        return uri
    else:
        log.error("  ✗ Failed")
        return None


def main():
    parser = argparse.ArgumentParser(description="Bluesky posting")
    parser.add_argument("--post", type=int, metavar="ARTICLE_ID",
                        help="Post an article to Bluesky directly (no queue)")
    parser.add_argument("--text", type=str, default=None,
                        help="Custom post text (used with --post)")
    parser.add_argument("--url", default="https://poliscopic.com", help="Base URL")

    # Legacy/deprecated options (still functional but not recommended)
    parser.add_argument("--create-drafts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--list-drafts", nargs="?", const="draft", help=argparse.SUPPRESS)
    parser.add_argument("--approve", type=int, nargs="+", metavar="ID", help=argparse.SUPPRESS)
    parser.add_argument("--post-drafts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=10, help=argparse.SUPPRESS)
    parser.add_argument("--article", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.post:
        init_db()
        session = get_session()
        result = post_article(session, args.post, draft_text=args.text, base_url=args.url)
        session.close()
        return 0 if result else 1

    # Legacy commands (deprecated)
    if args.create_drafts or args.post_drafts or args.list_drafts is not None or args.approve:
        print("Note: The draft queue system is deprecated. Use --post <article-id> to post directly.")
        init_db()
        session = get_session()

        if args.create_drafts:
            create_drafts(session, limit=args.limit, force=args.force, base_url=args.url)
        if args.list_drafts is not None:
            list_drafts(session, status_filter=args.list_drafts or None, limit=args.limit)
        if args.approve:
            count = approve_drafts(session, args.approve)
            if count:
                log.info("Approved %d draft(s).", count)
        if args.post_drafts:
            post_pending_drafts(session, limit=args.limit, dry_run=args.dry_run, base_url=args.url)

        session.close()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
