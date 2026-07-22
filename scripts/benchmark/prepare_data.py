#!/usr/bin/env python3
"""
Prepare the entity classification dataset from training items.
Splits into train/val/test (80/10/10), generates negative samples,
and creates feature vectors for benchmark experiments.

Usage:
    nohup .venv/bin/python3 -u scripts/benchmark/prepare_data.py \
        > data/benchmark-prep-$(date +%Y%m%d-%H%M).log 2>&1 &
"""

import json
import logging
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("prepare_data")

random.seed(42)

DATA_DIR = Path("data")
BENCHMARK_DIR = DATA_DIR / "benchmark"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)


# ── 1. Load training items ──────────────────────────────────────────

def load_items(path: str) -> list[dict]:
    """Load JSONL training items."""
    items = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    log.info("Loaded %d training items from %s", len(items), path)
    return items


# ── 2. Generate negative samples ────────────────────────────────────

def get_negative_spans(text: str, positive_spans: list[dict], span_len_avg: float = 20, n_per_item: int = 15) -> list[str]:
    """Generate negative (non-entity) span examples from text.

    Strategy: extract noun-phrase-like chunks that don't overlap with
    any positive span. Returns actual text snippets.
    """
    # Build a set of positions to avoid
    occupied = set()
    for sp in positive_spans:
        for i in range(sp["start"], sp["end"]):
            occupied.add(i)

    # Extract candidate spans by splitting on punctuation / newlines
    # and finding capitalized phrases (entity-like text but not labeled)
    import re
    
    # Find all capitalized phrases (2+ words)
    candidates = []
    
    # Pattern: sequences of capitalized words
    for m in re.finditer(r'(?:[A-Z][a-zA-Z\'.\-]+\s+){1,}[A-Z][a-zA-Z\'.\-]+', text):
        start, end = m.start(), m.end()
        # Skip if overlaps with any positive span
        if any(i in occupied for i in range(max(start, 0), min(end, len(text)))):
            continue
        # Skip if it's too short
        if end - start < 4:
            continue
        candidates.append(text[start:end])
    
    # Also add some random text chunks that look like potential entities
    for m in re.finditer(r'(?:[A-Z][a-zA-Z\'.\-]*\s*){2,8}', text):
        start, end = m.start(), m.end()
        if any(i in occupied for i in range(max(start, 0), min(end, len(text)))):
            continue
        if end - start < 4:
            continue
        candidates.append(text[start:end])
    
    # Deduplicate
    candidates = list(dict.fromkeys(candidates))
    
    # Select diverse negatives: prefer ones that look entity-like
    # but aren't labeled
    random.shuffle(candidates)
    
    # Sample up to n_per_item
    result = candidates[:n_per_item]
    
    # If we don't have enough, add simple bad candidates
    if len(result) < n_per_item:
        words = text.split()
        for i in range(0, len(words) - 2, 3):
            chunk = " ".join(words[i:i+3])
            if len(chunk) > 3 and chunk not in result:
                result.append(chunk)
            if len(result) >= n_per_item:
                break
    
    return result[:n_per_item]


