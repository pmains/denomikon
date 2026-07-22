#!/usr/bin/env python3
"""
Ensemble Test: Combine best individual models to see if ensemble beats best individual.

Loads trained fastText models, re-trains XGBoost/LightGBM/SVM/LR on MiniLM embeddings,
runs ModernBERT inference from checkpoint, then tests 5 ensemble strategies.

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/ensemble_test.py \
        > data/benchmark-ensemble-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import json
import logging
import os
import sys
import time
import numpy as np
from pathlib import Path
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ensemble_test")

DATA_DIR = Path("data/benchmark")
FASTTEXT_DIR = DATA_DIR / "fasttext"
MODERNBERT_DIR = DATA_DIR / "modernbert"

FEATURE_NAMES = ['all_caps_ratio', 'ctx_after_len', 'ctx_before_len',
    'ctx_has_colon', 'ctx_has_dash', 'has_ampersand_or_and', 'has_entity_keyword',
    'has_gov_keyword', 'has_legal_suffix', 'has_middle_initial', 'has_number',
    'n_cap_words', 'n_commas', 'name_len', 'pct_cap_words', 'punct_density', 'word_count']


def load_split(name: str) -> list[dict]:
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


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


# ════════════════════════════════════════════════════════════════════
# 1. fastText predictions
# ════════════════════════════════════════════════════════════════════

def get_fasttext_predictions(test_data, model_names, label: str):
    """Load fastText models and predict on test names."""
    import fasttext
    
    predictions = {}
    
    for model_key, model_file in model_names.items():
        model_path = FASTTEXT_DIR / model_file
        if not model_path.exists():
            log.warning("  fastText model %s not found at %s, skipping", model_key, model_path)
            continue
        
        log.info("  Loading fastText %s from %s...", model_key, model_path)
        model = fasttext.load_model(str(model_path))
        
        probs = []
        binary_preds = []
        for ex in test_data:
            text = ex[label]
            # fastText predict returns (labels, probs) tuples
            lbls, confs = model.predict(text, k=1)
            pred_label = 1 if lbls[0] == '__label__1' else 0
            prob = confs[0] if pred_label == 1 else 1.0 - confs[0]
            probs.append(prob)
            binary_preds.append(pred_label)
        
        predictions[model_key] = {
            "probs": np.array(probs, dtype=np.float32),
            "binary": np.array(binary_preds, dtype=np.int32),
        }
        log.info("  fastText %s: got %d predictions", model_key, len(probs))
    
    return predictions


# ════════════════════════════════════════════════════════════════════
# 2. MiniLM embeddings
# ════════════════════════════════════════════════════════════════════

def get_minilm_embeddings(texts: list[str], cache_key: str = "test_names") -> np.ndarray:
    """Generate MiniLM embeddings, caching to disk."""
    cache_path = DATA_DIR / f"embeddings_{cache_key}.npy"
    
    if cache_path.exists():
        log.info("  Loading cached embeddings from %s", cache_path)
        return np.load(cache_path)
    
    from sentence_transformers import SentenceTransformer
    
    log.info("  Loading MiniLM-L6-v2...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    
    log.info("  Generating %d embeddings...", len(texts))
    t0 = time.time()
    embeddings = model.encode(texts, batch_size=64, show_progress_bar=True,
                              normalize_embeddings=True)
    elapsed = time.time() - t0
    log.info("  Generated %d embeddings in %.1fs (%.1f/s)",
             len(embeddings), elapsed, len(embeddings) / elapsed)
    
    np.save(cache_path, embeddings)
    log.info("  Cached to %s", cache_path)
    
    return np.array(embeddings, dtype=np.float32)


# ════════════════════════════════════════════════════════════════════
# 3. Re-train XGBoost, LightGBM, SVM, LR
# ════════════════════════════════════════════════════════════════════

def train_sklearn_classifiers(train_X, train_y, test_X):
    """Train and predict with LR, LinearSVM, CalibratedSVM."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.svm import LinearSVC
    from sklearn.calibration import CalibratedClassifierCV
    
    results = {}
    
    # Logistic Regression
    log.info("  Training LR...")
    t0 = time.time()
    lr = LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced", random_state=42)
    lr.fit(train_X, train_y)
    lr_probs = lr.predict_proba(test_X)[:, 1]
    lr_preds = lr.predict(test_X)
    results["lr"] = {"probs": lr_probs, "binary": lr_preds, "train_time": time.time() - t0}
    log.info("    LR: train_time=%.2fs", results["lr"]["train_time"])
    
    # Linear SVM
    log.info("  Training LinearSVM...")
    t0 = time.time()
    svm = LinearSVC(max_iter=2000, C=1.0, class_weight="balanced", random_state=42, dual="auto")
    svm.fit(train_X, train_y)
    svm_preds = svm.predict(test_X)
    # Approximate probabilities via Platt scaling
    svm_decis = svm.decision_function(test_X)
    svm_probs = 1.0 / (1.0 + np.exp(-svm_decis))
    results["svm"] = {"probs": svm_probs, "binary": svm_preds, "train_time": time.time() - t0}
    log.info("    SVM: train_time=%.2fs", results["svm"]["train_time"])
    
    # Calibrated SVM
    log.info("  Training CalibratedSVM...")
    t0 = time.time()
    cal_svm = CalibratedClassifierCV(svm, cv=3)
    cal_svm.fit(train_X, train_y)
    cal_probs = cal_svm.predict_proba(test_X)[:, 1]
    cal_preds = cal_svm.predict(test_X)
    results["calibrated_svm"] = {"probs": cal_probs, "binary": cal_preds, "train_time": time.time() - t0}
    log.info("    CalSVM: train_time=%.2fs", results["calibrated_svm"]["train_time"])
    
    return results


