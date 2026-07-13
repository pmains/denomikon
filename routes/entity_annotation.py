"""
Entity Mention Annotation — Flask routes.

Presents a stratified sample of ~300 agenda items for pure entity-span
annotation.  No type labels — just "is this span an entity?"

Supports:
  • One item at a time (item number / 300 progress)
  • Pre-highlighted candidate spans (auto-detected)
  • Accept / reject individual spans
  • Select arbitrary text to add new spans
  • Navigate: next, previous, jump to unreviewed
  • Progress tracking with auto-save
  • Export completed annotations as training data
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, Response

log = logging.getLogger(__name__)

annotation_bp = Blueprint("annotation", __name__, url_prefix="/annotation")

# Where the sample file lives
SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "mention-sample.json"

# In-memory cache of the sample (reloaded from disk on each request so edits persist)
_sample_cache: dict | None = None


def _load_sample() -> dict:
    global _sample_cache
    if _sample_cache is not None:
        return _sample_cache
    if not SAMPLE_PATH.exists():
        return {"meta": {}, "items": []}
    with open(SAMPLE_PATH) as f:
        _sample_cache = json.load(f)
    return _sample_cache


def _save_sample(data: dict):
    """Persist sample back to disk."""
    with open(SAMPLE_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


@annotation_bp.route("/")
def annotation_dashboard():
    """Dashboard showing progress across all annotation items."""
    data = _load_sample()
    items = data.get("items", [])

    total = len(items)
    reviewed = sum(1 for it in items if it.get("annotation_status") == "reviewed")
    in_progress = sum(1 for it in items if it.get("annotation_status") == "in_progress")
    unreviewed = sum(1 for it in items if it.get("annotation_status", "unreviewed") == "unreviewed")
    total_human_spans = sum(len(it.get("human_spans", [])) for it in items)

    # Breakdown by jurisdiction
    jur_counts: dict[str, int] = {}
    jur_reviewed: dict[str, int] = {}
    for it in items:
        jur = it.get("jurisdiction", "Unknown")
        jur_counts[jur] = jur_counts.get(jur, 0) + 1
        if it.get("annotation_status") == "reviewed":
            jur_reviewed[jur] = jur_reviewed.get(jur, 0) + 1

    # Find first unreviewed
    first_unreviewed = None
    for it in items:
        if it.get("annotation_status", "unreviewed") == "unreviewed":
            first_unreviewed = it.get("sample_id")
            break

    return render_template(
        "entity_annotation.html",
        page="dashboard",
        total=total,
        reviewed=reviewed,
        in_progress=in_progress,
        unreviewed=unreviewed,
        total_human_spans=total_human_spans,
        jur_counts=sorted(jur_counts.items(), key=lambda x: -x[1]),
        jur_reviewed=jur_reviewed,
        first_unreviewed=first_unreviewed,
    )


@annotation_bp.route("/review/<int:sample_id>")
def annotation_review(sample_id: int):
    """Review a single annotation item."""
    data = _load_sample()
    items = data.get("items", [])

    item = next((it for it in items if it.get("sample_id") == sample_id), None)
    if not item:
        return render_template("404.html"), 404

    # Get next/previous IDs for navigation
    prev_id = sample_id - 1 if sample_id > 1 else None
    next_id = sample_id + 1 if sample_id < len(items) else None

    # Auto-set status to in_progress when first viewed
    if item.get("annotation_status", "unreviewed") == "unreviewed":
        item["annotation_status"] = "in_progress"
        _save_sample(data)

    # Find first unreviewed for jump-to-next-unreviewed
    first_unreviewed = None
    for it in items:
        if it.get("annotation_status", "unreviewed") == "unreviewed":
            first_unreviewed = it.get("sample_id")
            break

    return render_template(
        "entity_annotation.html",
        page="review",
        item=item,
        prev_id=prev_id,
        next_id=next_id,
        first_unreviewed=first_unreviewed,
        total=len(items),
    )


@annotation_bp.route("/api/save-spans", methods=["POST"])
def api_save_spans():
    """Save human-annotated spans for an item."""
    data = _load_sample()
    body = request.get_json(force=True)

    sample_id = body.get("sample_id")
    human_spans = body.get("human_spans", [])
    status = body.get("status", "in_progress")

    item = next((it for it in data.get("items", []) if it.get("sample_id") == sample_id), None)
    if not item:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    item["human_spans"] = human_spans
    item["annotation_status"] = status
    _save_sample(data)

    return jsonify({"ok": True, "saved": len(human_spans), "status": status})


@annotation_bp.route("/api/save-notes", methods=["POST"])
def api_save_notes():
    """Save annotation notes for an item."""
    data = _load_sample()
    body = request.get_json(force=True)

    sample_id = body.get("sample_id")
    notes = body.get("notes", "")

    item = next((it for it in data.get("items", []) if it.get("sample_id") == sample_id), None)
    if not item:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    item["notes"] = notes
    _save_sample(data)

    return jsonify({"ok": True})


@annotation_bp.route("/api/stats")
def api_stats():
    """Return annotation progress stats as JSON."""
    data = _load_sample()
    items = data.get("items", [])

    total = len(items)
    reviewed = sum(1 for it in items if it.get("annotation_status") == "reviewed")
    in_progress = sum(1 for it in items if it.get("annotation_status") == "in_progress")
    unreviewed = sum(1 for it in items if it.get("annotation_status", "unreviewed") == "unreviewed")
    total_human_spans = sum(len(it.get("human_spans", [])) for it in items)
    total_auto_spans = sum(len(it.get("entity_spans", [])) for it in items)

    return jsonify({
        "total": total,
        "reviewed": reviewed,
        "in_progress": in_progress,
        "unreviewed": unreviewed,
        "total_human_spans": total_human_spans,
        "total_auto_spans": total_auto_spans,
        "pct_complete": round(reviewed / total * 100, 1) if total else 0,
    })


@annotation_bp.route("/api/reload")
def api_reload():
    """Reload sample from disk (clears in-memory cache)."""
    global _sample_cache
    _sample_cache = None
    data = _load_sample()
    return jsonify({"ok": True, "items": len(data.get("items", []))})


@annotation_bp.route("/export")
def export_training_data():
    """Export completed annotations as training data (JSONL format).

    Each line is one annotated item with {text, spans[]} for model training.
    """
    data = _load_sample()
    items = data.get("items", [])

    completed = [it for it in items
                 if it.get("annotation_status") == "reviewed"
                 and it.get("human_spans")]

    lines = []
    for it in completed:
        # Build training record: text + character-level spans
        record = {
            "text": it["text"],
            "spans": [
                {"start": s["start"], "end": s["end"], "text": s["text"]}
                for s in it["human_spans"]
            ],
            "meta": {
                "sample_id": it["sample_id"],
                "jurisdiction": it.get("jurisdiction"),
                "meeting_type": it.get("meeting_type"),
                "meeting_date": it.get("meeting_date"),
            },
        }
        lines.append(json.dumps(record, default=str))

    return Response(
        "\n".join(lines),
        mimetype="application/jsonl",
        headers={
            "Content-Disposition": f"attachment; filename=mention-training-{len(completed)}items.jsonl"
        },
    )
