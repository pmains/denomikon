"""Podcast routes — Crown of Aragon podcast feed on poliscopic.com."""

import os
from pathlib import Path
from flask import Blueprint, send_from_directory, abort

_here = Path(__file__).resolve().parent.parent
PODCAST_DIR = _here / "static" / "podcast"

podcast_bp = Blueprint("podcast", __name__, url_prefix="/podcast")


@podcast_bp.route("/")
def podcast_index():
    """Serve a simple listing page or redirect to feed."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>The Crown of Aragon Podcast</title></head>
    <body>
        <h1>The Crown of Aragon: A Singular Mediterranean Empire</h1>
        <p><a href="/podcast/feed.xml">RSS Feed</a></p>
        <p>Subscribe in your favorite podcast app.</p>
    </body>
    </html>
    """


@podcast_bp.route("/feed.xml")
def feed():
    """Serve the podcast RSS feed."""
    feed_path = PODCAST_DIR / "feed.xml"
    if not feed_path.exists():
        abort(404)
    resp = send_from_directory(PODCAST_DIR, "feed.xml", mimetype="application/rss+xml")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@podcast_bp.route("/<path:filename>")
def podcast_file(filename):
    """Serve podcast audio files and other assets."""
    file_path = PODCAST_DIR / filename
    if not file_path.exists() or file_path.is_dir():
        abort(404)
    return send_from_directory(PODCAST_DIR, filename)