def train_tree_classifiers(train_X, train_y, test_X):
    """Train and predict with XGBoost and LightGBM."""
    import xgboost as xgb
    import lightgbm as lgb
    
    results = {}
    
    n_neg = sum(1 for y in train_y if y == 0)
    n_pos = sum(1 for y in train_y if y == 1)
    scale = n_neg / max(1, n_pos)
    
    # XGBoost
    log.info("  Training XGBoost (scale_pos_weight=%.2f)...", scale)
    t0 = time.time()
    xgb_model = xgb.XGBClassifier(
        n_estimators=200, max_depth=6, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8, n_jobs=1,
        scale_pos_weight=scale, random_state=42,
        eval_metric="logloss", verbosity=0,
    )
    xgb_model.fit(train_X, train_y)
    xgb_probs = xgb_model.predict_proba(test_X)[:, 1]
    xgb_preds = xgb_model.predict(test_X)
    results["xgboost"] = {"probs": xgb_probs, "binary": xgb_preds, "model": xgb_model, "train_time": time.time() - t0}
    log.info("    XGBoost: train_time=%.2fs", results["xgboost"]["train_time"])
    
    # LightGBM
    log.info("  Training LightGBM...")
    t0 = time.time()
    lgb_model = lgb.LGBMClassifier(
        n_estimators=200, max_depth=8, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.8,
        class_weight="balanced", random_state=42, verbosity=-1,
    )
    lgb_model.fit(train_X, train_y)
    lgb_probs = lgb_model.predict_proba(test_X)[:, 1]
    lgb_preds = lgb_model.predict(test_X)
    results["lightgbm"] = {"probs": lgb_probs, "binary": lgb_preds, "model": lgb_model, "train_time": time.time() - t0}
    log.info("    LightGBM: train_time=%.2fs", results["lightgbm"]["train_time"])
    
    return results


# ════════════════════════════════════════════════════════════════════
# 4. ModernBERT inference
# ════════════════════════════════════════════════════════════════════

