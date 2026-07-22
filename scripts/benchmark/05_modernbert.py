#!/usr/bin/env python3
"""
Benchmark 5: ModernBERT fine-tuning for candidate classification.

Full encoder fine-tuning using ModernBERT-base on the classification dataset.

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/05_modernbert.py \
        > data/benchmark-05-$(date +%Y%m%d-%H%M).log 2>&1 &
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
log = logging.getLogger("benchmark_modernbert")

DATA_DIR = Path("data/benchmark")
BENCHMARK_DIR = DATA_DIR / "modernbert"
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


def run_modernbert(train_texts, train_y, val_texts, val_y, test_texts, test_y,
                   name: str, max_epochs: int = 4) -> dict:
    """Fine-tune ModernBERT for classification."""
    from transformers import (
        AutoTokenizer, AutoModelForSequenceClassification,
        TrainingArguments, Trainer, DataCollatorWithPadding,
        EarlyStoppingCallback
    )
    from datasets import Dataset
    
    MODEL_NAME = "answerdotai/ModernBERT-base"
    
    log.info("  Loading ModernBERT tokenizer and model...")
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, num_labels=2,
    )
    load_time = time.time() - t0
    log.info("  Model loaded in %.1fs", load_time)
    
    # Tokenize
    def tokenize_fn(examples):
        return tokenizer(examples["text"], truncation=True, padding=False, max_length=128)
    
    train_ds = Dataset.from_dict({"text": train_texts, "label": train_y})
    val_ds = Dataset.from_dict({"text": val_texts, "label": val_y})
    test_ds = Dataset.from_dict({"text": test_texts, "label": test_y})
    
    train_ds = train_ds.map(tokenize_fn, batched=True)
    val_ds = val_ds.map(tokenize_fn, batched=True)
    test_ds = test_ds.map(tokenize_fn, batched=True)
    
    data_collator = DataCollatorWithPadding(tokenizer=tokenizer)
    
    # Training args — CPU-friendly smaller config
    n_train = len(train_texts)
    batch_size = 16
    eval_steps = max(1, n_train // batch_size // 2)
    
    training_args = TrainingArguments(
        output_dir=str(BENCHMARK_DIR / f"model_{name}"),
        num_train_epochs=max_epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size * 2,
        learning_rate=3e-5,
        warmup_ratio=0.1,
        eval_strategy="steps",
        eval_steps=eval_steps,
        save_strategy="steps",
        save_steps=eval_steps,
        save_total_limit=1,
        logging_steps=eval_steps,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        report_to="none",
        dataloader_num_workers=0,
        fp16=False,
    )
    
    log.info("  Training ModernBERT (%d epochs, %d train examples)...",
             max_epochs, n_train)
    log.info("  eval_steps=%d, batch_size=%d", eval_steps, batch_size)
    
    t0 = time.time()
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        tokenizer=tokenizer,
        data_collator=data_collator,
    )
    trainer.train()
    train_time = time.time() - t0
    log.info("  Training completed in %.1fs", train_time)
    
    # Evaluate on test
    log.info("  Evaluating on test set...")
    preds = trainer.predict(test_ds)
    pred_labels = np.argmax(preds.predictions, axis=1).tolist()
    
    metrics = compute_metrics(test_y, pred_labels)
    metrics["train_time_s"] = round(train_time, 2)
    
    # Throughput
    t0 = time.time()
    trainer.predict(test_ds[:50])
    infer_time = time.time() - t0
    metrics["throughput_items_per_s"] = round(50 / infer_time, 1) if infer_time > 0 else 0
    
    # Model size
    model_path = BENCHMARK_DIR / f"model_{name}"
    if model_path.exists():
        total_size = sum(os.path.getsize(os.path.join(dp, f))
                        for dp, _, fn in os.walk(str(model_path)) for f in fn) / (1024 * 1024)
        metrics["model_size_mb"] = round(total_size, 2)
    else:
        metrics["model_size_mb"] = "~420"
    
    log.info("  ModernBERT %s: macro_f1=%.4f, entity_f1=%.4f, noise_f1=%.4f, train=%.1fs, model=%.1fMB",
             name, metrics["macro_f1"], metrics["entity"]["f1"],
             metrics["noise"]["f1"], train_time, metrics["model_size_mb"])
    
    return metrics


def main():
    log.info("=" * 60)
    log.info("Benchmark 5: ModernBERT Fine-tuning")
    log.info("=" * 60)
    
    train = load_split("train")
    val = load_split("val")
    test = load_split("test")
    
    train_y = [ex["label"] for ex in train]
    val_y = [ex["label"] for ex in val]
    test_y = [ex["label"] for ex in test]
    
    results = {}
    
    # ── 5a. Name only ──
    log.info("\n--- Variant: Name Only ---")
    train_texts = [ex["name"] for ex in train]
    val_texts = [ex["name"] for ex in val]
    test_texts = [ex["name"] for ex in test]
    
    results["name_only"] = run_modernbert(
        train_texts, train_y, val_texts, val_y, test_texts, test_y,
        "name_only", max_epochs=4
    )
    
    # ── 5b. Name + Context ──
    log.info("\n--- Variant: Name + Context ---")
    train_texts = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}".strip() for ex in train]
    val_texts = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}".strip() for ex in val]
    test_texts = [f"{ex['name']} [SEP] {ex.get('context_before', '')} {ex.get('context_after', '')}".strip() for ex in test]
    
    results["name_context"] = run_modernbert(
        train_texts, train_y, val_texts, val_y, test_texts, test_y,
        "name_context", max_epochs=4
    )
    
    # Save
    save_results(results, "results")
    
    log.info("\n" + "=" * 60)
    log.info("ModernBERT Summary")
    log.info("=" * 60)
    for variant, metrics in results.items():
        log.info("  %-30s  macro_f1=%.4f  entity_f1=%.4f  noise_f1=%.4f  acc=%.4f  train=%.1fs",
                 variant, metrics["macro_f1"], metrics["entity"]["f1"],
                 metrics["noise"]["f1"], metrics["accuracy"],
                 metrics["train_time_s"])
    
    log.info("Done.")


if __name__ == "__main__":
    main()
