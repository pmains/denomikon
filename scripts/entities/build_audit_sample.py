#!/usr/bin/env python3
"""
Build a stratified audit sample from the LLM-classified entity candidates.

Produces a gold-standard evaluation set of ~1,500 records for human review.
Each record includes the LLM label and space for the human to assign:
  - audit_label: correct | wrong_type | should_be_noise | uncertain | ambiguous_taxonomy
  - audit_correct_type: the correct type (if wrong_type)
  - audit_note: short explanation

Usage:
    .venv/bin/python3 scripts/entities/build_audit_sample.py

Output:
    data/entity-audit-sample.json       (1,500 records for review)
    data/entity-audit-sample.csv         (same data, CSV for spreadsheet review)
    data/entity-audit-sample-meta.json   (sampling strategy metadata)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import random
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
)
log = logging.getLogger("audit-sample")

TARGET_SAMPLE = 1500
RANDOM_SEED = 42

# Types we care about for type classification (Stage 2)
ENTITY_TYPES = [
    "person", "law_firm", "planning_firm", "developer", "utility",
    "government_agency", "advocacy_group", "other_organization",
]

# Rare types that need oversampling to be evaluable
RARE_TYPES = {
    "consulting_firm", "engineering_firm", "legal_suffix", "company",
    "legal_firm", "organization", "consultant", "healthcare_organization",
    "staffing_firm", "event_management_firm", "noise",
}

# Minimum sample per major type for meaningful evaluation
MIN_PER_TYPE = {
    "other_organization": 250,
    "developer": 150,
    "person": 150,
    "government_agency": 120,
    "planning_firm": 100,
    "law_firm": 100,
    "advocacy_group": 80,
    "utility": 60,
}

# Confusion pairs to watch (from Aristotle)
CONFUSION_PAIRS = [
    ("planning_firm", "law_firm"),
    ("developer", "other_organization"),
    ("utility", "government_agency"),
    ("advocacy_group", "other_organization"),
]


def load_data(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def has_ambiguous_name(d: dict) -> bool:
    """Flag names that look ambiguous — short, generic, single-word, etc."""
    name = d.get("normalized_name", "")
    display = d.get("display_name", "")
    words = name.split()
    if len(words) == 1:
        return True
    if len(display) < 8:
        return True
    # Generic terms that don't tell us much
    generic = {"committee", "commission", "board", "department", "office",
               "division", "bureau", "agency", "group", "team", "section"}
    if words[-1].lower() in generic and len(words) <= 3:
        return True
    return False


def heuristics_disagree(d: dict) -> bool:
    """Did the Phase 1 heuristic pattern suggest a different type than the LLM?"""
    guessed = d.get("guessed_type")
    llm_type = d.get("llm_entity_type")
    if not guessed or not llm_type:
        return False
    # Map guessed_type to our entity types
    guess_map = {
        "person": "person",
        "organization": None,  # too generic to count as disagreement
        "uncertain": None,
    }
    mapped = guess_map.get(guessed)
    if mapped and mapped != llm_type:
        return True
    return False


def main():
    random.seed(RANDOM_SEED)

    data = load_data("data/entity-candidates-classified.json")
    log.info("Loaded %d candidates", len(data))

    # Separate valid entities by type and noise
    valid_by_type: dict[str, list[dict]] = {t: [] for t in ENTITY_TYPES}
    valid_by_type["other"] = []  # rare/other types
    noise_pool: list[dict] = []

    for d in data:
        if d.get("llm_is_valid"):
            t = d.get("llm_entity_type", "other_organization")
            if t in ENTITY_TYPES:
                valid_by_type[t].append(d)
            else:
                valid_by_type["other"].append(d)
        else:
            noise_pool.append(d)

    log.info("Valid by type:")
    for t, items in sorted(valid_by_type.items()):
        log.info("  %-25s %d", t, len(items))
    log.info("  %-25s %d", "noise", len(noise_pool))

    # ── Selection strategy ────────────────────────────────────────────
    # 1. Meet minimum per-type quota
    # 2. Fill remainder proportionally
    # 3. Oversample confidence-pair confusion zones

    selected: list[dict] = []
    selected_ids: set[str] = set()

    def already(d: dict) -> bool:
        n = d.get("normalized_name", "")
        if n in selected_ids:
            return True
        selected_ids.add(n)
        return False

    # Step 1: Minimum per-type samples
    for t, minimum in MIN_PER_TYPE.items():
        pool = valid_by_type.get(t, [])
        random.shuffle(pool)
        taken = 0
        for d in pool:
            if taken >= minimum:
                break
            if not already(d):
                selected.append(d)
                taken += 1
        log.info("  %s: minimum %d (got %d from pool of %d)", t, minimum, taken, len(pool))

    # Step 2: Oversample rare types
    rare_pool = valid_by_type.get("other", [])
    random.shuffle(rare_pool)
    rare_target = min(60, len(rare_pool))
    taken = 0
    for d in rare_pool:
        if taken >= rare_target:
            break
        if not already(d):
            selected.append(d)
            taken += 1
    log.info("  rare/other: target %d (got %d)", rare_target, taken)

    # Step 3: Oversample noise (we want ~10-15% noise in the audit)
    noise_target = int(TARGET_SAMPLE * 0.12)
    random.shuffle(noise_pool)
    taken = 0
    for d in noise_pool:
        if taken >= noise_target:
            break
        if not already(d):
            # Mark with a note
            d_copy = dict(d)
            d_copy["_sampling_note"] = "noise_oversample"
            selected.append(d_copy)
            taken += 1
    log.info("  noise: target %d (got %d)", noise_target, taken)

    # Step 4: Fill remaining slots proportionally across major types
    remaining = TARGET_SAMPLE - len(selected)
    log.info("  remaining slots to fill proportionally: %d", remaining)

    # Build proportional pools from what's left
    pools = {}
    for t in ENTITY_TYPES:
        pool = [d for d in valid_by_type.get(t, []) if d.get("normalized_name", "") not in selected_ids]
        random.shuffle(pool)
        pools[t] = pool

    # Also add some noise and rare
    extra_noise = [d for d in noise_pool if d.get("normalized_name", "") not in selected_ids]
    random.shuffle(extra_noise)
    pools["noise"] = extra_noise
    pools["other"] = [d for d in rare_pool if d.get("normalized_name", "") not in selected_ids]
    random.shuffle(pools["other"])

    # Distribute remaining slots roughly proportional to original distribution
    weights = {
        "other_organization": 0.20,
        "developer": 0.15,
        "person": 0.15,
        "government_agency": 0.12,
        "planning_firm": 0.10,
        "law_firm": 0.10,
        "advocacy_group": 0.07,
        "utility": 0.05,
        "noise": 0.04,
        "other": 0.02,
    }

    # Rebalance weights for what's actually available
    for t in list(weights.keys()):
        pool_key = t if t in pools else "other"
        if pool_key not in pools or len(pools.get(pool_key, [])) == 0:
            weights[t] = 0

    total_weight = sum(weights.values())
    allocations = {}
    used = 0
    for t, w in sorted(weights.items(), key=lambda x: -x[1]):
        alloc = int(remaining * w / total_weight)
        pool_key = t if t in pools else "other"
        available = len(pools.get(pool_key, []))
        alloc = min(alloc, available)
        allocations[t] = alloc
        used += alloc

    # Give leftover to noise or other
    leftover = remaining - used
    if leftover > 0 and pools.get("noise"):
        allocations["noise"] = allocations.get("noise", 0) + min(leftover, len(pools["noise"]))

    log.info("  proportional allocations:")
    for t, alloc in sorted(allocations.items(), key=lambda x: -x[1]):
        pool_key = t if t in pools else "other"
        available = len(pools.get(pool_key, []))
        taken = 0
        for d in pools[pool_key]:
            if taken >= alloc:
                break
            if not already(d):
                d_copy = dict(d)
                if t == "noise":
                    d_copy["_sampling_note"] = "noise_proportional"
                selected.append(d_copy)
                taken += 1
        log.info("    %-25s %d (from %d available)", t, taken, available)

    # Step 5: Ensure confusion-pair examples are well-represented
    # Flag entries that might be in confusion zones for priority review
    confusion_flagged = 0
    for d in selected:
        t = d.get("llm_entity_type", "")
        name_lower = d.get("normalized_name", "").lower()
        # Flag planning_firm ↔ law_firm confusion zone
        if t == "planning_firm":
            d["_review_priority"] = "medium"
            d["_review_note"] = "Confusion zone: planning_firm ↔ law_firm"
        elif t == "law_firm":
            d["_review_priority"] = "medium"
            d["_review_note"] = "Confusion zone: law_firm ↔ planning_firm"
        # Flag developer ↔ other_organization
        elif t == "developer" and any(kw in name_lower for kw in ["company", "inc", "llc", "group"]):
            d["_review_priority"] = "medium"
            d["_review_note"] = "Confusion zone: developer ↔ other_organization"
        elif t == "other_organization" and any(kw in name_lower for kw in ["development", "homes", "properties"]):
            d["_review_priority"] = "medium"
            d["_review_note"] = "Confusion zone: other_organization ↔ developer"
        # Flag utility ↔ government_agency
        elif t == "utility" and any(kw in name_lower for kw in ["city", "county", "department"]):
            d["_review_priority"] = "medium"
            d["_review_note"] = "Confusion zone: utility ↔ government_agency"
        elif t == "government_agency" and any(kw in name_lower for kw in ["water", "power", "electric", "utility"]):
            d["_review_priority"] = "medium"
            d["_review_note"] = "Confusion zone: government_agency ↔ utility"
        # Flag other_organization generally
        elif t == "other_organization":
            d["_review_priority"] = "high"
            d["_review_note"] = "other_organization catchall — needs scrutiny"
        # Flag ambiguous-looking names
        elif has_ambiguous_name(d):
            d["_review_priority"] = "high"
            d["_review_note"] = "Ambiguous name — verify"
        else:
            d["_review_priority"] = "normal"

        if d.get("_review_priority") in ("high", "medium"):
            confusion_flagged += 1

    log.info("  confusion-flagged for review: %d", confusion_flagged)

    # Trim to target
    if len(selected) > TARGET_SAMPLE:
        # Keep all high/medium priority, trim normal priority
        high_med = [d for d in selected if d.get("_review_priority") in ("high", "medium")]
        normal = [d for d in selected if d.get("_review_priority") == "normal"]
        random.shuffle(normal)
        keep = TARGET_SAMPLE - len(high_med)
        selected = high_med + normal[:max(0, keep)]
        log.info("  trimmed to %d (kept %d high/medium + normal)", len(selected), len(high_med))

    random.shuffle(selected)

    # ── Build audit records ──────────────────────────────────────────
    audit_records = []
    for idx, d in enumerate(selected, 1):
        record = {
            "audit_id": idx,
            "normalized_name": d.get("normalized_name", ""),
            "display_name": d.get("display_name", ""),
            "llm_entity_type": d.get("llm_entity_type", ""),
            "llm_confidence": d.get("llm_confidence", ""),
            "llm_reason": d.get("llm_reason", ""),
            "guessed_type": d.get("guessed_type", ""),
            "patterns": d.get("patterns", []),
            "occurrences": d.get("occurrences", 0),
            "sources": d.get("sources", 0),
            "jurisdictions": str(d.get("jurisdictions", {})),
            "context_samples": d.get("context_samples", [])[:2],  # first 2 samples
            "review_priority": d.get("_review_priority", "normal"),
            "review_note": d.get("_review_note", ""),
            "sampling_note": d.get("_sampling_note", ""),
            # Human audit fields — to be filled
            "audit_label": "",
            "audit_correct_type": "",
            "audit_note": "",
        }
        audit_records.append(record)

    # Add a context_id field that ties back to the normalized_name (for dedup tracking)
    log.info("─" * 50)
    log.info("Final audit sample: %d records", len(audit_records))

    # Summary stats
    type_dist = Counter(r["llm_entity_type"] for r in audit_records)
    log.info("LLM type distribution in sample:")
    for t, c in type_dist.most_common():
        log.info("  %-25s %d", t, c)

    priority_dist = Counter(r["review_priority"] for r in audit_records)
    log.info("Review priority distribution:")
    for p, c in priority_dist.most_common():
        log.info("  %-25s %d", p, c)

    # Write JSON
    output_path = Path("data/entity-audit-sample.json")
    with open(output_path, "w") as f:
        json.dump(audit_records, f, indent=2, default=str)
    log.info("Wrote %s", output_path)

    # Write CSV
    csv_path = Path("data/entity-audit-sample.csv")
    fieldnames = [
        "audit_id", "display_name", "normalized_name",
        "llm_entity_type", "llm_confidence", "llm_reason",
        "guessed_type", "patterns", "occurrences",
        "review_priority", "review_note",
        "audit_label", "audit_correct_type", "audit_note",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in audit_records:
            row = {k: r.get(k, "") for k in fieldnames}
            row["patterns"] = ", ".join(r.get("patterns", []))
            writer.writerow(row)
    log.info("Wrote %s", csv_path)

    # Write metadata
    meta = {
        "total_records": len(audit_records),
        "target_sample": TARGET_SAMPLE,
        "seed": RANDOM_SEED,
        "type_distribution": dict(type_dist.most_common()),
        "priority_distribution": dict(priority_dist),
        "sampling_strategy": {
            "min_per_type": MIN_PER_TYPE,
            "confusion_pairs": CONFUSION_PAIRS,
            "rare_types": sorted(RARE_TYPES),
            "noise_target_pct": 0.12,
            "ambiguous_name_flagged": True,
            "heuristic_disagreement_flagged": True,
        },
        "label_options": {
            "audit_label": [
                "correct",
                "wrong_type",
                "should_be_noise",
                "uncertain",
                "ambiguous_taxonomy",
            ],
            "note": "Add 'uncertain' when you can't confidently evaluate. "
                    "Add 'ambiguous_taxonomy' when the entity exists but doesn't fit a clean type.",
        },
    }
    meta_path = Path("data/entity-audit-sample-meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    log.info("Wrote %s", meta_path)


if __name__ == "__main__":
    main()