def get_modernbert_predictions(test_texts, checkpoint_dir, cache_key="modernbert_test"):
    """Run inference with saved ModernBERT checkpoint."""
    cache_path = DATA_DIR / f"preds_{cache_key}.npy"
    
    if cache_path.exists():
        log.info("  Loading cached ModernBERT predictions from %s", cache_path)
        cached = np.load(cache_path, allow_pickle=True).item()
        return cached
    
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    import torch
    from torch.utils.data import DataLoader, Dataset as TorchDataset
    
    checkpoint = MODERNBERT_DIR / checkpoint_dir
    if not (checkpoint / "model.safetensors").exists():
        log.warning("  ModernBERT checkpoint %s not found, skipping", checkpoint)
        return None
    
    log.info("  Loading ModernBERT from %s...", checkpoint)
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(str(checkpoint))
    model = AutoModelForSequenceClassification.from_pretrained(str(checkpoint))
    load_time = time.time() - t0
    log.info("  ModernBERT loaded in %.1fs", load_time)
    
    # Move to MPS if available
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)
    model.eval()
    log.info("  Using device: %s", device)
    
    # Tokenize
    log.info("  Tokenizing %d texts...", len(test_texts))
    encodings = tokenizer(test_texts, truncation=True, padding=True, 
                          max_length=128, return_tensors="pt")
    
    class SimpleDataset(TorchDataset):
        def __init__(self, encodings):
            self.encodings = encodings
        def __len__(self):
            return len(self.encodings["input_ids"])
        def __getitem__(self, idx):
            return {k: v[idx] for k, v in self.encodings.items()}
    
    dataset = SimpleDataset(encodings)
    loader = DataLoader(dataset, batch_size=32, shuffle=False)
    
    all_probs = []
    log.info("  Running inference...")
    t0 = time.time()
    with torch.no_grad():
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            outputs = model(**batch)
            probs = torch.softmax(outputs.logits, dim=-1)[:, 1].cpu().numpy()
            all_probs.extend(probs.tolist())
    
    infer_time = time.time() - t0
    log.info("  Inference complete: %d predictions in %.1fs (%.1f/s)",
             len(all_probs), infer_time, len(all_probs) / infer_time if infer_time > 0 else 0)
    
    probs = np.array(all_probs, dtype=np.float32)
    binary = (probs >= 0.5).astype(np.int32)
    
    result = {"probs": probs, "binary": binary, "infer_time": infer_time}
    np.save(cache_path, result)
    log.info("  Cached predictions to %s", cache_path)
    
    return result


# ════════════════════════════════════════════════════════════════════
# 5. Ensemble strategies
# ════════════════════════════════════════════════════════════════════

def ensemble_simple_average(model_predictions, y_true, names):
    """Simple average of probabilities, threshold at 0.5."""
    all_probs = np.column_stack([model_predictions[n]["probs"] for n in names])
    avg_probs = all_probs.mean(axis=1)
    preds = (avg_probs >= 0.5).astype(np.int32)
    return preds, avg_probs


def ensemble_weighted_average(model_predictions, weights_dict, y_true, names):
    """Weighted average by each model's individual F1 score."""
    all_probs = np.column_stack([model_predictions[n]["probs"] for n in names])
    weights = np.array([weights_dict.get(n, 1.0) for n in names])
    weights = weights / weights.sum()
    weighted_probs = np.average(all_probs, axis=1, weights=weights)
    preds = (weighted_probs >= 0.5).astype(np.int32)
    return preds, weighted_probs


def ensemble_majority_vote(model_predictions, y_true, names):
    """Hard voting: binary predictions, majority wins."""
    all_binary = np.column_stack([model_predictions[n]["binary"] for n in names])
    # Majority: > half
    n_models = len(names)
    threshold = n_models / 2
    preds = (all_binary.sum(axis=1) > threshold).astype(np.int32)
    # For tie-breaking, use probability avg
    return preds, None


def ensemble_stacking(train_probs_dict, train_y, test_probs_dict, names):
    """Train a logistic regression on model probabilities."""
    from sklearn.linear_model import LogisticRegression
    
    train_X = np.column_stack([train_probs_dict[n] for n in names])
    test_X = np.column_stack([test_probs_dict[n] for n in names])
    
    meta_clf = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)
    meta_clf.fit(train_X, train_y)
    preds = meta_clf.predict(test_X)
    probs = meta_clf.predict_proba(test_X)[:, 1]
    return preds, probs


def ensemble_best_pair(model_predictions, y_true, names, pair):
    """Try combining just 2 specific models."""
    return ensemble_simple_average(model_predictions, y_true, [pair[0], pair[1]])


# ════════════════════════════════════════════════════════════════════
# 6. Error analysis
# ════════════════════════════════════════════════════════════════════