def generate_classification_dataset(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Generate positive and negative classification examples."""
    positives = []
    negatives = []
    
    for item in items:
        text = item["text"]
        spans = item["spans"]
        meta = item["meta"]
        
        # Get context: the surrounding text of each span
        for sp in spans:
            span_text = sp["text"]
            start = sp["start"]
            end = sp["end"]
            
            # Context: 100 chars before, span text, 100 chars after
            ctx_start = max(0, start - 100)
            ctx_end = min(len(text), end + 100)
            context = text[ctx_start:start] + "[ENT]" + span_text + "[/ENT]" + text[end:ctx_end]
            context = context.replace("\n", " ").strip()
            
            positives.append({
                "name": span_text,
                "label": 1,
                "label_str": "entity",
                "context": context,
                "context_before": text[max(0, start-100):start].replace("\n", " ").strip(),
                "context_after": text[end:min(len(text), end+100)].replace("\n", " ").strip(),
                "jurisdiction": meta.get("jurisdiction", ""),
                "meeting_type": meta.get("meeting_type", ""),
                "item_id": meta.get("sample_id", 0),
            })
        
        # Generate negatives
        neg_samples = get_negative_spans(text, spans, n_per_item=10)
        for ns in neg_samples:
            # Find where this negative appears in text
            idx = text.find(ns)
            if idx < 0:
                continue
            ctx_start = max(0, idx - 100)
            ctx_end = min(len(text), idx + len(ns) + 100)
            
            negatives.append({
                "name": ns.strip(),
                "label": 0,
                "label_str": "noise",
                "context": text[ctx_start:idx] + "[ENT]" + ns + "[/ENT]" + text[idx+len(ns):ctx_end],
                "context_before": text[max(0, idx-100):idx].replace("\n", " ").strip(),
                "context_after": text[idx+len(ns):min(len(text), idx+len(ns)+100)].replace("\n", " ").strip(),
                "jurisdiction": meta.get("jurisdiction", ""),
                "meeting_type": meta.get("meeting_type", ""),
                "item_id": meta.get("sample_id", 0),
            })
    
    log.info("Generated %d positive and %d negative examples", len(positives), len(negatives))
    return positives, negatives


# ── 3. Stratified split ────────────────────────────────────────────

def stratified_split(positives: list[dict], negatives: list[dict],
                     train_pct: float = 0.8, val_pct: float = 0.1) -> tuple[list, list, list]:
    """Split with stratification by jurisdiction."""
    all_examples = positives + negatives
    
    # Group by jurisdiction
    by_jurisdiction = defaultdict(list)
    for ex in all_examples:
        by_jurisdiction[ex["jurisdiction"]].append(ex)
    
    train, val, test = [], [], []
    
    for j, examples in by_jurisdiction.items():
        random.shuffle(examples)
        n = len(examples)
        n_train = int(n * train_pct)
        n_val = int(n * val_pct)
        
        train.extend(examples[:n_train])
        val.extend(examples[n_train:n_train + n_val])
        test.extend(examples[n_train + n_val:])
    
    random.shuffle(train)
    random.shuffle(val)
    random.shuffle(test)
    
    log.info("Split: %d train, %d val, %d test", len(train), len(val), len(test))
    
    # Check label balance
    for name, split in [("train", train), ("val", val), ("test", test)]:
        counts = Counter(ex["label_str"] for ex in split)
        log.info("  %s: entity=%d, noise=%d", name, counts.get("entity", 0), counts.get("noise", 0))
    
    return train, val, test


# ── 4. Compute structured features ──────────────────────────────────

def compute_features(name: str, context_before: str = "", context_after: str = "") -> dict:
    """Compute heuristic features for a candidate string."""
    import re
    
    features = {}
    
    # Length features
    features["name_len"] = len(name)
    features["word_count"] = len(name.split())
    
    # Capitalization features
    words = name.split()
    features["n_cap_words"] = sum(1 for w in words if w and w[0].isupper())
    features["pct_cap_words"] = features["n_cap_words"] / max(1, len(words))
    features["all_caps_ratio"] = sum(1 for c in name if c.isupper()) / max(1, len(name))
    
    # Entity keyword signals
    entity_keywords = {
        "llc", "inc", "corp", "ltd", "co", "plc", "pa", "pc",
        "company", "corporation", "group", "partners", "associates",
        "management", "consulting", "construction", "development",
        "design", "engineering", "solutions", "services",
        "properties", "realty", "holdings", "capital", "ventures",
        "homes", "builders", "communities", "industries",
    }
    name_lower = name.lower()
    features["has_legal_suffix"] = int(bool(re.search(
        r'\b(llc|inc|ltd|corp|plc|pa|pc|llp)\b\.?', name_lower
    )))
    features["has_entity_keyword"] = int(any(kw in name_lower for kw in entity_keywords))
    
    # Government/agency keywords
    gov_keywords = {"city of", "town of", "county of", "department", "board", "commission",
                    "committee", "authority", "bureau", "office", "agency", "division"}
    features["has_gov_keyword"] = int(any(kw in name_lower for kw in gov_keywords))
    
    # Person-like features (name has initials, common person patterns)
    features["has_middle_initial"] = int(bool(re.search(r'\b[A-Z]\.\s+[A-Z]', name)))
    features["n_commas"] = name.count(",")
    
    # Numbers in name (often indicates ordinance/resolution/code references)
    features["has_number"] = int(bool(re.search(r'\d', name)))
    
    # Contains "&" or "and"
    features["has_ampersand_or_and"] = int("&" in name or " and " in name_lower)
    
    # Punctuation density
    punct_chars = sum(1 for c in name if c in ".,;:!?-()[]{}'\"")
    features["punct_density"] = punct_chars / max(1, len(name))
    
    # Context features
    features["ctx_before_len"] = len(context_before)
    features["ctx_after_len"] = len(context_after)
    
    # Colon/separation signals in context
    features["ctx_has_colon"] = int(":" in context_before[-50:])
    features["ctx_has_dash"] = int("\u2013" in context_before[-50:] or "--" in context_before[-50:])
    
    return features


def add_features_to_dataset(dataset: list[dict]) -> list[dict]:
    """Add structured features to all examples."""
    for ex in dataset:
        feats = compute_features(ex["name"], ex.get("context_before", ""), ex.get("context_after", ""))
        ex["features"] = feats
    return dataset


# ── 5. Main ─────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Entity Classification Benchmark: Data Preparation")
    log.info("=" * 60)
    
    start = time.time()
    
    # Load items
    items = load_items(str(DATA_DIR / "entity-training-items.jsonl"))
    
    # Generate classification dataset
    positives, negatives = generate_classification_dataset(items)
    
    # Stratified split
    train, val, test = stratified_split(positives, negatives, train_pct=0.8, val_pct=0.1)
    
    # Add structured features
    train = add_features_to_dataset(train)
    val = add_features_to_dataset(val)
    test = add_features_to_dataset(test)
    
    # Save splits
    def save_split(split, name):
        path = BENCHMARK_DIR / f"{name}.json"
        with open(path, "w") as f:
            json.dump(split, f, indent=2)
        log.info("Saved %d examples to %s", len(split), path)
    
    save_split(train, "train")
    save_split(val, "val")
    save_split(test, "test")
    
    # Save combined
    combined = {"train": train, "val": val, "test": test}
    with open(BENCHMARK_DIR / "splits.json", "w") as f:
        json.dump(combined, f, indent=2)
    
    # Save feature metadata
    if train:
        feature_names = sorted(train[0]["features"].keys())
        with open(BENCHMARK_DIR / "feature_names.json", "w") as f:
            json.dump(feature_names, f, indent=2)
        log.info("Feature names: %s", feature_names)
    
    elapsed = time.time() - start
    log.info("Done in %.1fs", elapsed)
    log.info("Results in %s", BENCHMARK_DIR / "splits.json")


if __name__ == "__main__":
    main()
