#!/usr/bin/env python3
"""
Benchmark 2: Sentence Embeddings + Linear Classifiers (LR/SVM).

Generates embeddings using a small sentence transformer model,
then trains logistic regression and linear SVM on:
- Name only (embedding)
- Name + context (embedding)
- Name + context + features (embedding + structured features)

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/02_embeddings_lr_svm.py \
        > data/benchmark-02-$(date +%Y%m%d-%H%M).log 2>&1 &
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
log = logging.getLogger("benchmark_embeddings")

DATA_DIR = Path("data/benchmark")
BENCHMARK_DIR = DATA_DIR / "embeddings"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)


def load_split(name: str) -> list[dict]:
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


def save_results(results: dict, name: str):
    path = BENCHMARK_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info("Saved results to %s", path)


# ── Embedding generation ────────────────────────────────────────────

def generate_embeddings(texts: list[str], model_name: str = "all-MiniLM-L6-v2",
                        batch_size: int = 64) -> np.ndarray:
    """Generate embeddings using sentence-transformers."""
    from sentence_transformers import SentenceTransformer
    
    log.info("Loading model %s...", model_name)
    t0 = time.time()
    model = SentenceTransformer(model_name)
    load_time = time.time() - t0
    log.info("Model loaded in %.1fs", load_time)
    
    log.info("Generating %d embeddings...", len(texts))
    t0 = time.time()
    embeddings = model.encode(texts, batch_size=batch_size, show_progress_bar=True,
                              normalize_embeddings=True)
    elapsed = time.time() - t0
    log.info("Generated %d embeddings in %.1fs (%.1f/s)",
             len(embeddings), elapsed, len(embeddings) / elapsed if elapsed else 0)
    
    return np.array(embeddings, dtype=np.float32)


# ── Build feature vectors ──────────────────────────────────────────

FEATURE_NAMES = ['all_caps_ratio', 'ctx_after_len', 'ctx_before_len',
    'ctx_has_colon', 'ctx_has_dash', 'has_ampersand_or_and', 'has_entity_keyword',
    'has_gov_keyword', 'has_legal_suffix', 'has_middle_initial', 'has_number',
    'n_cap_words', 'n_commas', 'name_len', 'pct_cap_words', 'punct_density', 'word_count']


def get_feature_vector(examples: list[dict]) -> np.ndarray:
    """Extract structured features as a matrix."""
    vecs = []
    for ex in examples:
        feats = ex.get("features", {})
        row = [float(feats.get(f, 0)) for f in FEATURE_NAMES]
        vecs.append(row)
    return np.array(vecs, dtype=np.float32)


def normalize_features(X: np.ndarray, means: np.ndarray = None, stds: np.ndarray = None):
    """Z-score normalize features."""
    if means is None:
        means = X.mean(axis=0)
        stds = X.std(axis=0)
        stds[stds == 0] = 1.0
    X_norm = (X - means) / stds
    return X_norm, means, stds


# ── Prepare data variants ───────────────────────────────────────────

def get_name_texts(examples: list[dict]) -> list[str]:
    return [ex["name"] for ex in examples]


def get_context_texts(examples: list[dict]) -> list[str]:
    texts = []
    for ex in examples:
        ctx = f"{ex.get('context_before', '')} {ex.get('context_after', '')}".strip()
        texts.append(f"{ex['name']} [SEP] {ctx}")
    return texts


# ── Metrics ──────────────────────────────────────────────────────────

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


# ── Single run ──────────────────────────────────────────────────────

def run_variant(name: str, train_X, train_y, test_X, test_y):
    """Train LR and SVM, return best result."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.svm import LinearSVC
    from sklearn.calibration import CalibratedClassifierCV
    
    results = {}
    
    # Logistic Regression
    log.info("  Training Logistic Regression...")
    t0 = time.time()
    lr = LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced", random_state=42)
    lr.fit(train_X, train_y)
    lr_train_time = time.time() - t0
    
    lr_pred = lr.predict(test_X)
    lr_metrics = compute_metrics(test_y, lr_pred.tolist())
    lr_metrics["train_time_s"] = round(lr_train_time, 2)
    
    # Inference throughput
    t0 = time.time()
    lr.predict(test_X)
    infer_time = time.time() - t0
    lr_metrics["throughput_items_per_s"] = round(len(test_X) / infer_time, 1) if infer_time > 0 else 0
    lr_metrics["model_size_mb"] = "~0.01"  # Tiny
    results["logistic_regression"] = lr_metrics
    log.info("  LR: macro_f1=%.4f", lr_metrics["macro_f1"])
    
    # Linear SVM
    log.info("  Training Linear SVM...")
    t0 = time.time()
    svm = LinearSVC(max_iter=2000, C=1.0, class_weight="balanced", random_state=42, dual="auto")
    svm.fit(train_X, train_y)
    svm_train_time = time.time() - t0
    
    svm_pred = svm.predict(test_X)
    svm_metrics = compute_metrics(test_y, svm_pred.tolist())
    svm_metrics["train_time_s"] = round(svm_train_time, 2)
    
    t0 = time.time()
    svm.predict(test_X)
    infer_time = time.time() - t0
    svm_metrics["throughput_items_per_s"] = round(len(test_X) / infer_time, 1) if infer_time > 0 else 0
    svm_metrics["model_size_mb"] = "~0.01"
    results["linear_svm"] = svm_metrics
    log.info("  SVM: macro_f1=%.4f", svm_metrics["macro_f1"])
    
    # Calibrated SVM (for probability estimates)
    # Note: LinearSVC doesn't naturally give probabilities, but we can calibrate
    try:
        t0 = time.time()
        cal_svm = CalibratedClassifierCV(svm, cv=3)
        cal_svm.fit(train_X, train_y)
        cal_time = time.time() - t0
        cal_pred = cal_svm.predict(test_X)
        cal_metrics = compute_metrics(test_y, cal_pred.tolist())
        cal_metrics["train_time_s"] = round(cal_time, 2)
        results["calibrated_svm"] = cal_metrics
    except Exception as e:
        log.warning("Calibrated SVM failed: %s", e)
    
    # Best
    best_key = max(results, key=lambda k: results[k]["macro_f1"])
    results["best"] = results[best_key]
    results["best_method"] = best_key
    log.info("  Best: %s with macro_f1=%.4f", best_key, results["best"]["macro_f1"])
    
    return results