def error_analysis(y_true, model_preds, ensemble_preds, model_names, test_data):
    """Analyze error overlap between ensemble and individual models."""
    
    # Per-model error indices
    model_errors = {}
    for name in model_names:
        incorrect = np.where(model_preds[name]["binary"] != np.array(y_true))[0]
        model_errors[name] = set(incorrect.tolist())
    
    ensemble_incorrect = set(np.where(ensemble_preds != np.array(y_true))[0].tolist())
    
    # How many errors does ensemble catch that each individual model missed?
    rescued = {}
    for name in model_names:
        # Errors the individual model made that the ensemble gets right
        individual_wrong = model_errors[name]
        rescued[name] = individual_wrong - ensemble_incorrect
    
    # FN overlap: which entity examples (y_true=1) do models miss?
    entity_indices = [i for i, y in enumerate(y_true) if y == 1]
    fn_by_model = {}
    for name in model_names:
        fn_indices = set(i for i in entity_indices if model_preds[name]["binary"][i] == 0)
        fn_by_model[name] = fn_indices
    
    # Pairwise overlap
    fn_overlap = {}
    model_list = sorted(model_names)
    for i, m1 in enumerate(model_list):
        for j, m2 in enumerate(model_list):
            if i >= j:
                continue
            overlap = fn_by_model[m1] & fn_by_model[m2]
            fn_overlap[f"{m1}_vs_{m2}"] = {
                "m1_only": len(fn_by_model[m1] - fn_by_model[m2]),
                "m2_only": len(fn_by_model[m2] - fn_by_model[m1]),
                "both": len(overlap),
                "total_m1": len(fn_by_model[m1]),
                "total_m2": len(fn_by_model[m2]),
            }
    
    # Find examples where ensemble is correct but best model is wrong
    best_model = max(model_names, key=lambda n: len(rescued.get(n, set())))
    rescued_indices = sorted(rescued[best_model])[:5]
    rescued_examples = []
    for idx in rescued_indices:
        ex = test_data[idx]
        model_pred = model_preds[best_model]["binary"][idx]
        rescued_examples.append({
            "name": ex["name"],
            "label": ex["label"],
            "model_prediction": int(model_pred),
            "ensemble_prediction": int(ensemble_preds[idx]),
            "context": (ex.get("context", "")[:100] if ex.get("context") else ""),
        })
    
    analysis = {
        "total_errors_ensemble": len(ensemble_incorrect),
        "error_counts": {n: len(e) for n, e in model_errors.items()},
        "rescued_from": {n: len(s) for n, s in rescued.items()},
        "error_overlap": {
            "ensemble_unique_errors": len(ensemble_incorrect - set.union(*model_errors.values())),
            "errors_caught_by_all": len(set.intersection(*model_errors.values())),
        },
        "fn_overlap": fn_overlap,
        "fn_by_model": {n: len(s) for n, s in fn_by_model.items()},
        "rescued_examples": rescued_examples,
    }
    
    return analysis


# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("ENSEMBLE TEST — Combining Best Classifiers")
    log.info("=" * 60)
    
    # Load data
    train = load_split("train")
    test = load_split("test")
    train_y = [ex["label"] for ex in train]
    test_y = [ex["label"] for ex in test]
    y_true = np.array(test_y)
    
    log.info("Train: %d examples (%d entity, %d noise)",
             len(train), sum(train_y), len(train_y) - sum(train_y))
    log.info("Test: %d examples (%d entity, %d noise)",
             len(test), sum(test_y), len(test_y) - sum(test_y))
    
    # ── Step 1: fastText predictions ──
    log.info("\n" + "─" * 60)
    log.info("STEP 1: fastText Predictions")
    log.info("─" * 60)
    
    ft_test_names = [ex['name'].replace('\n', ' ').replace('\r', ' ').strip() for ex in test]
    ft_test_ctx_feat = [
        (f"{ex['name'].replace(chr(10), ' ').replace(chr(13), ' ')} [CTX] "
         f"{(ex.get('context_before', '') or '').replace(chr(10), ' ')} "
         f"{(ex.get('context_after', '') or '').replace(chr(10), ' ')} "
         + " ".join(f"[{k}={v}]" for k, v in ex.get("features", {}).items()))
        for ex in test
    ]
    
    ft_models = {
        "fasttext_name_only": ft_test_names,
        "fasttext_name_context": ft_test_ctx_feat,  # loaded differently
    }
    
    # Load fastText models and predict
    import fasttext
    
    ft_predictions = {}
    
    # Model: name-only
    ft_path = FASTTEXT_DIR / "model_name_only.bin"
    if ft_path.exists():
        log.info("  Loading fastText name-only from %s", ft_path)
        ft_model = fasttext.load_model(str(ft_path))
        probs = []
        binary = []
        for text in ft_test_names:
            clean = text.replace('\n', ' ').replace('\r', ' ').strip()
            lbls, confs = ft_model.predict(clean, k=1)
            p = confs[0] if lbls[0] == '__label__1' else 1.0 - confs[0] if lbls[0] == '__label__0' else 0.5
            probs.append(p)
            binary.append(1 if lbls[0] == '__label__1' else 0)
        ft_predictions["fasttext_name_only"] = {
            "probs": np.array(probs, dtype=np.float32),
            "binary": np.array(binary, dtype=np.int32),
        }
        log.info("  fastText name-only: %d predictions", len(probs))
    
    # Model: name+context+features
    ft_path = FASTTEXT_DIR / "model_name_context_features.bin"
    if ft_path.exists():
        log.info("  Loading fastText name+ctx+feat from %s", ft_path)
        ft_model = fasttext.load_model(str(ft_path))
        probs = []
        binary = []
        for text in ft_test_ctx_feat:
            clean = text.replace('\n', ' ').replace('\r', ' ').strip()
            lbls, confs = ft_model.predict(clean, k=1)
            p = confs[0] if lbls[0] == '__label__1' else 1.0 - confs[0] if lbls[0] == '__label__0' else 0.5
            probs.append(p)
            binary.append(1 if lbls[0] == '__label__1' else 0)
        ft_predictions["fasttext_name_context_features"] = {
            "probs": np.array(probs, dtype=np.float32),
            "binary": np.array(binary, dtype=np.int32),
        }
        log.info("  fastText name+ctx+feat: %d predictions", len(probs))
    
    # ── Step 2: MiniLM embeddings ──
    log.info("\n" + "─" * 60)
    log.info("STEP 2: MiniLM Embeddings")
    log.info("─" * 60)
    
    train_names = [ex["name"] for ex in train]
    test_names = [ex["name"] for ex in test]
    
    train_emb = get_minilm_embeddings(train_names, "train_names")
    test_emb = get_minilm_embeddings(test_names, "test_names")
    
    # ── Step 3: Re-train classifiers on MiniLM embeddings ──
    log.info("\n" + "─" * 60)
    log.info("STEP 3: Re-train XGBoost/LightGBM/SVM/LR")
    log.info("─" * 60)
    
    # Also need train probs for stacking meta-classifier
    ml_predictions = {}
    train_probs = {}
    
    # Scikit-learn classifiers
    sk_results = train_sklearn_classifiers(train_emb, train_y, test_emb)
    for name, res in sk_results.items():
        ml_predictions[f"minilm_{name}"] = res
    
    # Tree classifiers (need train probs too for stacking)
    tree_results = train_tree_classifiers(train_emb, train_y, test_emb)
    for name, res in tree_results.items():
        model = res.pop("model")
        ml_predictions[f"minilm_{name}"] = res
        # Get train probabilities for stacking
        train_probs[f"minilm_{name}"] = model.predict_proba(train_emb)[:, 1]
    
    # Get train probs for LR and calibrated SVM
    from sklearn.linear_model import LogisticRegression as LR2
    from sklearn.calibration import CalibratedClassifierCV as CalSVM
    from sklearn.svm import LinearSVC as LSVM
    
    # LR train probs
    lr_cv = LR2(max_iter=1000, C=1.0, class_weight="balanced", random_state=42)
    lr_cv.fit(train_emb, train_y)
    train_probs["minilm_lr"] = lr_cv.predict_proba(train_emb)[:, 1]
    
    # Cal SVM train probs
    svm_base = LSVM(max_iter=2000, C=1.0, class_weight="balanced", random_state=42, dual="auto")
    cal_svm = CalSVM(svm_base, cv=3)
    cal_svm.fit(train_emb, train_y)
    train_probs["minilm_calibrated_svm"] = cal_svm.predict_proba(train_emb)[:, 1]
    
    # SVM train probs (Platt scaling)
    svm_base.fit(train_emb, train_y)
    svm_decis_train = svm_base.decision_function(train_emb)
    train_probs["minilm_svm"] = 1.0 / (1.0 + np.exp(-svm_decis_train))
    
    # ── Step 4: ModernBERT inference ──
    log.info("\n" + "─" * 60)
    log.info("STEP 4: ModernBERT Inference")
    log.info("─" * 60)
    
    mb_preds = get_modernbert_predictions(test_names, "model_name_only/checkpoint-212")
    if mb_preds is not None:
        ml_predictions["modernbert"] = mb_preds
        # Need train probs for ModernBERT too
        mb_train_preds = get_modernbert_predictions(train_names, "model_name_only/checkpoint-212", 
                                                     "modernbert_train")
        if mb_train_preds is not None:
            train_probs["modernbert"] = mb_train_preds["probs"]
    
    # Combine all model predictions for fastText + ML models
    all_predictions = {}
    all_predictions.update(ft_predictions)
    all_predictions.update(ml_predictions)
    
    # ── Calculate individual model metrics on test set ──
    log.info("\n" + "─" * 60)
    log.info("INDIVIDUAL MODEL METRICS (on test set)")
    log.info("─" * 60)
    
    individual_metrics = {}
    for name in sorted(all_predictions.keys()):
        preds = all_predictions[name]["binary"]
        metrics = compute_metrics(test_y, preds.tolist())
        individual_metrics[name] = metrics
        log.info("  %-35s  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f",
                 name, metrics["macro_f1"], metrics["entity"]["f1"],
                 metrics["noise"]["f1"], metrics["accuracy"])
    
    # ── Pick top models for ensemble ──
    # Sort by macro F1
    ranked = sorted(individual_metrics.items(), key=lambda x: x[1]["macro_f1"], reverse=True)
    log.info("\nTop models by macro F1:")
    for i, (name, m) in enumerate(ranked):
        log.info("  %d. %-35s  macro_f1=%.4f", i+1, name, m["macro_f1"])
    
    # Use all available models for the "all" ensemble
    all_model_names = sorted(all_predictions.keys())
    log.info("\nAll models for ensemble: %s", ", ".join(all_model_names))
    
    # ── Step 5: Ensemble strategies ──
    log.info("\n" + "─" * 60)
    log.info("STEP 5: Ensemble Strategies")
    log.info("─" * 60)
    
    ensemble_results = {}
    
    # a) Simple average (all models)
    log.info("\n--- a) Simple Average (soft voting, all models) ---")
    avg_preds, avg_probs = ensemble_simple_average(all_predictions, test_y, all_model_names)
    ensemble_results["simple_average_all"] = compute_metrics(test_y, avg_preds.tolist())
    log.info("  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f",
             ensemble_results["simple_average_all"]["macro_f1"],
             ensemble_results["simple_average_all"]["entity"]["f1"],
             ensemble_results["simple_average_all"]["noise"]["f1"],
             ensemble_results["simple_average_all"]["accuracy"])
    
    # b) Weighted average
    log.info("\n--- b) Weighted Average (by macro F1) ---")
    f1_weights = {n: individual_metrics[n]["macro_f1"] for n in all_model_names}
    wavg_preds, wavg_probs = ensemble_weighted_average(all_predictions, f1_weights, test_y, all_model_names)
    ensemble_results["weighted_average_all"] = compute_metrics(test_y, wavg_preds.tolist())
    log.info("  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f",
             ensemble_results["weighted_average_all"]["macro_f1"],
             ensemble_results["weighted_average_all"]["entity"]["f1"],
             ensemble_results["weighted_average_all"]["noise"]["f1"],
             ensemble_results["weighted_average_all"]["accuracy"])
    
    # c) Majority vote
    log.info("\n--- c) Majority Vote (hard voting, all models) ---")
    maj_preds, _ = ensemble_majority_vote(all_predictions, test_y, all_model_names)
    ensemble_results["majority_vote_all"] = compute_metrics(test_y, maj_preds.tolist())
    log.info("  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f",
             ensemble_results["majority_vote_all"]["macro_f1"],
             ensemble_results["majority_vote_all"]["entity"]["f1"],
             ensemble_results["majority_vote_all"]["noise"]["f1"],
             ensemble_results["majority_vote_all"]["accuracy"])
    
    # d) Stacking
    log.info("\n--- d) Stacking (LR meta-classifier) ---")
    # Only use models for which we have both train and test probs
    stackable = [n for n in all_model_names if n in train_probs]
    if len(stackable) >= 2:
        log.info("  Stackable models: %s", ", ".join(stackable))
        stack_preds, stack_probs = ensemble_stacking(
            {n: train_probs[n] for n in stackable}, train_y,
            {n: all_predictions[n]["probs"] for n in stackable}, stackable
        )
        ensemble_results["stacking"] = compute_metrics(test_y, stack_preds.tolist())
        log.info("  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f",
                 ensemble_results["stacking"]["macro_f1"],
                 ensemble_results["stacking"]["entity"]["f1"],
                 ensemble_results["stacking"]["noise"]["f1"],
                 ensemble_results["stacking"]["accuracy"])
    else:
        log.warning("  Not enough models with train probs for stacking!")
    
    # e) Best pairs
    log.info("\n--- e) Best Pairs ---")
    # Use top models (filtered to ones with meaningful diversity)
    top3 = [n for n, _ in ranked[:3] if n in all_predictions]
    log.info("  Top 3 models for pairs: %s", top3)
    
    for i in range(len(top3)):
        for j in range(i+1, len(top3)):
            pair = (top3[i], top3[j])
            pair_preds, _ = ensemble_best_pair(all_predictions, test_y, top3, pair)
            name = f"pair_{top3[i]}_{top3[j]}"
            ensemble_results[name] = compute_metrics(test_y, pair_preds.tolist())
            log.info("  %s vs %s: macro_f1=%.4f",
                     top3[i], top3[j], ensemble_results[name]["macro_f1"])
    
    # Also try top 3 together
    if len(top3) >= 3:
        top3_preds, _ = ensemble_simple_average(all_predictions, test_y, top3)
        ensemble_results["simple_average_top3"] = compute_metrics(test_y, top3_preds.tolist())
        log.info("  top3 avg: macro_f1=%.4f", ensemble_results["simple_average_top3"]["macro_f1"])
        
        top3_wavg, _ = ensemble_weighted_average(
            all_predictions, 
            {n: individual_metrics[n]["macro_f1"] for n in top3}, 
            test_y, top3
        )
        ensemble_results["weighted_average_top3"] = compute_metrics(test_y, top3_wavg.tolist())
        log.info("  top3 weighted: macro_f1=%.4f", ensemble_results["weighted_average_top3"]["macro_f1"])
        
        top3_maj, _ = ensemble_majority_vote(all_predictions, test_y, top3)
        ensemble_results["majority_vote_top3"] = compute_metrics(test_y, top3_maj.tolist())
        log.info("  top3 majority: macro_f1=%.4f", ensemble_results["majority_vote_top3"]["macro_f1"])
    
    # ── Step 6: Error Analysis ──
    log.info("\n" + "─" * 60)
    log.info("STEP 6: Error Analysis")
    log.info("─" * 60)
    
    # Find best ensemble
    best_ens_name = max(ensemble_results, key=lambda k: ensemble_results[k]["macro_f1"])
    best_ens_metrics = ensemble_results[best_ens_name]
    log.info("Best ensemble: %s (macro_f1=%.4f)", best_ens_name, best_ens_metrics["macro_f1"])
    
    # Get best ensemble predictions for analysis
    best_ens_preds = None
    for name_key, preds_var in [("simple_average_all", avg_preds), 
                                  ("weighted_average_all", wavg_preds),
                                  ("majority_vote_all", maj_preds)]:
        if name_key == best_ens_name:
            best_ens_preds = preds_var
            break
    if best_ens_preds is None and "stacking" == best_ens_name:
        best_ens_preds = stack_preds
    
    if best_ens_preds is not None:
        analysis = error_analysis(test_y, all_predictions, best_ens_preds, 
                                  all_model_names, test)
        
        log.info("\nError overlap analysis:")
        log.info("  Ensemble total errors: %d", analysis["total_errors_ensemble"])
        for n, c in sorted(analysis["error_counts"].items(), key=lambda x: -x[1]):
            log.info("  %-35s: %d errors", n, c)
        
        log.info("\nErrors rescued by ensemble (individual model wrong, ensemble right):")
        for n, c in sorted(analysis["rescued_from"].items(), key=lambda x: -x[1]):
            log.info("  From %-35s: %d rescued", n, c)
        
        # Find examples where ensemble is correct but best model is wrong
        best_indiv = max(individual_metrics, key=lambda n: individual_metrics[n]["macro_f1"])
        log.info("\nBest individual model: %s (macro_f1=%.4f)", 
                 best_indiv, individual_metrics[best_indiv]["macro_f1"])
        
        best_indiv_preds = all_predictions[best_indiv]["binary"]
        best_indiv_wrong = np.where(best_indiv_preds != y_true)[0]
        ens_correct_on_best_wrong = np.where(
            (best_indiv_preds != y_true) & (best_ens_preds == y_true)
        )[0]
        
        log.info("Examples where ensemble correct, best individual wrong: %d",
                 len(ens_correct_on_best_wrong))
        for i, idx in enumerate(ens_correct_on_best_wrong[:5]):
            ex = test[idx]
            log.info("  %d. '%s' (label=%d) — best=%d, ensemble=%d",
                     i+1, ex["name"], ex["label"], 
                     int(best_indiv_preds[idx]), int(best_ens_preds[idx]))
        
        # FN overlap across top models
        log.info("\nFalse Negative overlap (entity examples missed):")
        for n, c in sorted(analysis["fn_by_model"].items(), key=lambda x: -x[1]):
            log.info("  %-35s: %d FN", n, c)
        
        if "fn_overlap" in analysis:
            for pair, stats in sorted(analysis["fn_overlap"].items()):
                log.info("  %s: both=%d, m1_only=%d, m2_only=%d",
                         pair, stats["both"], stats["m1_only"], stats["m2_only"])
    else:
        analysis = None
    
    # ── Save results ──
    log.info("\n" + "─" * 60)
    log.info("SAVING RESULTS")
    log.info("─" * 60)
    
    # Convert numpy arrays for JSON serialization
    ens_results_serializable = {}
    for k, v in ensemble_results.items():
        ens_results_serializable[k] = v
    
    indiv_metrics_serializable = {}
    for k, v in individual_metrics.items():
        indiv_metrics_serializable[k] = v
    
    output = {
        "individual_metrics": indiv_metrics_serializable,
        "ensemble_results": ens_results_serializable,
        "best_ensemble": best_ens_name,
        "best_ensemble_metrics": best_ens_metrics,
        "models_used": all_model_names,
    }
    
    if analysis:
        output["error_analysis"] = {
            "total_errors_ensemble": analysis["total_errors_ensemble"],
            "error_counts": analysis["error_counts"],
            "rescued_from": analysis["rescued_from"],
            "error_overlap": analysis["error_overlap"],
            "fn_by_model": analysis["fn_by_model"],
            "fn_overlap": analysis.get("fn_overlap", {}),
            "rescued_examples": analysis.get("rescued_examples", []),
        }
    
    out_path = DATA_DIR / "ensemble_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    log.info("Results saved to %s", out_path)
    
    # ── Topline comparison ──
    log.info("\n" + "=" * 60)
    log.info("TOPLINE COMPARISON")
    log.info("=" * 60)
    log.info("%-40s  %10s  %10s  %10s  %10s",
             "Model/Variant", "Macro F1", "Entity F1", "Noise F1", "Accuracy")
    log.info("-" * 80)
    
    # Best individual
    best_indiv = max(individual_metrics, key=lambda n: individual_metrics[n]["macro_f1"])
    log.info("%-40s  %10.4f  %10.4f  %10.4f  %10.4f",
             f"Best Individual: {best_indiv}",
             individual_metrics[best_indiv]["macro_f1"],
             individual_metrics[best_indiv]["entity"]["f1"],
             individual_metrics[best_indiv]["noise"]["f1"],
             individual_metrics[best_indiv]["accuracy"])
    
    for name, m in sorted(ensemble_results.items(), key=lambda x: -x[1]["macro_f1"]):
        log.info("%-40s  %10.4f  %10.4f  %10.4f  %10.4f",
                 f"Ensemble: {name}",
                 m["macro_f1"], m["entity"]["f1"], m["noise"]["f1"], m["accuracy"])
    
    log.info("\nDone.")


if __name__ == "__main__":
    main()
