#!/usr/bin/env python3
"""
Benchmark 3: Embeddings + XGBoost/LightGBM with structured features.

Uses embeddings from MiniLM-L6-v2 (generated in benchmark 2) combined
with structured features. Trains XGBoost and LightGBM classifiers.

If benchmark 2 embeddings exist, reuses them; otherwise generates fresh.

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/03_xgboost_lgbm.py \
        > data/benchmark-03-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import json
import logging
import os
import sys
import time
import numpy as np
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("benchmark_xgb_lgbm")

DATA_DIR = Path("data/benchmark")
BENCHMARK_DIR = DATA_DIR / "xgboost_lgbm"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

FEATURE_NAMES = ['all_caps_ratio', 'ctx_after_len', 'ctx_before_len',
    'ctx_has_colon', 'ctx_has_dash', 'has_ampersand_or_and', 'has_entity_keyword',
    'has_gov_keyword', 'has_legal_suffix', 'has_middle_initial', 'has_number',
    'n_cap_words', 'n_commas', 'name_len', 'pct_cap_words', 'punct_density', 'word_count']


def load_split(name: str) -> list[dict]:
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


def save_results(results: dict, name: str):
    path = BENCHMARK_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info("Saved results to %s", path)


def get_feature_vector(examples: list[dict]) -> np.ndarray:
    vecs = []
    for ex in examples:
        feats = ex.get("features", {})
        row = [float(feats.get(f, 0)) for f in FEATURE_NAMES]
        vecs.append(row)
    return np.array(vecs, dtype=np.float32)


def normalize_features(X: np.ndarray, means: np.ndarray = None, stds: np.ndarray = None):
    if means is None:
        means = X.mean(axis=0)
        stds = X.std(axis=0)
        stds[stds == 0] = 1.0
    return (X - means) / stds, means, stds


def generate_embeddings(texts: list[str], model_name: str = "all-MiniLM-L6-v2",
                        batch_size: int = 64) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    log.info("Loading model %s...", model_name)
    model = SentenceTransformer(model_name)
    log.info("Generating %d embeddings...", len(texts))
    t0 = time.time()
    embeddings = model.encode(texts, batch_size=batch_size, show_progress_bar=True,
                              normalize_embeddings=True)
    elapsed = time.time() - t0
    log.info("Generated %d embeddings in %.1fs (%.1f/s)",
             len(embeddings), elapsed, len(embeddings) / elapsed if elapsed else 0)
    return np.array(embeddings, dtype=np.float32)


def compute_metrics(y_true: list[int], y_pred: list[int]) -> dict:
    tp = sum(1 for p, l in zip(y_pred, y_true) if p == 1 and l == 1)
    fp = sum(1 for p, l in zip(y_pred, y_true) if p == 1 and l == 0)
    tn = sum(1 for p, l in zip(y_pred, y_true) if p == 0 and l == 0)
    fn = sum(1 for p, l in zip(y_pred, y_true) if p == 0 and l == 1)
    
    precision_1 = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall_1 = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1_1 = 2 * precision_1 * recall_1 / (precision_1 + recall_1) if (precision_1 + recall_1) > 0 else 0.0
    
    precision_0 = tn / (tn + fn) if (tn + fn) > 0 else 0.0
    recall_0 = tn / (tn + fp) if (tn + fp) > 0 else 0.0
    f1_0 = 2 * precision_0 * recall_0 / (precision_0 + recall_0) if (precision_0 + recall_0) > 0 else 0.0
    
    macro_f1 = (f1_1 + f1_0) / 2
    accuracy = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) > 0 else 0.0
    
    return {
        "macro_f1": round(macro_f1, 4),
        "accuracy": round(accuracy, 4),
        "entity": {"precision": round(precision_1, 4), "recall": round(recall_1, 4), "f1": round(f1_1, 4)},
        "noise": {"precision": round(precision_0, 4), "recall": round(recall_0, 4), "f1": round(f1_0, 4)},
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def run_xgboost(train_X, train_y, test_X, test_y) -> dict:
    import xgboost as xgb
    
    log.info("  Training XGBoost...")
    
    # Compute scale_pos_weight for imbalance
    n_neg = sum(1 for y in train_y if y == 0)
    n_pos = sum(1 for y in train_y if y == 1)
    scale = n_neg / max(1, n_pos)
    
    t0 = time.time()
    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale,
        random_state=42,
        eval_metric="logloss",
        use_label_encoder=False,
        verbosity=0,
    )
    model.fit(train_X, train_y, eval_set=[(test_X, test_y)], verbose=False)
    train_time = time.time() - t0
    
    pred = model.predict(test_X)
    metrics = compute_metrics(test_y, pred.tolist())
    metrics["train_time_s"] = round(train_time, 2)
    
    # Feature importance
    if hasattr(model, "feature_importances_"):
        metrics["top_features"] = sorted(
            [(float(v), f"f{i}") for i, v in enumerate(model.feature_importances_)],
            reverse=True
        )[:10]
    
    # Throughput
    t0 = time.time()
    model.predict(test_X[:10])
    infer_time = time.time() - t0
    metrics["throughput_items_per_s"] = round(10 / infer_time, 1) if infer_time > 0 else 0
    
    log.info("  XGBoost: macro_f1=%.4f, entity_f1=%.4f, noise_f1=%.4f, train=%.1fs",
             metrics["macro_f1"], metrics["entity"]["f1"],
             metrics["noise"]["f1"], train_time)
    
    return metrics


def run_lightgbm(train_X, train_y, test_X, test_y) -> dict:
    import lightgbm as lgb
    
    log.info("  Training LightGBM...")
    
    # Compute class weight
    n_neg = sum(1 for y in train_y if y == 0)
    n_pos = sum(1 for y in train_y if y == 1)
    
    t0 = time.time()
    model = lgb.LGBMClassifier(
        n_estimators=200,
        max_depth=8,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        class_weight="balanced",
        random_state=42,
        verbosity=-1,
    )
    model.fit(train_X, train_y, eval_set=[(test_X, test_y)])
    train_time = time.time() - t0
    
    pred = model.predict(test_X)
    metrics = compute_metrics(test_y, pred.tolist())
    metrics["train_time_s"] = round(train_time, 2)
    
    t0 = time.time()
    model.predict(test_X[:10])
    infer_time = time.time() - t0
    metrics["throughput_items_per_s"] = round(10 / infer_time, 1) if infer_time > 0 else 0
    
    log.info("  LightGBM: macro_f1=%.4f, entity_f1=%.4f, noise_f1=%.4f, train=%.1fs",
             metrics["macro_f1"], metrics["entity"]["f1"],
             metrics["noise"]["f1"], train_time)
    
    return metrics


def main():
    log.info("=" * 60)
    log.info("Benchmark 3: Embeddings + XGBoost/LightGBM")
    log.info("=" * 60)
    
    train = load_split("train")
    val = load_split("val")
    test = load_split("test")
    
    train_y = [ex["label"] for ex in train]
    test_y = [ex["label"] for ex in test]
    
    overall_results = {}
    
    # ── 3a. Features only (no embeddings) ──
    log.info("\n--- Variant: Structured Features Only ---")
    train_feats = get_feature_vector(train)
    test_feats = get_feature_vector(test)
    train_feats_norm, feat_means, feat_stds = normalize_features(train_feats)
    test_feats_norm, _, _ = normalize_features(test_feats, feat_means, feat_stds)
    
    xgb_feats = run_xgboost(train_feats_norm, train_y, test_feats_norm, test_y)
    lgb_feats = run_lightgbm(train_feats_norm, train_y, test_feats_norm, test_y)
    overall_results["features_only"] = {"xgboost": xgb_feats, "lightgbm": lgb_feats}
    
    # ── 3b. Name embeddings only ──
    log.info("\n--- Variant: Name Embeddings Only ---")
    train_texts = [ex["name"] for ex in train]
    test_texts = [ex["name"] for ex in test]
    train_emb = generate_embeddings(train_texts)
    test_emb = generate_embeddings(test_texts)
    
    xgb_name = run_xgboost(train_emb, train_y, test_emb, test_y)
    lgb_name = run_lightgbm(train_emb, train_y, test_emb, test_y)
    overall_results["name_embeddings"] = {"xgboost": xgb_name, "lightgbm": lgb_name}
    
    # ── 3c. Context embeddings + features ──
    log.info("\n--- Variant: Context Embeddings + Features ---")
    train_texts_ctx = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}" for ex in train]
    test_texts_ctx = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}" for ex in test]
    train_emb_ctx = generate_embeddings(train_texts_ctx)
    test_emb_ctx = generate_embeddings(test_texts_ctx)
    
    train_combined = np.concatenate([train_emb_ctx, train_feats_norm], axis=1)
    test_combined = np.concatenate([test_emb_ctx, test_feats_norm], axis=1)
    
    xgb_ctx = run_xgboost(train_combined, train_y, test_combined, test_y)
    lgb_ctx = run_lightgbm(train_combined, train_y, test_combined, test_y)
    overall_results["context_embeddings_features"] = {"xgboost": xgb_ctx, "lightgbm": lgb_ctx}
    
    # ── Save ──
    save_results(overall_results, "results")
    
    # ── Summary ──
    log.info("\n" + "=" * 60)
    log.info("XGBoost/LightGBM Summary")
    log.info("=" * 60)
    for variant, res in overall_results.items():
        for method, metrics in res.items():
            log.info("  %-35s %-10s  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f  train=%.1fs",
                     variant, method, metrics["macro_f1"],
                     metrics["entity"]["f1"], metrics["noise"]["f1"],
                     metrics["accuracy"], metrics["train_time_s"])
    
    log.info("Done.")


if __name__ == "__main__":
    main()
