#!/usr/bin/env python3
"""
role_classifier.py — ML role classification using 6-signal XGBoost ensemble.

Phase 4 of the entity pipeline.  Loads the pre-trained XGBoost ensemble that
combines 6 signals: fastText probs, sentence embeddings (name + context),
TF-IDF context windows, surface-form features, and body group.

Trained at 95.5% accuracy on 29k labeled mentions.

Usage:
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/role_classifier.py
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/role_classifier.py --dry-run
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/role_classifier.py --force
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import numpy as np
import xgboost as xgb
from sklearn.feature_extraction.text import TfidfVectorizer
from sentence_transformers import SentenceTransformer
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("role_classifier")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")

# Model artifacts
XGB_PATH = os.path.join(ROOT, "data", "role_ensemble_xgb.json")
TFIDF_PATH = os.path.join(ROOT, "data", "role_ensemble_tfidf.pkl")
LABELS_PATH = os.path.join(ROOT, "data", "role_ensemble_labels.pkl")

# Load artifact dependencies at module level
ROLE_NAMES = pickle.load(open(LABELS_PATH, "rb"))
TFIDF = pickle.load(open(TFIDF_PATH, "rb"))
SENTENCE_ENCODER = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
XGB_MODEL = xgb.Booster()
XGB_MODEL.load_model(XGB_PATH)

BODY_GROUPS = {"phoenix-cc":0,"tempe-cc":0,"chandler-cc":0,"scottsdale-cc":0,"mesa-cc":0,
               "glendale-cc":0,"goodyear-cc":0,"gilbert-cc":0,"bos":1,"pz":2,"phoenix-pc":2,
               "phoenix-ti":3,"phoenix-ps":3,"phoenix-ed":3,"phoenix-boa":4,"scottsdale-boa":4}
BATCH_SIZE = 500

# Roles we can confidently assign from structured data — skip these
STRUCTURAL_ROLES = {"applicant", "attorney", "staff", "owner", "presenter",
                    "representative", "reference", "mentioned",
                    "iga_counterparty"}


def load_model():
    """All models loaded at module level. This is a no-op for compatibility."""
    return True


def extract_surface(name: str, ctx: str) -> list:
    n, c = name.lower(), ctx.lower()
    return [len(name), len(name.split()),
            1 if name.isupper() and len(name)>3 else 0,
            1 if name and name[0].isupper() else 0,
            1 if "&" in n or " AND " in name else 0,
            1 if "," in name else 0, name.count(","),
            1 if any(w in n for w in ["attorney","esq","llc","inc","corp","plc","ltd"]) else 0,
            1 if any(w in n for w in ["jr","sr","iii","phd","md","jd"]) else 0,
            1 if any(w in n for w in ["city of","county of","town of","arizona","state of"]) else 0,
            1 if any(w in c for w in ["applicant","attorney","representative","staff"]) else 0,
            1 if name.endswith(".") else 0, 1 if "." in name else 0]


def build_features(names: list[str], contexts: list[str], full_texts: list[str],
                   bodies: list[str]) -> np.ndarray:
    """Build the full 1297-dim feature vector for the XGBoost ensemble."""
    import fasttext
    ft_model = fasttext.load_model(os.path.join(ROOT, "data", "role_classifier.bin"))

    # Signal 1: fastText probs
    ft_prob = np.zeros((len(names), len(ROLE_NAMES)))
    for i in range(len(names)):
        txt = (contexts[i] + " " + names[i]).replace("\n", " ").replace("\r", " ")
        if not txt.strip(): continue
        p = ft_model.predict(txt, k=len(ROLE_NAMES))
        for j, lb in enumerate(p[0]):
            role = lb.replace("__label__", "")
            if role in ROLE_NAMES:
                ft_prob[i, ROLE_NAMES.index(role)] = float(p[1][j])

    # Signal 2: sentence embeddings (name)
    name_emb = SENTENCE_ENCODER.encode(names, show_progress_bar=False)

    # Signal 3: sentence embeddings (context window)
    ctx_windows = []
    for i in range(len(names)):
        ft = full_texts[i] or ""
        nm = names[i] or ""
        if ft:
            idx = ft.lower().find(nm.lower()[:30])
            if idx >= 0:
                ctx_windows.append(ft[max(0,idx-10):min(len(ft),idx+len(nm)+100)])
            else:
                ctx_windows.append(nm)
        else:
            ctx_windows.append(nm)
    ctx_emb = SENTENCE_ENCODER.encode(ctx_windows, show_progress_bar=False)

    # Signal 4: TF-IDF on context
    tfidf_feats = TFIDF.transform(ctx_windows).toarray()

    # Signal 5: surface features
    surf = np.array([extract_surface(names[i], contexts[i]) for i in range(len(names))])

    # Signal 6: body group
    body_f = np.zeros((len(names), 6))
    for i, b in enumerate(bodies):
        body_f[i, BODY_GROUPS.get(b, 5)] = 1

    return np.hstack([ft_prob, name_emb, ctx_emb, tfidf_feats, surf, body_f])


def classify_entity(entity_name: str, context_snippet: str = "",
                    full_text: str = "", body: str = "") -> tuple[str, float]:
    """Predict the role of an entity using the full XGBoost ensemble.

    Returns (role, confidence).  For single-entity use; batch use
    should call build_features() + XGB_MODEL directly.
    """
    features = build_features([entity_name], [context_snippet],
                               [full_text], [body])
    d = xgb.DMatrix(features)
    preds = XGB_MODEL.predict(d)[0]
    idx = int(np.argmax(preds))
    return ROLE_NAMES[idx], float(preds[idx])


def run_role_classifier(
    engine,
    dry_run: bool = False,
    force: bool = False,
    confidence: float = 0.5,
    verbose: bool = False,
) -> dict:
    """Run role classification phase. Returns structured result dict.

    Loads the XGBoost ensemble, scans unclassified entity_mentions,
    predicts roles, and bulk-updates the DB.
    """
    total_updated = 0
    total_scanned = 0
    distribution: dict[str, int] = {}
    start_ts = time.time()

    where_clauses = ["1=1"]
    if not force:
        where_clauses.append("(em.role_in_context IS NULL OR em.role_in_context = '')")

    with engine.connect() as conn:
        total_mention_count = conn.execute(text(
            "SELECT COUNT(*) FROM entity_mentions"
        )).scalar()

        rows = conn.execute(text(f"""
            SELECT em.id, e.name, em.context_snippet, em.role_in_context,
                   ai.body, ai.agenda_item_text
            FROM entity_mentions em
            JOIN entities e ON em.entity_id = e.id
            LEFT JOIN agenda_items ai ON ai.id = CAST(em.source_id AS INTEGER)
            WHERE {' AND '.join(where_clauses)}
            LIMIT 10000
        """)).fetchall()

        log.info("Total mentions: %d. Scanning %d mentions.",
                 total_mention_count, len(rows))
        total_scanned = len(rows)
        if not rows:
            elapsed = time.time() - start_ts
            return {
                "success": True,
                "total_scanned": 0,
                "total_updated": 0,
                "duration_s": round(elapsed, 1),
                "distribution": {},
                "dry_run": dry_run,
            }

        # Batch feature extraction
        names = [r[1] or "" for r in rows]
        ctxs = [r[2] or "" for r in rows]
        current_roles = [r[3] or "" for r in rows]
        bodies = [r[4] or "" for r in rows]
        texts = [r[5] or "" for r in rows]

        log.info("Building features for %d mentions...", len(rows))
        X = build_features(names, ctxs, texts, bodies)
        d = xgb.DMatrix(X)
        preds = XGB_MODEL.predict(d)
        pred_labels = np.argmax(preds, axis=1)
        confs = np.max(preds, axis=1)

        # Classify in batches
        update_data = []
        for i, mention_id in enumerate([r[0] for r in rows]):
            if not force and current_roles[i] in STRUCTURAL_ROLES:
                continue

            predicted_role = ROLE_NAMES[pred_labels[i]]
            pred_confidence = float(confs[i])
            distribution[predicted_role] = distribution.get(predicted_role, 0) + 1

            if dry_run:
                if verbose and i < 10:
                    log.info("  %s → %s (%.2f) [mid=%d]",
                             names[i][:30], predicted_role, pred_confidence, mention_id)
                continue

            if pred_confidence >= confidence and predicted_role != current_roles[i]:
                update_data.append((predicted_role, mention_id))
                total_updated += 1

            if len(update_data) >= BATCH_SIZE:
                _flush_updates(engine, update_data)
                update_data = []

        if update_data:
            _flush_updates(engine, update_data)

    elapsed = time.time() - start_ts

    return {
        "success": True,
        "total_scanned": total_scanned,
        "total_updated": total_updated,
        "duration_s": round(elapsed, 1),
        "distribution": dict(sorted(distribution.items(), key=lambda x: -x[1])[:10]),
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(
        description="XGBoost ensemble role classification (95.5% accuracy)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be classified without writing")
    parser.add_argument("--force", action="store_true",
                        help="Re-classify even entities with existing roles")
    parser.add_argument("--confidence", type=float, default=0.5,
                        help="Minimum confidence to apply (default: 0.5)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    engine = get_engine()
    result = run_role_classifier(
        engine,
        dry_run=args.dry_run,
        force=args.force,
        confidence=args.confidence,
        verbose=args.verbose,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d scanned, %d updated in %.0fs",
             mode, result["total_scanned"], result["total_updated"],
             result["duration_s"])
    if result["distribution"]:
        log.info("Distribution: %s", result["distribution"])

    print(json.dumps({"phase": "role_classifier", **result}))


def _flush_updates(engine, updates: list[tuple[str, int]]):
    """Bulk update role classifications."""
    if not updates:
        return
    with engine.begin() as conn:
        case_parts = []
        for role, mention_id in updates:
            es = role.replace("'", "''")
            case_parts.append(f"WHEN {mention_id} THEN '{es}'")
        ids = ",".join(str(mid) for _, mid in updates)
        conn.execute(text(f"""
            UPDATE entity_mentions
            SET role_in_context = CASE id
                {' '.join(case_parts)}
                ELSE role_in_context
            END
            WHERE id IN ({ids})
        """))


if __name__ == "__main__":
    main()
