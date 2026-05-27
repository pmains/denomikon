"""Social media publishing — Bluesky integration for article announcements.

Requires environment variables:
  BLUESKY_HANDLE=poliscopic.bsky.social
  BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx

The app password is generated from Bluesky Settings > App Passwords.
Never use your account password directly.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Load .env file if available (for local dev)
_env_file = Path(__file__).resolve().parent / ".env"
if not _env_file.exists():
    _env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        # Fallback: parse key=value lines manually
        with open(_env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


def post_to_bluesky(title: str, summary: str, url: str) -> bool:
    """Post an article announcement to Bluesky.

    Args:
        title: Article title (used in the post text)
        summary: Article summary (appended after the title)
        url: Full article URL (e.g. https://poliscopic.com/articles/slug)

    Returns True on success, False on failure.
    """
    handle = os.environ.get("BLUESKY_HANDLE", "")
    app_password = os.environ.get("BLUESKY_APP_PASSWORD", "")

    if not handle or not app_password:
        log.warning(
            "Bluesky not configured. Set BLUESKY_HANDLE and "
            "BLUESKY_APP_PASSWORD environment variables."
        )
        return False

    try:
        from atproto import Client, client_utils

        client = Client()
        client.login(handle, app_password)

        # Build the post text: title + summary + link
        # Bluesky has a 300-char limit, so keep it concise
        text_builder = client_utils.TextBuilder()

        # Title (bold)
        text_builder.text(title)

        # Separator + summary if there's room
        remaining = 300 - len(title) - len(url) - 20  # 20 chars for spacing
        if summary and remaining > 20:
            text_builder.text("\n\n")
            if len(summary) > remaining:
                summary = summary[: remaining - 3] + "..."
            text_builder.text(summary)

        # Link
        text_builder.text("\n\n")
        text_builder.link(url, url)

        post = client.send_post(text_builder)
        log.info("Posted to Bluesky: https://bsky.app/profile/%s/post/%s",
                 handle, post.uri.split("/")[-1])
        return True

    except Exception as e:
        log.error("Failed to post to Bluesky: %s", e)
        return False
