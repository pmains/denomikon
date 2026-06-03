"""Social media publishing — Bluesky integration for article announcements.

Requires environment variables:
  BLUESKY_HANDLE=poliscopic.bsky.social
  BLUESKY_APP_PASSWORD=xxxx-x....xxxx

The app password is generated from Bluesky Settings > App Passwords.
Never use your account password directly.
"""

import logging
import os
import tempfile
import urllib.request
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)

# Load .env file if available
_env_file = Path(__file__).resolve().parent / ".env"
if not _env_file.exists():
    _env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        with open(_env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


def _get_client():
    """Authenticate and return a Bluesky client."""
    from atproto import Client

    handle = os.environ.get("BLUESKY_HANDLE", "")
    app_password = os.environ.get("BLUESKY_APP_PASSWORD", "")
    if not handle or not app_password:
        raise ValueError("BLUESKY_HANDLE and BLUESKY_APP_PASSWORD not set")

    client = Client()
    client.login(handle, app_password)
    return client


def _fetch_image_bytes(url_or_path: str) -> bytes | None:
    """Fetch an image from a URL or local path.

    Handles:
    - Absolute HTTP(S) URLs
    - Absolute filesystem paths
    - Relative paths like ``static/uploads/abc.jpg``
    - URL paths like ``/static/uploads/abc.jpg``

    Returns raw image bytes, or None on failure.
    """
    if not url_or_path:
        return None
    try:
        if url_or_path.startswith(("http://", "https://", "ftp://")):
            with urllib.request.urlopen(url_or_path, timeout=15) as resp:
                return resp.read()
        else:
            # Try as-is
            path = Path(url_or_path)
            if path.exists():
                return path.read_bytes()
            # Try relative to project root (strip leading /)
            rel = url_or_path.lstrip("/")
            path = Path(__file__).resolve().parent.parent / rel
            if path.exists():
                return path.read_bytes()
            # Try relative to static/uploads (common case)
            path = Path(__file__).resolve().parent.parent / "static" / "uploads" / rel
            if path.exists():
                return path.read_bytes()
    except Exception as e:
        log.warning("Failed to fetch image %s: %s", url_or_path[:80], e)
    return None


def _resize_image_for_bluesky(image_bytes: bytes, max_size=1_000_000) -> bytes:
    """Resize and compress an image for Bluesky's 1MB blob limit.

    Bluesky link cards look best at ~1200×630 (1.91:1 aspect ratio).
    Strips EXIF metadata. Returns JPEG bytes.
    """
    try:
        from PIL import Image, ImageOps
        img = Image.open(BytesIO(image_bytes))

        # Convert to RGB if needed (RGBA → white background)
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Target aspect ratio 1.91:1 (link card standard), max 1200px wide
        target_w = min(1200, img.width)
        target_h = int(target_w / 1.91)

        # Fit image into the canvas
        img.thumbnail((target_w, target_h * 3), Image.LANCZOS)

        # Center-crop to 1.91:1
        if img.width / img.height > 1.91:
            new_w = int(img.height * 1.91)
            offset = (img.width - new_w) // 2
            img = img.crop((offset, 0, offset + new_w, img.height))
        elif img.height / img.width > 1 / 1.91:
            new_h = int(img.width / 1.91)
            offset = (img.height - new_h) // 2
            img = img.crop((0, offset, img.width, offset + new_h))

        img = img.resize((target_w, target_h), Image.LANCZOS)

        # Compress JPEG to stay under limit
        quality = 85
        buf = BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        while buf.tell() > max_size and quality > 20:
            quality -= 10
            buf.seek(0)
            buf.truncate()
            img.save(buf, "JPEG", quality=quality, optimize=True)

        return buf.getvalue()
    except ImportError:
        # No PIL — return original bytes (may fail on oversize)
        return image_bytes
    except Exception as e:
        log.warning("Image resize failed: %s", e)
        return image_bytes


def post_to_bluesky(
    title: str,
    summary: str = "",
    url: str = "",
    image_bytes: bytes | None = None,
    draft_text: str = "",
    alt_text: str = "",
) -> str | None:
    """Post an article announcement to Bluesky with a link card embed.

    Uses ``app.bsky.embed.external`` to render a rich card with image,
    headline, description, and domain — matching the Democracy Docket pattern.

    Args:
        title: Article title (shown in the card headline).
        summary: Article summary (shown in the card description).
        url: Full article URL.
        image_bytes: Raw image bytes for the card thumbnail. If None, no
            image is attached (card will show the link only).
        draft_text: Curated post text (the skeet body). If empty, uses
            title + summary as before.
        alt_text: Alt text for the link card image.

    Returns the Bluesky post AT URI on success, or None on failure.
    """
    try:
        client = _get_client()
    except ValueError as e:
        log.warning("Bluesky not configured: %s", e)
        return False
    except Exception as e:
        log.error("Failed to authenticate with Bluesky: %s", e)
        return None

    from atproto import models

    try:
        # ── 1. Build the external embed with optional thumbnail ──
        thumb_blob = None
        if image_bytes:
            try:
                resized = _resize_image_for_bluesky(image_bytes)
                blob = client.upload_blob(resized)
                thumb_blob = blob.blob
            except Exception as e:
                log.warning("Image upload to Bluesky failed: %s", e)
                thumb_blob = None

        embed = models.AppBskyEmbedExternal.Main(
            external=models.AppBskyEmbedExternal.External(
                uri=url,
                title=title[:120],
                description=summary[:300] if summary else "",
                thumb=thumb_blob,
            )
        )

        # ── 2. Build the post text ──
        if draft_text:
            text = draft_text[:300]
        else:
            text = title[:280]

        # ── 3. Send the post ──
        result = client.send_post(
            text=text,
            embed=embed,
            langs=["en-US"],
        )

        uri = str(result.uri)
        log.info(
            "Posted to Bluesky: https://bsky.app/profile/%s/post/%s",
            os.environ.get("BLUESKY_HANDLE", ""),
            uri.split("/")[-1],
        )
        return uri

    except Exception as e:
        log.error("Failed to post to Bluesky: %s", e)
        return None


def post_draft_to_bluesky(
    title: str,
    summary: str,
    url: str,
    draft_text: str = "",
    image_path: str = "",
    alt_text: str = "",
) -> str | None:
    """Convenience wrapper: fetch image from path, then post."""
    image_bytes = _fetch_image_bytes(image_path) if image_path else None
    return post_to_bluesky(
        title=title,
        summary=summary,
        url=url,
        image_bytes=image_bytes,
        draft_text=draft_text,
        alt_text=alt_text,
    )
