#!/usr/bin/env python3
"""
Benchmark B (Extraction): GLiNER zero-shot and fine-tuned for entity extraction.

Tasks:
1. Existing spaCy pipeline baseline (word-level entity detection)
2. GLiNER zero-shot extraction
3. GLiNER fine-tuned on human span annotations

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/b1_gliner_extraction.py \
        > data/benchmark-b1-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import json
import logging
import os
import re
import sys
import time
import numpy as np
from pathlib import Path
from collections import defaultdict, Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("benchmark_gliner")

DATA_DIR = Path("data")
BENCHMARK_DIR = DATA_DIR / "benchmark" / "gliner"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

# ── Load data ──────────────────────────────────────────────────────

def load_training_items(path: str):
    items = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    log.info("Loaded %d items from %s", len(items), path)
    return items


def split_items(items: list[dict]):
    """Use the same 80/10/10 split as the classification benchmark."""
    # Reload the split from benchmark data
    with open(DATA_DIR / "benchmark" / "splits.json") as f:
        splits = json.load(f)
    
    # Get the sample IDs used in each split
    train_ids = set(ex["item_id"] for ex in splits["train"])
    val_ids = set(ex["item_id"] for ex in splits["val"])
    test_ids = set(ex["item_id"] for ex in splits["test"])
    
    train = [i for i in items if i["meta"]["sample_id"] in train_ids]
    val = [i for i in items if i["meta"]["sample_id"] in val_ids]
    test = [i for i in items if i["meta"]["sample_id"] in test_ids]
    
    log.info("Split: %d train, %d val, %d test items", len(train), len(val), len(test))
    return train, val, test


# ── Metrics for extraction ─────────────────────────────────────────

def resolve_entity_overlap(spans: list[dict]) -> list[dict]:
    """Resolve overlapping spans by taking the longer one."""
    if not spans:
        return spans
    sorted_spans = sorted(spans, key=lambda s: s["start"])
    resolved = []
    for sp in sorted_spans:
        if not resolved:
            resolved.append(sp)
            continue
        last = resolved[-1]
        if sp["start"] < last["end"]:
            # Overlap: keep longer one
            if sp["end"] - sp["start"] > last["end"] - last["start"]:
                resolved[-1] = sp
        else:
            resolved.append(sp)
    return resolved


def normalize_text(text: str) -> str:
    """Normalize for matching: lowercase, collapse whitespace, remove punctuation."""
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9\s\'\-]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def compute_extraction_metrics(gold_spans: list[list[dict]],
                               pred_spans: list[list[dict]],
                               texts: list[str]) -> dict:
    """Compute token-level and span-level metrics."""
    span_tp, span_fp, span_fn = 0, 0, 0
    
    for golds, preds, text in zip(gold_spans, pred_spans, texts):
        # Resolve overlaps
        golds = resolve_entity_overlap(golds)
        preds = resolve_entity_overlap(preds)
        
        gold_set = set()
        for g in golds:
            g_text = normalize_text(g["text"])
            if g_text:
                gold_set.add(g_text)
        
        pred_set = set()
        for p in preds:
            p_text = normalize_text(p["text"])
            if p_text:
                pred_set.add(p_text)
        
        # For span-level evaluation, we match on normalized text
        # Since exact position matching is hard, we use text-based matching
        matched_gold = set()
        matched_pred = set()
        
        for g_text in gold_set:
            for p_text in pred_set:
                if g_text == p_text or p_text in g_text or g_text in p_text:
                    matched_gold.add(g_text)
                    matched_pred.add(p_text)
                    break
        
        span_tp += len(matched_gold)
        span_fp += len(pred_set - matched_pred)
        span_fn += len(gold_set - matched_gold)
    
    # Compute metrics
    precision = span_tp / (span_tp + span_fp) if (span_tp + span_fp) > 0 else 0.0
    recall = span_tp / (span_tp + span_fn) if (span_tp + span_fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    
    return {
        "f1": round(f1, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "tp": int(span_tp),
        "fp": int(span_fp),
        "fn": int(span_fn),
    }


# ── 1. spaCy baseline ──────────────────────────────────────────────

def run_spacy_baseline(items: list[dict]) -> list[list[dict]]:
    """Run existing spaCy NER pipeline and return predicted spans."""
    import spacy
    
    log.info("Loading spaCy model...")
    t0 = time.time()
    try:
        nlp = spacy.load("en_core_web_lg")
    except OSError:
        log.info("en_core_web_lg not found, downloading...")
        spacy.cli.download("en_core_web_lg")
        nlp = spacy.load("en_core_web_lg")
    log.info("spaCy loaded in %.1fs", time.time() - t0)
    
    all_pred_spans = []
    
    t0 = time.time()
    for item in items:
        text = item["text"]
        doc = nlp(text)
        
        preds = []
        for ent in doc.ents:
            preds.append({
                "start": ent.start_char,
                "end": ent.end_char,
                "text": ent.text,
                "label": ent.label_,
            })
        
        all_pred_spans.append(preds)
    
    elapsed = time.time() - t0
    log.info("spaCy processed %d items in %.1fs (%.1f/s)", len(items), elapsed,
             len(items) / elapsed if elapsed else 0)
    
    return all_pred_spans


# ── 2. GLiNER zero-shot ────────────────────────────────────────────

def run_gliner_zeroshot(items: list[dict], labels: list[str]) -> list[list[dict]]:
    """Run GLiNER zero-shot extraction."""
    from gliner import GLiNER
    
    log.info("Loading GLiNER model (urchade/gliner_multi-v2.1)...")
    t0 = time.time()
    model = GLiNER.from_pretrained("urchade/gliner_multi-v2.1")
    log.info("GLiNER loaded in %.1fs", time.time() - t0)
    
    all_pred_spans = []
    
    t0 = time.time()
    for item in items:
        text = item["text"]
        entities = model.predict_entities(text, labels, threshold=0.5)
        
        preds = []
        for ent in entities:
            preds.append({
                "start": ent.get("start", 0),
                "end": ent.get("end", 0),
                "text": ent.get("text", ""),
                "label": ent.get("label", ""),
            })
        
        all_pred_spans.append(preds)
    
    elapsed = time.time() - t0
    log.info("GLiNER zero-shot processed %d items in %.1fs (%.1f/s)", len(items), elapsed,
             len(items) / elapsed if elapsed else 0)
    
    return all_pred_spans


# ── 3. GLiNER fine-tuned ──────────────────────────────────────────

def run_gliner_finetuned(train_items: list[dict], test_items: list[dict],
                         labels: list[str]) -> list[list[dict]]:
    """Fine-tune GLiNER on training data and evaluate on test."""
    from gliner import GLiNER
    
    log.info("Loading GLiNER model for fine-tuning...")
    t0 = time.time()
    model = GLiNER.from_pretrained("urchade/gliner_multi-v2.1")
    log.info("GLiNER loaded in %.1fs", time.time() - t0)
    
    # Prepare training data in GLiNER format
    train_samples = []
    for item in train_items:
        text = item["text"]
        gold_spans = item["spans"]
        # Convert to GLiNER format
        entities = []
        for sp in gold_spans:
            entities.append({
                "text": sp["text"],
                "start": sp["start"],
                "end": sp["end"],
                "label": "entity",
            })
        train_samples.append({
            "text": text,
            "entities": entities,
        })
    
    log.info("Training with %d samples...", len(train_samples))
    t0 = time.time()
    
    # GLiNER fine-tuning can be memory-intensive. Use a subset if needed
    sample_size = min(200, len(train_samples))
    if len(train_samples) > sample_size:
        import random
        random.seed(42)
        train_samples = random.sample(train_samples, sample_size)
        log.info("Using %d training samples (memory constraint)", sample_size)
    
    # GLiNER's train_model expects dataset+eval_dataset and output_dir
    # Use a small eval split for tracking
    import random
    random.seed(42)
    val_split = train_samples[:5] if len(train_samples) > 5 else train_samples
    
    model.train_model(
        train_dataset=train_samples,
        eval_dataset=val_split,
        output_dir=str(BENCHMARK_DIR / "gliner_ft_model"),
        learning_rate=1e-5,
        per_device_train_batch_size=4,
        max_steps=min(500, len(train_samples) * 5),
        save_steps=500,
        logging_steps=50,
    )
    train_time = time.time() - t0
    log.info("GLiNER fine-tuning completed in %.1fs", train_time)
    
    # Evaluate
    all_pred_spans = []
    t0 = time.time()
    for item in test_items:
        text = item["text"]
        entities = model.predict_entities(text, labels, threshold=0.5)
        
        preds = []
        for ent in entities:
            preds.append({
                "start": ent.get("start", 0),
                "end": ent.get("end", 0),
                "text": ent.get("text", ""),
                "label": ent.get("label", ""),
            })
        all_pred_spans.append(preds)
    
    elapsed = time.time() - t0
    log.info("GLiNER fine-tuned processed %d items in %.1fs (%.1f/s)", len(test_items), elapsed,
             len(test_items) / elapsed if elapsed else 0)
    
    return all_pred_spans


# ── Main ────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Benchmark B: Entity Extraction (GLiNER + spaCy)")
    log.info("=" * 60)
    
    items = load_training_items(str(DATA_DIR / "entity-training-items.jsonl"))
    train_items, val_items, test_items = split_items(items)
    
    gold_spans = [item["spans"] for item in test_items]
    texts = [item["text"] for item in test_items]
    
    results = {}
    
    # ── B1. spaCy baseline ──
    log.info("\n--- B1. spaCy NER Baseline ---")
    if os.path.exists(str(BENCHMARK_DIR / "spacy_preds.json")):
        log.info("Loading cached spaCy predictions...")
        with open(str(BENCHMARK_DIR / "spacy_preds.json")) as f:
            spacy_preds = json.load(f)
    else:
        spacy_preds = run_spacy_baseline(test_items)
        with open(str(BENCHMARK_DIR / "spacy_preds.json"), "w") as f:
            json.dump(spacy_preds, f)
    
    spacy_metrics = compute_extraction_metrics(gold_spans, spacy_preds, texts)
    results["spacy"] = spacy_metrics
    log.info("  spaCy: f1=%.4f, precision=%.4f, recall=%.4f (tp=%d, fp=%d, fn=%d)",
             spacy_metrics["f1"], spacy_metrics["precision"], spacy_metrics["recall"],
             spacy_metrics["tp"], spacy_metrics["fp"], spacy_metrics["fn"])
    
    # ── B2. GLiNER zero-shot ──
    log.info("\n--- B2. GLiNER Zero-shot ---")
    labels = ["organization", "person", "location", "government agency", "company",
              "legal entity", "ordinance", "resolution", "program name", "building"]
    
    if os.path.exists(str(BENCHMARK_DIR / "gliner_zs_preds.json")):
        log.info("Loading cached GLiNER zero-shot predictions...")
        with open(str(BENCHMARK_DIR / "gliner_zs_preds.json")) as f:
            gliner_zs_preds = json.load(f)
    else:
        gliner_zs_preds = run_gliner_zeroshot(test_items, labels)
        with open(str(BENCHMARK_DIR / "gliner_zs_preds.json"), "w") as f:
            json.dump(gliner_zs_preds, f)
    
    gliner_zs_metrics = compute_extraction_metrics(gold_spans, gliner_zs_preds, texts)
    results["gliner_zeroshot"] = gliner_zs_metrics
    log.info("  GLiNER zero-shot: f1=%.4f, precision=%.4f, recall=%.4f (tp=%d, fp=%d, fn=%d)",
             gliner_zs_metrics["f1"], gliner_zs_metrics["precision"], gliner_zs_metrics["recall"],
             gliner_zs_metrics["tp"], gliner_zs_metrics["fp"], gliner_zs_metrics["fn"])
    
    # ── B3. GLiNER fine-tuned ──
    log.info("\n--- B3. GLiNER Fine-tuned ---")
    if os.path.exists(str(BENCHMARK_DIR / "gliner_ft_preds.json")):
        log.info("Loading cached GLiNER fine-tuned predictions...")
        with open(str(BENCHMARK_DIR / "gliner_ft_preds.json")) as f:
            gliner_ft_preds = json.load(f)
    else:
        gliner_ft_preds = run_gliner_finetuned(train_items, test_items, labels)
        with open(str(BENCHMARK_DIR / "gliner_ft_preds.json"), "w") as f:
            json.dump(gliner_ft_preds, f)
    
    gliner_ft_metrics = compute_extraction_metrics(gold_spans, gliner_ft_preds, texts)
    results["gliner_finetuned"] = gliner_ft_metrics
    log.info("  GLiNER fine-tuned: f1=%.4f, precision=%.4f, recall=%.4f (tp=%d, fp=%d, fn=%d)",
             gliner_ft_metrics["f1"], gliner_ft_metrics["precision"], gliner_ft_metrics["recall"],
             gliner_ft_metrics["tp"], gliner_ft_metrics["fp"], gliner_ft_metrics["fn"])
    
    # Save results
    with open(str(BENCHMARK_DIR / "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    log.info("Saved results to %s", BENCHMARK_DIR / "results.json")
    
    log.info("\n" + "=" * 60)
    log.info("Extraction Benchmark Summary")
    log.info("=" * 60)
    for name, metrics in results.items():
        log.info("  %-25s  f1=%.4f  precision=%.4f  recall=%.4f",
                 name, metrics["f1"], metrics["precision"], metrics["recall"])
    
    log.info("Done.")


if __name__ == "__main__":
    main()