def main():
    log.info("=" * 60)
    log.info("Benchmark 2: Embeddings + LR/SVM")
    log.info("=" * 60)
    
    train = load_split("train")
    val = load_split("val")
    test = load_split("test")
    
    train_y = [ex["label"] for ex in train]
    test_y = [ex["label"] for ex in test]
    
    MODEL = "all-MiniLM-L6-v2"  # 384-dim, fast on CPU
    
    overall_results = {}
    
    # ── 2a. Name only ──
    log.info("\n--- Variant: Name Only ---")
    train_texts = get_name_texts(train)
    test_texts = get_name_texts(test)
    
    train_emb = generate_embeddings(train_texts, MODEL)
    test_emb = generate_embeddings(test_texts, MODEL)
    
    overall_results["name_only"] = run_variant("name_only", train_emb, train_y, test_emb, test_y)
    
    # ── 2b. Name + Context ──
    log.info("\n--- Variant: Name + Context ---")
    train_texts = get_context_texts(train)
    test_texts = get_context_texts(test)
    
    train_emb_ctx = generate_embeddings(train_texts, MODEL)
    test_emb_ctx = generate_embeddings(test_texts, MODEL)
    
    overall_results["name_context"] = run_variant("name_context", train_emb_ctx, train_y, test_emb_ctx, test_y)
    
    # ── 2c. Name + Context + Features ──
    log.info("\n--- Variant: Name + Context + Features ---")
    train_features = get_feature_vector(train)
    test_features = get_feature_vector(test)
    train_features, feat_means, feat_stds = normalize_features(train_features)
    test_features, _, _ = normalize_features(test_features, feat_means, feat_stds)
    
    train_combined = np.concatenate([train_emb_ctx, train_features], axis=1)
    test_combined = np.concatenate([test_emb_ctx, test_features], axis=1)
    
    overall_results["name_context_features"] = run_variant(
        "name_context_features", train_combined, train_y, test_combined, test_y
    )
    
    # ── 2d. Larger embedding model test ──
    log.info("\n--- Variant: Larger model (all-mpnet-base-v2) ---")
    BIG_MODEL = "all-mpnet-base-v2"  # 768-dim, slower but better
    train_texts = get_context_texts(train)
    test_texts = get_context_texts(test)
    
    train_emb_big = generate_embeddings(train_texts, BIG_MODEL)
    test_emb_big = generate_embeddings(test_texts, BIG_MODEL)
    
    train_big_combined = np.concatenate([train_emb_big, train_features], axis=1)
    test_big_combined = np.concatenate([test_emb_big, test_features], axis=1)
    
    overall_results["large_model_context_features"] = run_variant(
        "large_model_context_features", train_big_combined, train_y, test_big_combined, test_y
    )
    
    # ── Save ──
    save_results(overall_results, "results")
    
    # ── Summary ──
    log.info("\n" + "=" * 60)
    log.info("Embeddings + LR/SVM Summary")
    log.info("=" * 60)
    for variant, res in overall_results.items():
        best = res["best"]
        log.info("  %-35s  best=%-15s  macro_f1=%.4f  entity_f1=%.4f  acc=%.4f  inf=%s/s",
                 variant, res["best_method"], best["macro_f1"],
                 best["entity"]["f1"], best["accuracy"],
                 best["throughput_items_per_s"])
    
    log.info("Done.")


if __name__ == "__main__":
    main()
