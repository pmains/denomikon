"""Flask blueprint for the interactive entity viewer with live model selection.

Routes:
    GET  /entity-viewer            — Renders the viewer UI
    POST /entity-viewer/classify   — Runs classification and returns JSON
    GET  /entity-viewer/feedback   — Returns saved feedback (for display)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

log = logging.getLogger(__name__)

entity_viewer_bp = Blueprint("entity_viewer", __name__)

HERE = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = HERE / "tests" / "fixtures" / "bos_summary_text_4669.txt"
FEEDBACK_PATH = HERE / "data" / "entity-viewer-feedback.json"

# ── Default sample text ───────────────────────────────────────────────

_DEFAULT_TEXT: str | None = None


def _get_default_text() -> str:
    global _DEFAULT_TEXT
    if _DEFAULT_TEXT is None:
        if FIXTURE_PATH.exists():
            _DEFAULT_TEXT = FIXTURE_PATH.read_text(encoding="utf-8")
        else:
            _DEFAULT_TEXT = (
                "Sample meeting text. No fixture found at "
                f"{FIXTURE_PATH}. Upload text to classify."
            )
    return _DEFAULT_TEXT


# ── Lazy-loaded model ─────────────────────────────────────────────────

_model_cache: dict[str, object] = {}


def _load_model(model_name: str) -> object:
    """Lazy-load a model, caching it after first load."""
    from entities.predict import _MODEL_LOADERS
    if model_name not in _model_cache:
        loader = _MODEL_LOADERS.get(model_name)
        if loader is None:
            raise ValueError(f"Unknown model: {model_name}")
        log.info("Loading model '%s' on first request...", model_name)
        t0 = time.time()
        _model_cache[model_name] = loader()
        log.info("  Model '%s' loaded in %.2f s", model_name, time.time() - t0)
    return _model_cache[model_name]


def _get_type_colors() -> dict[str, str]:
    """Return the color mapping for entity types (hex).
    
    Matches the CSS type-color classes defined in entity-viewer.css.
    """
    return {
        "Person": "#4CAF50",
        "Company": "#2196F3",
        "Date": "#FF9800",
        "Developer": "#9C27B0",
        "Gov Agency": "#F44336",
        "Law/Code": "#795548",
        "Law Firm": "#E91E63",
        "Location": "#00BCD4",
        "Organization": "#3F51B5",
        "Permit": "#FF5722",
        "Planning": "#009688",
        "Unclassified": "#9E9E9E",
        "Case #": "#607D8B",
    }


# ── Routes ─────────────────────────────────────────────────────────────


@entity_viewer_bp.route("/entity-viewer")
def entity_viewer():
    """Render the entity viewer page with default text."""
    text = request.args.get("text", _get_default_text())
    model = request.args.get("model", "fasttext")
    return render_template(
        "entity_viewer.html",
        text=text,
        selected_model=model,
        type_colors=_get_type_colors(),
        models=[
            {"id": "fasttext", "name": "fastText", "f1": "0.82"},
            {"id": "modernbert", "name": "ModernBERT", "f1": "0.905"},
            {"id": "minilm_xgboost", "name": "MiniLM + XGBoost", "f1": "0.876"},
            {"id": "setfit", "name": "SetFit", "f1": "0.938"},
        ],
    )


@entity_viewer_bp.route("/entity-viewer/classify", methods=["POST"])
def classify():
    """Run the selected model on the given text and return entity results as JSON."""
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    model_name = data.get("model", "fasttext")

    if not text:
        return jsonify({"error": "No text provided"}), 400

    if model_name not in ("fasttext", "modernbert", "minilm_xgboost", "setfit"):
        return jsonify({"error": f"Unknown model: {model_name}"}), 400

    try:
        from entities.predict import predict
        # Run predict with lazy-loaded model
        results = predict(text, model_name=model_name)
        return jsonify({
            "model": model_name,
            "results": results,
            "total": len(results),
            "entities": len([r for r in results if r["is_entity"]]),
            "noise": len([r for r in results if not r["is_entity"]]),
        })
    except Exception as e:
        log.exception("Classification failed")
        return jsonify({"error": str(e)}), 500


@entity_viewer_bp.route("/entity-viewer/feedback", methods=["POST", "GET"])
def feedback():
    """Save or retrieve user feedback on entity classifications.

    POST: save feedback for one entity
        {"normalized_name": "...", "action": "accept"|"reject", "model": "..."}
    GET: return the list of all saved feedback
    """
    if request.method == "GET":
        return _get_feedback()
    return _save_feedback()


def _get_feedback():
    """Return the saved feedback as JSON."""
    if FEEDBACK_PATH.exists():
        data = json.loads(FEEDBACK_PATH.read_text())
    else:
        data = []
    return jsonify(data)


def _save_feedback():
    """Append one feedback entry to the feedback file."""
    entry = request.get_json(silent=True) or {}
    required = ("normalized_name", "action")
    if not all(k in entry for k in required):
        return jsonify({"error": "Missing required fields: normalized_name, action"}), 400
    if entry["action"] not in ("accept", "reject"):
        return jsonify({"error": "Action must be 'accept' or 'reject'"}), 400

    entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    feedback = []
    if FEEDBACK_PATH.exists():
        feedback = json.loads(FEEDBACK_PATH.read_text())
    feedback.append(entry)

    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_PATH.write_text(json.dumps(feedback, indent=2))
    return jsonify({"status": "ok", "entry": entry})
