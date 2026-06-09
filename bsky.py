#!/usr/bin/env python3
"""Post an article to Bluesky with an article card.

Usage:
    python bsky.py --article 60 --text "Glendale's 10-year transportation plan is out. Here's what's in it."

Posts directly to Bluesky — no drafts, no queue, no cron.
The article's featured image, title, and summary form the card.
"""

import argparse
import os
import sys
from pathlib import Path

# Default to development DB unless told otherwise
os.environ.setdefault("POLISCOPIC_DB_TIER", "development")

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here / "scripts"))

from db import get_session
from db.newsroom import Article
from social import post_draft_to_bluesky


def main():
    parser = argparse.ArgumentParser(description="Post an article to Bluesky")
    parser.add_argument("--article", "-a", type=int, required=True,
                        help="Article ID to post")
    parser.add_argument("--text", "-t", type=str, required=True,
                        help="Skeet text (displayed as the post body)")
    parser.add_argument("--url", default="https://poliscopic.com",
                        help="Base URL for article links (default: https://poliscopic.com)")
    parser.add_argument("--dev", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--test", action="store_true", help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args()
    if args.test:
        os.environ["POLISCOPIC_DB_TIER"] = "test"

    session = get_session()
    article = session.get(Article, args.article)

    if not article:
        print(f"Article {args.article} not found.")
        session.close()
        return 1

    # Check if already posted
    from bluesky_sync import is_already_posted, mark_posted
    if is_already_posted(article.id):
        print(f"Article {args.article} has already been posted to Bluesky. Use --force to skip this check (not implemented yet).")
        # For now, let them post again if they really want to — mark_posted only adds a record
        # so we'll just warn and proceed.

    article_url = f"{args.url}/articles/{article.slug}"
    image = article.featured_image or ""

    print(f"Posting article {args.article}: {article.title}")
    print(f"  URL:    {article_url}")
    print(f"  Text:   {args.text}")
    print(f"  Image:  {image[:80] or '(none)'}...")

    uri = post_draft_to_bluesky(
        title=article.title,
        summary=article.summary or "",
        url=article_url,
        draft_text=args.text,
        image_path=image,
    )

    if uri:
        mark_posted(article.id, bluesky_post_uri=uri)
        print(f"  ✓ Posted: https://bsky.app/profile/poliscopic.bsky.social/post/{uri.split('/')[-1]}")
        session.close()
        return 0
    else:
        print("  ✗ Failed")
        session.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
