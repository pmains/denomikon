#!/usr/bin/env python3
"""
Benchmark 4: SetFit — contrastive fine-tuning + classification head.

SetFit is designed for small labeled datasets. Uses a Sentence Transformer
backbone with contrastive fine-tuning followed by a lightweight classifier.

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/04_setfit.py \
        > data/benchmark-04-$(date +%Y%m%d-%H%M).log 2>&1 &
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
log = logging.getLogger("benchmark_setfit")

DATA_DIR = Path("data/benchmark")
BENCHMARK_DIR = DATA_DIR / "setfit"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)


def load_split(name: str) -> list[dict]:
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


def save_results(results: dict, name: str):
    path = BENCHMARK_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info("Saved results to %s", path)


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


def run_setfit(train_texts, train_y, val_texts, val_y, test_texts, test_y,
               name: str, num_epochs: int = 3) -> dict:
    """Train a SetFit model and evaluate."""
    from setfit import SetFitModel, SetFitTrainer
    from datasets import Dataset
    
    log.info("  Loading SetFit model (all-MiniLM-L6-v2)...")
    t0 = time.time()
    model = SetFitModel.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
    
    train_ds = Dataset.from_dict({"text": train_texts, "label": train_y})
    val_ds = Dataset.from_dict({"text": val_texts, "label": val_y})
    
    trainer = SetFitTrainer(
        model=model,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        column_mapping={"text": "text", "label": "label"},
        num_iterations=5,
        num_epochs=min(num_epochs, 3),  # 3 epochs for speed
        learning_rate=2e-5,
        batch_size=32,
        seed=42,
    )
    
    log.info("  Training SetFit (%d epochs)...", num_epochs)
    trainer.train()
    train_time = time.time() - t0
    
    # Evaluate
    log.info("  Evaluating...")
    pred = model(test_texts)
    
    metrics = compute_metrics(test_y, pred.tolist())
    metrics["train_time_s"] = round(train_time, 2)
    
    # Throughput
    t0 = time.time()
    model(test_texts[:50])
    infer_time = time.time() - t0
    metrics["throughput_items_per_s"] = round(50 / infer_time, 1) if infer_time > 0 else 0
    
    metrics["model_size_mb"] = "~90"
    
    log.info("  SetFit %s: macro_f1=%.4f, entity_f1=%.4f, noise_f1=%.4f, train=%.1fs, model=%.1fMB",
             name, metrics["macro_f1"], metrics["entity"]["f1"],
             metrics["noise"]["f1"], train_time, metrics["model_size_mb"])
    
    return metrics


def main():
    log.info("=" * 60)
    log.info("Benchmark 4: SetFit")
    log.info("=" * 60)
    
    train = load_split("train")
    val = load_split("val")
    test = load_split("test")
    
    train_y = [ex["label"] for ex in train]
    val_y = [ex["label"] for ex in val]
    test_y = [ex["label"] for ex in test]
    
    results = {}
    
    # ── 4a. Name only ──
    log.info("\n--- Variant: Name Only ---")
    train_texts = [ex["name"] for ex in train]
    val_texts = [ex["name"] for ex in val]
    test_texts = [ex["name"] for ex in test]
    
    results["name_only"] = run_setfit(
        train_texts, train_y, val_texts, val_y, test_texts, test_y,
        "name_only", num_epochs=5
    )
    
    # ── 4b. Name + Context ──
    log.info("\n--- Variant: Name + Context ---")
    train_texts = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}" for ex in train]
    val_texts = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}" for ex in val]
    test_texts = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}" for ex in test]
    
    results["name_context"] = run_setfit(
        train_texts, train_y, val_texts, val_y, test_texts, test_y,
        "name_context", num_epochs=5
    )
    
    # ── Save ──
    save_results(results, "results")
    
    log.info("\n" + "=" * 60)
    log.info("SetFit Summary")
    log.info("=" * 60)
    for variant, metrics in results.items():
        log.info("  %-30s  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f  train=%.1fs  inf=%s/s",
                 variant, metrics["macro_f1"], metrics["entity"]["f1"],
                 metrics["noise"]["f1"], metrics["accuracy"],
                 metrics["train_time_s"], metrics["throughput_items_per_s"])
    
    log.info("Done.")


if __name__ == "__main__":
    main()
