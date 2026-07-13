#!/usr/bin/env python3
"""
Entity candidate classification — Phase 2.

Uses gpt-4o-mini to classify candidate entity names from Phase 1
into structured types. Runs in background via nohup.

Usage:
    nohup .venv/bin/python3 -u scripts/entities/classify_candidates.py \
        --batch-size 50 \
        &> data/classify-$(date +%Y%m%d-%H%M).log &
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import openai

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
)
log = logging.getLogger("classify")

PROMPT = """Classify each name from Arizona government meeting records.
Types: person, developer, law_firm, planning_firm, advocacy_group, utility, government_agency, other_organization, noise
Return ONLY a JSON array of objects with: name, is_valid (bool), entity_type, confidence (high|medium|low), reason
No markdown. Only JSON.

Names:
{names}"""


def load_candidates(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def build_batch_text(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        name = c.get("display_name", c.get("normalized_name", "?"))
        occ = c.get("occurrences", 0)
        ctx = c.get("context_samples", [])
        ctx_snip = (ctx[0][:100] if ctx else "").replace("\n", " ")
        pats = ",".join(c.get("patterns", []))[:40]
        lines.append(f'{name} (x{occ}, [{pats}]) "{ctx_snip}"')
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.strip().startswith("```"))
        text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Classify entity candidates")
    parser.add_argument("--people", default="data/entity-candidates-people.json")
    parser.add_argument("--orgs", default="data/entity-candidates-orgs.json")
    parser.add_argument("--uncertain", default="data/entity-candidates.uncertain.json")
    parser.add_argument("--output", default="data/entity-candidates-classified.json")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-candidates", type=int, default=None)
    args = parser.parse_args()

    # Load candidates
    all_candidates = []
    for source_type, path in [("person", args.people), ("organization", args.orgs), ("uncertain", args.uncertain)]:
        if not path or not Path(path).exists():
            log.info("Skipping %s — %s not found", source_type, path)
            continue
        batch = load_candidates(path)
        log.info("Loaded %d %s candidates from %s", len(batch), source_type, path)
        for c in batch:
            c["_source"] = source_type
        all_candidates.extend(batch)

    if not all_candidates:
        log.error("No candidates found.")
        return

    if args.max_candidates:
        all_candidates = all_candidates[:args.max_candidates]

    total = len(all_candidates)
    batch_size = args.batch_size
    total_batches = (total + batch_size - 1) // batch_size
    log.info("Total: %d candidates, %d per batch (%d batches)", total, batch_size, total_batches)

    # Init OpenAI
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.error("OPENAI_API_KEY not set")
        sys.exit(1)
    client = openai.OpenAI(api_key=api_key, timeout=120)

    results = []
    start = time.time()

    for i in range(0, total, batch_size):
        batch = all_candidates[i:i + batch_size]
        batch_num = i // batch_size + 1
        names_text = build_batch_text(batch)
        prompt = PROMPT.format(names=names_text)

        log.info("Batch %d/%d (%d-%d)...", batch_num, total_batches, i + 1, min(i + batch_size, total))

        # Retry loop for transient failures
        classifications = None
        for attempt in range(5):
            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You classify entity names. Return only JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=4096,
                )
                text = response.choices[0].message.content
                if not text:
                    raise ValueError("Empty response")
                classifications = parse_response(text)
                break
            except Exception as e:
                wait = 15 * (attempt + 1)
                log.warning("  Attempt %d failed: %s — retrying in %ds...", attempt + 1, e, wait)
                time.sleep(wait)

        if classifications is None:
            log.error("  Batch %d: all attempts failed — marking as noise", batch_num)
            classifications = [
                {"name": c.get("display_name", ""), "is_valid": False,
                 "entity_type": "noise", "confidence": "low", "reason": "All LLM attempts failed"}
                for c in batch
            ]

        for j, cand in enumerate(batch):
            cls = classifications[j] if j < len(classifications) else {
                "name": cand.get("display_name", ""), "is_valid": False,
                "entity_type": "noise", "confidence": "low", "reason": "Missing result",
            }
            results.append({**cand, "llm_is_valid": cls.get("is_valid", False),
                           "llm_entity_type": cls.get("entity_type", "noise"),
                           "llm_confidence": cls.get("confidence", "low"),
                           "llm_reason": cls.get("reason", "")})

        if batch_num % 10 == 0 or i + batch_size >= total:
            valid = sum(1 for r in results if r.get("llm_is_valid"))
            elapsed = time.time() - start
            rate = len(results) / elapsed if elapsed else 0
            log.info("  %d/%d done (%d valid, %.1f/s, %ds elapsed, ~%dm remaining)",
                     len(results), total, valid, rate, int(elapsed),
                     int((total - len(results)) / rate / 60) if rate > 0 else 0)

        if i + batch_size < total:
            time.sleep(0.5)

    elapsed = time.time() - start
    valid = sum(1 for r in results if r.get("llm_is_valid"))
    log.info("─" * 50)
    log.info("Done: %d total, %d valid, %d noise, %ds (%.1f min)", len(results), valid, total - valid, int(elapsed), elapsed / 60)

    type_counts = Counter(r.get("llm_entity_type") for r in results if r.get("llm_is_valid"))
    log.info("Valid entities by type:")
    for etype, count in type_counts.most_common():
        log.info("  %-25s %d", etype, count)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info("Wrote %d results to %s", len(results), output_path)

    valid_results = [r for r in results if r.get("llm_is_valid")]
    valid_path = output_path.with_suffix(".valid.json")
    with open(valid_path, "w") as f:
        json.dump(valid_results, f, indent=2, default=str)
    log.info("Wrote %d valid entities to %s", len(valid_results), valid_path)


if __name__ == "__main__":
    main()
