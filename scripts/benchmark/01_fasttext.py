#!/usr/bin/env python3
"""
Benchmark 1: fastText — lightweight lexical baseline for entity classification.

Runs on name-only, name+context, and name+context+features.

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/01_fasttext.py \
        > data/benchmark-01-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import json
import logging
import os
import sys
import time
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("benchmark_fasttext")

DATA_DIR = Path("data/benchmark")
BENCHMARK_DIR = DATA_DIR / "fasttext"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

# ── Load splits ─────────────────────────────────────────────────────

def load_split(name: str) -> list[dict]:
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


def save_results(results: dict, name: str):
    path = BENCHMARK_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("Saved results to %s", path)


# ── Format text for fastText ────────────────────────────────────────

def format_name_only(examples: list[dict]) -> list[str]:
    """Format as 'name' only."""
    return [ex["name"] for ex in examples]


def format_name_context(examples: list[dict]) -> list[str]:
    """Format as 'name [SEP] context_before [SEP] context_after'."""
    texts = []
    for ex in examples:
        ctx = f"{ex.get('context_before', '')} {ex.get('context_after', '')}".strip()
        texts.append(f"{ex['name']} [CTX] {ctx}")
    return texts


# ── Write fastText training file ────────────────────────────────────

def write_fasttext_file(texts: list[str], labels: list[int], path: str):
    """Write a fastText-format file: __label__X text."""
    with open(path, "w") as f:
        for text, label in zip(texts, labels):
            ft_label = "__label__1" if label == 1 else "__label__0"
            # fastText expects space-separated tokens; clean up
            clean = text.replace("\n", " ").replace("\r", " ").strip()
            f.write(f"{ft_label} {clean}\n")


# ── Compute features text ───────────────────────────────────────────

def feature_text(ex: dict) -> str:
    """Return a text representation of features."""
    feats = ex.get("features", {})
    parts = []
    for k, v in sorted(feats.items()):
        parts.append(f"[{k}={v}]")
    return " ".join(parts)


def format_name_context_features(examples: list[dict]) -> list[str]:
    texts = []
    for ex in examples:
        ctx = f"{ex.get('context_before', '')} {ex.get('context_after', '')}".strip()
        feats = feature_text(ex)
        texts.append(f"{ex['name']} [CTX] {ctx} {feats}")
    return texts


# ── Evaluate ────────────────────────────────────────────────────────

def evaluate(model_path: str, test_texts: list[str], test_labels: list[int]) -> dict:
    """Evaluate a fastText model on test data."""
    import fasttext
    
    model = fasttext.load_model(model_path)
    
    predictions = []
    for text in test_texts:
        clean = text.replace("\n", " ").replace("\r", " ").strip()
        pred_label, probs = model.predict(clean, k=2)
        pred = 1 if pred_label[0] == "__label__1" else 0
        predictions.append(pred)
    
    # Compute metrics
    tp = sum(1 for p, l in zip(predictions, test_labels) if p == 1 and l == 1)
    fp = sum(1 for p, l in zip(predictions, test_labels) if p == 1 and l == 0)
    tn = sum(1 for p, l in zip(predictions, test_labels) if p == 0 and l == 0)
    fn = sum(1 for p, l in zip(predictions, test_labels) if p == 0 and l == 1)
    
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
        "entity": {
            "precision": round(precision_1, 4),
            "recall": round(recall_1, 4),
            "f1": round(f1_1, 4),
        },
        "noise": {
            "precision": round(precision_0, 4),
            "recall": round(recall_0, 4),
            "f1": round(f1_0, 4),
        },
        "confusion_matrix": {
            "tp": tp, "fp": fp, "tn": tn, "fn": fn
        },
    }


def run_benchmark_variant(name: str, train_texts, train_labels, test_texts, test_labels,
                          epoch: int = 50, lr: float = 0.5, dim: int = 100, wordNgrams: int = 3):
    """Train and evaluate a fastText model."""
    import fasttext
    
    train_path = str(BENCHMARK_DIR / f"train_{name}.txt")
    test_path = str(BENCHMARK_DIR / f"test_{name}.txt")
    model_path = str(BENCHMARK_DIR / f"model_{name}.bin")
    
    write_fasttext_file(train_texts, train_labels, train_path)
    write_fasttext_file(test_texts, test_labels, test_path)
    
    log.info("Training fastText %s (epoch=%d, lr=%.2f, dim=%d, wordNgrams=%d)...",
             name, epoch, lr, dim, wordNgrams)
    
    t0 = time.time()
    model = fasttext.train_supervised(
        input=train_path,
        epoch=epoch,
        lr=lr,
        dim=dim,
        wordNgrams=wordNgrams,
        verbose=0,
    )
    train_time = time.time() - t0
    model.save_model(model_path)
    
    # Evaluate
    results = evaluate(model_path, test_texts, test_labels)
    results["train_time_s"] = round(train_time, 2)
    
    # Throughput: how many items per second on CPU
    n_test = len(test_texts)
    t0 = time.time()
    for text in test_texts:
        clean = text.replace("\n", " ").replace("\r", " ").strip()
        model.predict(clean)
    infer_time = time.time() - t0
    results["throughput_items_per_s"] = round(n_test / infer_time, 1) if infer_time > 0 else 0
    
    # Model size
    results["model_size_mb"] = round(os.path.getsize(model_path) / (1024 * 1024), 2)
    
    log.info("  %s: macro_f1=%.4f, entity_f1=%.4f, noise_f1=%.4f, acc=%.4f, train=%.1fs, inf=%.1f/s, size=%.1fMB",
             name, results["macro_f1"], results["entity"]["f1"], results["noise"]["f1"],
             results["accuracy"], train_time, results["throughput_items_per_s"], results["model_size_mb"])
    
    return results


def main():
    log.info("=" * 60)
    log.info("Benchmark 1: fastText Lexical Baseline")
    log.info("=" * 60)
    
    train = load_split("train")
    val = load_split("val")
    test = load_split("test")
    
    train_labels = [ex["label"] for ex in train]
    test_labels = [ex["label"] for ex in test]
    
    results = {}
    
    # ── 1a. Name only ──
    log.info("\n--- Variant: Name Only ---")
    train_texts = format_name_only(train)
    test_texts = format_name_only(test)
    results["name_only"] = run_benchmark_variant(
        "name_only", train_texts, train_labels, test_texts, test_labels
    )
    
    # ── 1b. Name + Context ──
    log.info("\n--- Variant: Name + Context ---")
    train_texts = format_name_context(train)
    test_texts = format_name_context(test)
    results["name_context"] = run_benchmark_variant(
        "name_context", train_texts, train_labels, test_texts, test_labels
    )
    
    # ── 1c. Name + Context + Features ──
    log.info("\n--- Variant: Name + Context + Features ---")
    train_texts = format_name_context_features(train)
    test_texts = format_name_context_features(test)
    results["name_context_features"] = run_benchmark_variant(
        "name_context_features", train_texts, train_labels, test_texts, test_labels
    )
    
    # ── 1d. Hyperparameter sweep on name+context ──
    log.info("\n--- Hyperparameter sweep (name+context) ---")
    sweep_results = []
    for dim in [50, 100, 200]:
        for wordNgrams in [2, 3, 4]:
            sweep = run_benchmark_variant(
                f"sweep_d{dim}_ng{wordNgrams}",
                format_name_context(train), train_labels,
                format_name_context(test), test_labels,
                epoch=50, lr=0.5, dim=dim, wordNgrams=wordNgrams,
            )
            sweep["dim"] = dim
            sweep["wordNgrams"] = wordNgrams
            sweep_results.append(sweep)
    
    results["sweep"] = sweep_results
    # Best sweep result
    best = max(sweep_results, key=lambda r: r["macro_f1"])
    results["best_name_context"] = best
    log.info("Best sweep: dim=%d, wordNgrams=%d, macro_f1=%.4f",
             best["dim"], best["wordNgrams"], best["macro_f1"])
    
    # Save all results
    save_results(results, "results")
    
    # ── Summary ──
    log.info("\n" + "=" * 60)
    log.info("fastText Summary")
    log.info("=" * 60)
    for variant in ["name_only", "name_context", "name_context_features"]:
        r = results[variant]
        log.info("  %-30s  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f  inf=%.1f/s  size=%.1fMB",
                 variant, r["macro_f1"], r["entity"]["f1"], r["noise"]["f1"],
                 r["accuracy"], r["throughput_items_per_s"], r["model_size_mb"])
    
    log.info("Done.")


if __name__ == "__main__":
    main()
