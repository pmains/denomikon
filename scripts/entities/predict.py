#!/usr/bin/env python3
"""Unified entity prediction — classify candidate names as entity vs noise.

Usage:
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/predict.py \\
        --model fasttext --text "Gilmore Planning and Architecture"

    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/predict.py \\
        --model modernbert --text "Tiffany & Bosco, P.A."

    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/predict.py \\
        --model minilm_xgboost --text "Speedworld, LLC"

    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/predict.py \\
        --model setfit --text "Burch & Cracchiolo"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("predict")

HERE = Path(__file__).resolve().parent.parent.parent
BENCHMARK = HERE / "data" / "benchmark"
CLASSIFIED_PATH = HERE / "data" / "entity-candidates-classified.valid.json"

# ── In-memory model cache (lazy-loaded) ──────────────────────────────
_model_cache: dict[str, object] = {}

# ── Type mapping from GPT-4o-mini llm_entity_type → our ontology ──────
LLM_TYPE_TO_ONTOLOGY = {
    "person": "Person",
    "other_organization": "Organization",
    "advocacy_group": "Organization",
    "law_firm": "Law Firm",
    "government_agency": "Gov Agency",
    "utility": "Organization",
    "developer": "Developer",
    "planning_firm": "Planning",
    "consulting_firm": "Organization",
    "legal_firm": "Law Firm",
    "legal_suffix": "Organization",
    "company": "Company",
    "engineering_firm": "Organization",
    "organization": "Organization",
    "consultant": "Person",
    "healthcare_organization": "Organization",
    "staffing_firm": "Organization",
    "event_management_firm": "Organization",
    "noise": None,  # filtered out
}

# ── Entity type lookup (from classified.json) ─────────────────────────
_type_lookup: Optional[dict[str, str]] = None


def _load_type_lookup() -> dict[str, str]:
    """Load and cache the GPT-4o-mini classification results.

    Returns a dict mapping normalized_name -> ontology entity type string.
    """
    global _type_lookup
    if _type_lookup is not None:
        return _type_lookup

    if not CLASSIFIED_PATH.exists():
        log.warning("Classified data not found at %s — entity types will be 'unclassified'",
                     CLASSIFIED_PATH)
        _type_lookup = {}
        return _type_lookup

    with open(CLASSIFIED_PATH) as f:
        data = json.load(f)

    _type_lookup = {}
    for entry in data:
        norm = entry.get("normalized_name", "").lower().strip()
        llm_type = entry.get("llm_entity_type", "")
        if not norm or not llm_type:
            continue
        ontology_type = LLM_TYPE_TO_ONTOLOGY.get(llm_type)
        if ontology_type:
            _type_lookup[norm] = ontology_type

    log.info("Loaded %d entity type mappings from classified data", len(_type_lookup))
    return _type_lookup


def resolve_entity_type(normalized_name: str) -> str:
    """Look up entity type from GPT-4o-mini classifications.

    Returns the ontology type string, or 'Unclassified' if unknown.
    """
    lookup = _load_type_lookup()
    norm = normalized_name.lower().strip()
    return lookup.get(norm, "Unclassified")


# ── Model loaders ─────────────────────────────────────────────────────


def _load_fasttext():
    """Load the fastText model (name + context features variant)."""
    import fasttext
    model_path = BENCHMARK / "fasttext" / "model_name_context_features.bin"
    if not model_path.exists():
        # Fallback to name-only
        model_path = BENCHMARK / "fasttext" / "model_name_only.bin"
    if not model_path.exists():
        raise FileNotFoundError(
            f"fastText model not found at {model_path}. "
            f"Looked for name_context_features and name_only."
        )
    log.info("Loading fastText model from %s ...", model_path)
    t0 = time.time()
    model = fasttext.load_model(str(model_path))
    log.info("  Loaded in %.2f s", time.time() - t0)
    return model


def _load_modernbert():
    """Load the ModernBERT sequence classification model."""
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    model_path = BENCHMARK / "modernbert" / "model_name_only" / "checkpoint-212"
    if not model_path.exists():
        raise FileNotFoundError(
            f"ModernBERT model not found at {model_path}"
        )
    log.info("Loading ModernBERT from %s ...", model_path)
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(str(model_path))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_path))
    log.info("  Loaded in %.2f s", time.time() - t0)
    return {"model": model, "tokenizer": tokenizer}


def _load_minilm_xgboost():
    """Load the MiniLM + XGBoost model."""
    import xgboost as xgb
    from sentence_transformers import SentenceTransformer

    model_path = BENCHMARK / "minilm_xgboost" / "model.json"
    if not model_path.exists():
        raise FileNotFoundError(
            f"MiniLM+XGBoost model not found at {model_path}. "
            f"Run scripts/entities/save_models.py first."
        )
    log.info("Loading MiniLM+XGBoost from %s ...", model_path)
    t0 = time.time()
    xgb_model = xgb.XGBClassifier()
    xgb_model.load_model(str(model_path))
    encoder = SentenceTransformer("all-MiniLM-L6-v2")
    log.info("  Loaded in %.2f s", time.time() - t0)
    return {"model": xgb_model, "encoder": encoder}


def _load_setfit():
    """Load the SetFit model (encoder + logistic regression classifier)."""
    from sentence_transformers import SentenceTransformer
    import joblib

    model_dir = BENCHMARK / "setfit" / "model"
    encoder_path = model_dir / "encoder"
    classifier_path = model_dir / "classifier.joblib"
    if not encoder_path.exists() or not classifier_path.exists():
        raise FileNotFoundError(
            f"SetFit model not found at {model_dir}. "
            f"Run scripts/entities/save_models.py first."
        )
    log.info("Loading SetFit encoder from %s ...", encoder_path)
    t0 = time.time()
    encoder = SentenceTransformer(str(encoder_path))
    classifier = joblib.load(str(classifier_path))
    log.info("  Loaded in %.2f s", time.time() - t0)
    return {"encoder": encoder, "classifier": classifier}


_MODEL_LOADERS = {
    "fasttext": _load_fasttext,
    "modernbert": _load_modernbert,
    "minilm_xgboost": _load_minilm_xgboost,
    "setfit": _load_setfit,
}

# ── Individual candidate classifier ───────────────────────────────────


def classify_candidate(name: str, model_name: str, model_obj: object) -> dict:
    """Classify a single candidate name string using the loaded model.

    Returns:
        {"name": str, "is_entity": bool, "confidence": float}
    """
    if model_name == "fasttext":
        # fastText predict returns (labels, probabilities)
        labels, probs = model_obj.predict(name.strip().replace("\n", " "))
        # fastText labels are __label__0 or __label__1
        label = int(labels[0].replace("__label__", ""))
        is_entity = bool(label == 1)
        confidence = float(probs[0])
        return {"name": name, "is_entity": is_entity, "confidence": confidence}

    elif model_name == "modernbert":
        tokenizer = model_obj["tokenizer"]
        model = model_obj["model"]
        inputs = tokenizer(name, return_tensors="pt", truncation=True, max_length=128)
        import torch
        with torch.no_grad():
            outputs = model(**inputs)
            probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
            pred = outputs.logits.argmax(dim=-1).item()
        is_entity = bool(pred == 1)
        confidence = float(probs[0, pred].item())
        return {"name": name, "is_entity": is_entity, "confidence": confidence}

    elif model_name == "minilm_xgboost":
        encoder = model_obj["encoder"]
        xgb_model = model_obj["model"]
        embedding = encoder.encode([name])
        pred = xgb_model.predict(embedding)[0]
        proba = xgb_model.predict_proba(embedding)[0]
        is_entity = bool(pred == 1)
        confidence = float(proba[int(pred)])
        return {"name": name, "is_entity": is_entity, "confidence": confidence}

    elif model_name == "setfit":
        encoder = model_obj["encoder"]
        classifier = model_obj["classifier"]
        emb = encoder.encode([name], show_progress_bar=False)
        proba = classifier.predict_proba(emb)[0]
        pred = proba.argmax()
        is_entity = bool(pred == 1)
        confidence = float(proba[pred])
        return {"name": name, "is_entity": is_entity, "confidence": confidence}

    else:
        raise ValueError(f"Unknown model: {model_name}")


# ── Full prediction pipeline ──────────────────────────────────────────


def predict(text: str, model_name: str = "fasttext") -> list[dict]:
    """Extract candidates from text and classify each one.

    Args:
        text: Raw agenda/meeting text.
        model_name: One of "fasttext", "modernbert", "minilm_xgboost", "setfit".

    Returns:
        List of dicts, each with:
            normalized_name, display_name, pattern, context_snippet,
            span_start, span_end,
            is_entity, confidence, entity_type
    """
    # Lazy-load the model
    if model_name not in _model_cache:
        loader = _MODEL_LOADERS.get(model_name)
        if loader is None:
            raise ValueError(f"Unknown model: {model_name}. "
                             f"Choose from: {list(_MODEL_LOADERS.keys())}")
        _model_cache[model_name] = loader()
    model_obj = _model_cache[model_name]

    # Extract candidates using the standalone function from discover_candidates
    from entities.discover_candidates import extract_candidates_from_text
    candidates = extract_candidates_from_text(text)

    # Deduplicate by normalized_name, keeping the first occurrence's span
    seen_norms: dict[str, dict] = {}
    for c in candidates:
        norm = c["normalized_name"]
        if norm not in seen_norms:
            seen_norms[norm] = c
    candidates = list(seen_norms.values())

    # Classify each unique candidate
    results = []
    for c in candidates:
        name = c["display_name"]
        result = classify_candidate(name, model_name, model_obj)
        entity_type = resolve_entity_type(c["normalized_name"])
        results.append({
            "normalized_name": c["normalized_name"],
            "display_name": c["display_name"],
            "pattern": c["pattern"],
            "context_snippet": c["context_snippet"],
            "span_start": c.get("span_start", 0),
            "span_end": c.get("span_end", 0),
            "is_entity": result["is_entity"],
            "confidence": round(result["confidence"], 4),
            "entity_type": entity_type,
        })

    # Sort: confirmed entities first, then by confidence descending
    results.sort(key=lambda r: (not r["is_entity"], -r["confidence"]))

    return results


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Classify entity candidates in text"
    )
    parser.add_argument(
        "--model", choices=list(_MODEL_LOADERS.keys()), default="fasttext",
        help="Model to use for classification (default: fasttext)"
    )
    parser.add_argument(
        "--text", type=str, default="",
        help="Text to analyze"
    )
    parser.add_argument(
        "--file", type=str, default="",
        help="Path to text file containing agenda text"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON"
    )
    args = parser.parse_args()

    if args.text:
        text = args.text
    elif args.file:
        with open(args.file) as f:
            text = f.read()
    else:
        parser.print_help()
        sys.exit(1)

    results = predict(text, model_name=args.model)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        entities = [r for r in results if r["is_entity"]]
        noise = [r for r in results if not r["is_entity"]]

        print(f"Model: {args.model}")
        print(f"Candidates found: {len(results)}")
        print(f"  Entities:   {len(entities)}")
        print(f"  Noise:      {len(noise)}")
        print()

        if entities:
            print("─" * 72)
            print("ENTITIES:")
            print("─" * 72)
            for r in entities:
                print(f"  {r['display_name']:<40s}  [{r['entity_type']:<15s}]  "
                      f"conf={r['confidence']:.3f}  ({r['pattern']})")
            print()

        if noise:
            print("─" * 72)
            print("NOISE (filtered):")
            print("─" * 72)
            for r in noise[:20]:
                print(f"  {r['display_name']:<40s}  conf={r['confidence']:.3f}  ({r['pattern']})")
            if len(noise) > 20:
                print(f"  ... and {len(noise) - 20} more")


if __name__ == "__main__":
    main()
