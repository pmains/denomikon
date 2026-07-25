#!/usr/bin/env python3
"""
Build a stratified random sample of ~300 agenda items for pure mention-level
entity annotation.  No entity types, no ontology — just "is this span an entity?"

Stratification axes:
  • Jurisdiction (county + major cities + smaller towns)
  • Meeting type (public hearing, consent agenda, study session, etc.)
  • Agenda item length
  • Entity density (estimated via capitalized multi-word phrase heuristic)

Usage:
    .venv/bin/python3 scripts/entities/build_mention_sample.py
    .venv/bin/python3 scripts/entities/build_mention_sample.py --output data/mention-sample.json
    .venv/bin/python3 scripts/entities/build_mention_sample.py --dry-run  (stats only)
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db.core import get_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
)
log = logging.getLogger("mention-sample")

TARGET_SAMPLE = 300
RANDOM_SEED = 42
MAX_PER_JURISDICTION = 30  # Cap per-jurisdiction share

# ── Stratification weights ────────────────────────────────────────────

# Jurisdiction buckets: ensure broad geographic + body-type coverage
JURISDICTION_PRIORITY = {
    "Maricopa County": 0.20,
    "Phoenix": 0.18,
    "Chandler": 0.10,
    "Mesa": 0.10,
    "Tempe": 0.10,
    "Scottsdale": 0.08,
    "Glendale": 0.06,
    "Peoria": 0.04,
    "Surprise": 0.03,
    "Goodyear": 0.03,
    "Buckeye": 0.02,
    "Avondale": 0.02,
    "Gilbert": 0.02,
    "El Mirage": 0.01,
    "Tolleson": 0.01,
}

# Meeting-type weights: ensure variety in agenda structure
MEETING_TYPE_WEIGHTS = {
    "Regular Session": 0.45,
    "Study Session": 0.10,
    "Public Hearing": 0.10,
    "Special Session": 0.05,
    "Work Session": 0.05,
    "Planning and Zoning": 0.10,
    "Board of Adjustment": 0.05,
    "Executive Session": 0.03,
    "Other": 0.07,
}

MEETING_TYPE_MAP = {
    "regular": "Regular Session",
    "study": "Study Session",
    "special": "Special Session",
    "work": "Work Session",
    "public hearing": "Public Hearing",
    "planning": "Planning and Zoning",
    "zoning": "Planning and Zoning",
    "p&z": "Planning and Zoning",
    "board of adjustment": "Board of Adjustment",
    "boac": "Board of Adjustment",
    "executive": "Executive Session",
}

# ── Entity density estimation ─────────────────────────────────────────

# Simple heuristic: count capitalized multi-word phrases that look like
# entity names (organizations, law firms, developers, etc.)
ENTITY_LIKE_PATTERN = re.compile(
    r"[A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,5}"
    r"(?:\s+(?:LLC|PLC|PLLC|PC|PA|Inc|Corp|Ltd|Co|Group|"
    r"Homes|Development|Design|Architecture|Engineering|"
    r"Consulting|Construction|Properties|Planning|Associates|"
    r"Partners|Solutions|Communities|Ventures|Holdings|"
    r"Management|Enterprises|Builders|Landscape|Law))?",
)


def estimate_entity_density(text: str) -> int:
    """Rough count of entity-like phrases in text.  Used for stratification only."""
    if not text:
        return 0
    matches = ENTITY_LIKE_PATTERN.findall(text)
    # Heuristic boost: presence of "Applicant:" or "Attorney:" fields
    field_indicators = len(re.findall(
        r"(?:Applicant|Attorney|Represented by|Planner|Architect|Engineer)\s*:",
        text, re.IGNORECASE,
    ))
    return len(matches) + (field_indicators * 3)


def assign_length_bucket(char_count: int) -> str:
    if char_count < 200:
        return "short"
    elif char_count < 800:
        return "medium"
    elif char_count < 3000:
        return "long"
    else:
        return "very_long"


def assign_density_bucket(score: int) -> str:
    if score <= 2:
        return "low"
    elif score <= 8:
        return "medium"
    elif score <= 20:
        return "high"
    else:
        return "very_high"


def normalize_meeting_type(raw: str) -> str:
    """Map raw meeting_type strings into the stratified buckets."""
    if not raw:
        return "Other"
    raw_lower = raw.strip().lower()
    for key, mapped in MEETING_TYPE_MAP.items():
        if key in raw_lower:
            return mapped
    return "Other"


# ── Database queries ──────────────────────────────────────────────────


def fetch_agenda_items(engine, limit: int = None) -> list[dict]:
    """Fetch agenda items with enough context for stratification and annotation."""
    log.info("Fetching agenda items from database...")

    query = """
        SELECT
            ai.id,
            ai.agenda_item_title,
            ai.agenda_item_text,
            ai.agenda_item_number,
            ai.item_type,
            m.id AS meeting_db_id,
            m.meeting_date,
            m.meeting_type,
            m.meeting_title,
            m.body AS meeting_body_code,
            pb.name AS body_name,
            j.name AS jurisdiction_name,
            j.id AS jurisdiction_id,
            b.name AS public_body_name
        FROM agenda_items ai
        JOIN meetings m ON m.id = ai.meeting_db_id
        JOIN public_bodies pb ON pb.body_code = m.body
        JOIN jurisdictions j ON j.id = m.jurisdiction_id
        LEFT JOIN public_bodies b ON b.id = ai.public_body_id
        WHERE (ai.agenda_item_text IS NOT NULL AND ai.agenda_item_text != '')
           OR (ai.agenda_item_title IS NOT NULL AND ai.agenda_item_title != '')
        ORDER BY RANDOM()
    """

    if limit:
        query += f" LIMIT {limit}"

    start = time.time()
    with engine.connect() as c:
        rows = c.execute(text(query)).fetchall()
    elapsed = time.time() - start
    log.info("  Fetched %d items in %.1fs", len(rows), elapsed)

    items = []
    for r in rows:
        title = r[1] or ""
        text_content = r[2] or ""
        # Deduplicate: skip text_content when it's identical to the title
        # or a near-duplicate (common when scrapers store same content in both fields)
        title_stripped = title.strip()
        text_stripped = text_content.strip()
        if text_stripped and text_stripped != title_stripped and not text_stripped.startswith(title_stripped[:80]):
            full_text = f"{title}\n{text_content}"
        else:
            full_text = title or text_content or ""
        char_count = len(full_text)

        items.append({
            "id": r[0],
            "title": title,
            "text": full_text,
            "char_count": char_count,
            "item_number": r[3] or "",
            "item_type": r[4] or "",
            "meeting_db_id": r[5] if r[5] else 0,
            "meeting_date": str(r[6] or ""),
            "meeting_type_raw": r[7] or "",
            "meeting_title": r[8] or "",
            "body_code": r[9] or "",
            "body_name": r[10] or "",
            "jurisdiction": r[11] or "",
            "jurisdiction_id": r[12] if r[12] else 0,
            "public_body_name": r[13] or "",
        })

    return items


def precompute_stratification(items: list[dict]) -> list[dict]:
    """Add stratification fields to each item."""
    for item in items:
        item["length_bucket"] = assign_length_bucket(item["char_count"])
        item["density_score"] = estimate_entity_density(item["text"])
        item["density_bucket"] = assign_density_bucket(item["density_score"])
        item["meeting_type_normalized"] = normalize_meeting_type(item["meeting_type_raw"])
    return items


# ── Stratified sampling ───────────────────────────────────────────────


def stratified_sample(items: list[dict], target: int = 300,
                      seed: int = 42) -> list[dict]:
    """Select a stratified random sample of agenda items.

    Stratifies on jurisdiction, meeting type, length bucket, and density bucket
    to ensure the sample is representative of the full corpus.
    """
    random.seed(seed)

    # Precompute stratification fields
    items = precompute_stratification(items)

    # Log population distribution
    log.info("─" * 50)
    log.info("Population distribution:")

    jur_dist = Counter(item["jurisdiction"] for item in items)
    log.info("  Jurisdictions:")
    for j, c in jur_dist.most_common():
        log.info("    %-25s %5d (%5.1f%%)", j[:25], c, c / len(items) * 100)

    mt_dist = Counter(item["meeting_type_normalized"] for item in items)
    log.info("  Meeting types:")
    for t, c in mt_dist.most_common():
        log.info("    %-25s %5d (%5.1f%%)", t[:25], c, c / len(items) * 100)

    len_dist = Counter(item["length_bucket"] for item in items)
    log.info("  Length buckets:")
    for t, c in len_dist.most_common():
        log.info("    %-25s %5d (%5.1f%%)", t[:25], c, c / len(items) * 100)

    dens_dist = Counter(item["density_bucket"] for item in items)
    log.info("  Entity density buckets:")
    for t, c in dens_dist.most_common():
        log.info("    %-25s %5d (%5.1f%%)", t[:25], c, c / len(items) * 100)

    # ── Build selection quotas ──

    # 1. Jurisdiction allocation
    jur_alloc: dict[str, int] = {}
    remaining = target
    total_weight = sum(JURISDICTION_PRIORITY.get(j, 0.02) for j in jur_dist)
    for j in jur_dist:
        weight = JURISDICTION_PRIORITY.get(j, 0.02)
        alloc = max(1, int(target * weight / total_weight))
        avail = jur_dist[j]
        alloc = min(alloc, avail, MAX_PER_JURISDICTION)
        jur_alloc[j] = alloc
        remaining -= alloc

    # Distribute remaining evenly across jurisdictions that can take more
    if remaining > 0:
        undersubscribed = sorted(
            [(j, jur_dist[j] - jur_alloc[j]) for j in jur_dist
             if jur_alloc[j] < jur_dist[j] and jur_alloc[j] < MAX_PER_JURISDICTION],
            key=lambda x: -x[1],
        )
        for j, avail in undersubscribed:
            if remaining <= 0:
                break
            can_take = min(remaining, avail, MAX_PER_JURISDICTION - jur_alloc[j])
            jur_alloc[j] += can_take
            remaining -= can_take

    log.info("─" * 50)
    log.info("Jurisdiction allocation (target=%d, max_per_jurisdiction=%d):",
             target, MAX_PER_JURISDICTION)
    for j, alloc in sorted(jur_alloc.items(), key=lambda x: -x[1]):
        avail = jur_dist[j]
        log.info("  %-25s %3d / %5d available", j[:25], alloc, avail)

    # 2. Within each jurisdiction, allocate by meeting type, length, density
    # Build stratified pools: (jurisdiction, meeting_type, length, density) → items
    stratum_pools: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        key = (
            item["jurisdiction"],
            item["meeting_type_normalized"],
            item["length_bucket"],
            item["density_bucket"],
        )
        stratum_pools[key].append(item)

    # Shuffle each pool
    for pool in stratum_pools.values():
        random.shuffle(pool)

    selected: list[dict] = []
    selected_ids: set[int] = set()

    # Allocate per jurisdiction
    for jur, jur_quota in sorted(jur_alloc.items(), key=lambda x: -x[1]):
        # Gather all strata for this jurisdiction
        jur_strata = [(k, v) for k, v in stratum_pools.items() if k[0] == jur]
        if not jur_strata:
            continue

        # Try to get a diverse mix: prefer different meeting types, lengths, densities
        taken = 0
        # Round-robin through meeting types first
        for mt in sorted(set(k[1] for k, _ in jur_strata)):
            if taken >= jur_quota:
                break
            mt_strata = [(k, v) for k, v in jur_strata if k[1] == mt]
            for len_bucket in ["short", "medium", "long", "very_long"]:
                if taken >= jur_quota:
                    break
                for dens_bucket in ["low", "medium", "high", "very_high"]:
                    if taken >= jur_quota:
                        break
                    pool = [p for k, p in mt_strata if k[2] == len_bucket and k[3] == dens_bucket]
                    if pool:
                        pool_flat = pool[0]
                        available = [it for it in pool_flat if it["id"] not in selected_ids]
                        if available:
                            pick = available[0]
                            pick["_stratum"] = f"{jur}/{mt}/{len_bucket}/{dens_bucket}"
                            selected.append(pick)
                            selected_ids.add(pick["id"])
                            taken += 1

        # If quota not met, fill from remaining items in this jurisdiction
        if taken < jur_quota:
            remaining_items = [it for it in items
                               if it["jurisdiction"] == jur
                               and it["id"] not in selected_ids]
            random.shuffle(remaining_items)
            for it in remaining_items:
                if taken >= jur_quota:
                    break
                it["_stratum"] = f"{jur}/fill"
                selected.append(it)
                selected_ids.add(it["id"])
                taken += 1

    # Shuffle final selection
    random.shuffle(selected)

    log.info("─" * 50)
    log.info("Final sample: %d items", len(selected))

    # Summary stats
    jur_final = Counter(it["jurisdiction"] for it in selected)
    log.info("  Jurisdictions in sample:")
    for j, c in jur_final.most_common():
        log.info("    %-25s %3d (%.1f%%)", j[:25], c, c / len(selected) * 100)

    mt_final = Counter(it["meeting_type_normalized"] for it in selected)
    log.info("  Meeting types in sample:")
    for t, c in mt_final.most_common():
        log.info("    %-25s %3d (%.1f%%)", t[:25], c, c / len(selected) * 100)

    len_final = Counter(it["length_bucket"] for it in selected)
    log.info("  Length buckets in sample:")
    for t, c in len_final.most_common():
        log.info("    %-25s %3d (%.1f%%)", t[:25], c, c / len(selected) * 100)

    dens_final = Counter(it["density_bucket"] for it in selected)
    log.info("  Entity density buckets in sample:")
    for t, c in dens_final.most_common():
        log.info("    %-25s %3d (%.1f%%)", t[:25], c, c / len(selected) * 100)

    return selected


# ── Pre-annotation (weak) ─────────────────────────────────────────────


def pre_highlight_entities(text: str) -> list[dict]:
    """Identify likely entity spans using simple heuristics.

    Returns a list of {start, end, text} dicts representing character-offset spans.
    This is a weak pre-annotation — the human accepts/rejects/adjusts.

    Heuristics used (in priority order):
    1. "Applicant:" / "Attorney:" / "Represented by:" field values
    2. Known org names from extract.py (if available)
    3. "X & Y" firm patterns
    4. Capitalized phrases ending with legal/org keywords (LLC, Inc, Development, etc.)
    5. Role-triggered person names ("presented by John Smith")
    """
    spans = []
    seen_offsets: set[tuple[int, int]] = set()

    def add_span(start: int, end: int, text_snip: str):
        """Add span if it doesn't overlap with an existing one."""
        for s, e in seen_offsets:
            if not (end <= s or start >= e):
                return  # overlaps
        seen_offsets.add((start, end))
        spans.append({"start": start, "end": end, "text": text_snip, "auto": True})

    # 1. Field-value patterns: Applicant, Attorney, etc.
    for pat_name, pat in [
        ("applicant", re.compile(r"(?:Applicant|Applicant/Owner|Petitioner)\s*:?\s*(.{5,80}?)(?:\n|$)", re.MULTILINE)),
        ("attorney", re.compile(r"(?:Attorney|Represented by|Represented By|Counsel)\s*:?\s*(.{5,80}?)(?:\n|$)", re.MULTILINE)),
        ("planner", re.compile(r"(?:Planner|Planning Consultant|Architect|Engineer)\s*:?\s*(.{5,80}?)(?:\n|$)", re.MULTILINE)),
    ]:
        for m in pat.finditer(text):
            val = m.group(1).strip().rstrip(".,;")
            if len(val) >= 5:
                add_span(m.start(1), m.start(1) + len(val), val)

    # 2. Known organizations (try importing from extract.py, fallback to empty)
    known_orgs = []
    try:
        from entities.extract import KNOWN_ORGANIZATIONS
        known_orgs = list(KNOWN_ORGANIZATIONS.keys())
        known_orgs.sort(key=len, reverse=True)
    except (ImportError, ModuleNotFoundError):
        pass

    if known_orgs:
        known_pat = re.compile("(" + "|".join(re.escape(n) for n in known_orgs) + ")", re.IGNORECASE)
        for m in known_pat.finditer(text):
            add_span(m.start(1), m.end(1), m.group(1))

    # 3. "X & Y" patterns (firm names)
    amp_pat = re.compile(r"\b([A-Z][A-Za-z0-9'.\-]+\s+&\s+[A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){0,3})\b")
    for m in amp_pat.finditer(text):
        add_span(m.start(1), m.end(1), m.group(1))

    # 4. Capitalized phrases ending with legal/org keywords
    org_kw_pat = re.compile(
        r"\b([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,5}"
        r"\s+(?:Homes|Development|Group|Design|Architecture|Engineering|"
        r"Consulting|Construction|Properties|Planning|Associates|Partners|"
        r"Solutions|Communities|Ventures|Holdings|Management|Enterprises|"
        r"Builders|Landscape|"
        r"LLC|P\.?L\.?L\.?C\.?|P\.?L\.?C\.?|P\.?C\.?|P\.?A\.?|"
        r"L\.?L\.?C\.?|I\.?N\.?C\.?|L\.?T\.?D\.?))\\b"
    )
    for m in org_kw_pat.finditer(text):
        add_span(m.start(1), m.end(1), m.group(1))

    # 5. Legal suffix endings
    legal_pat = re.compile(
        r"\b([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,5}"
        r"(?:,?\s+(?:P\.?L\.?L\.?C\.?|P\.?L\.?C\.?|P\.?C\.?|P\.?A\.?|"
        r"L\.?L\.?C\.?|I\.?N\.?C\.?|L\.?T\.?D\.?)))\\b"
    )
    for m in legal_pat.finditer(text):
        add_span(m.start(1), m.end(1), m.group(1))

    return spans


def process_mention_sample(items: list[dict]) -> list[dict]:
    """Add pre-annotation spans and structure for human review."""
    output = []
    for idx, item in enumerate(items, 1):
        spans = pre_highlight_entities(item["text"])

        record = {
            "sample_id": idx,
            "item_id": item["id"],
            "title": item["title"],
            "text": item["text"],
            "char_count": item["char_count"],
            "density_score": item["density_score"],
            "entity_spans": spans,
            "annotation_status": "unreviewed",
            # Metadata for context
            "jurisdiction": item["jurisdiction"],
            "meeting_type": item["meeting_type_normalized"],
            "meeting_date": item["meeting_date"],
            "meeting_title": item["meeting_title"],
            "body_code": item["body_code"],
            "body_name": item["body_name"],
            "item_number": item["item_number"],
            "item_type": item["item_type"],
            "meeting_db_id": item["meeting_db_id"],
            "stratum": item.get("_stratum", ""),
            # Annotation fields (human fills)
            "human_spans": [],
            "notes": "",
        }
        output.append(record)

    return output


# ── Main ──────────────────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Build a stratified sample for entity mention annotation"
    )
    parser.add_argument("--output", type=str, default="data/mention-sample.json",
                        help="Output JSON path (default: data/mention-sample.json)")
    parser.add_argument("--target", type=int, default=TARGET_SAMPLE,
                        help=f"Target sample size (default: {TARGET_SAMPLE})")
    parser.add_argument("--limit", type=int, default=20000,
                        help="Max items to fetch from DB (for performance; default 20000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show stats only without writing output")
    parser.add_argument("--seed", type=int, default=RANDOM_SEED,
                        help=f"Random seed (default: {RANDOM_SEED})")
    parser.add_argument("--no-pre-annotate", action="store_true",
                        help="Skip pre-annotation (no auto-detected spans)")
    parser.add_argument("--min-chars", type=int, default=200,
                        help="Minimum text length (default: 200)")
    args = parser.parse_args()

    global MAX_PER_JURISDICTION
    if args.target < 50:
        MAX_PER_JURISDICTION = max(5, args.target // 3)

    engine = get_engine()
    start = time.time()

    log.info("═" * 50)
    log.info("Entity Mention Sample Builder")
    log.info(f"  Target:    {args.target} items")
    log.info(f"  DB limit:  {args.limit}")
    log.info(f"  Seed:      {args.seed}")
    log.info(f"  Dry run:   {args.dry_run}")
    log.info(f"  Output:    {args.output}")
    log.info("═" * 50)

    # Fetch items
    items = fetch_agenda_items(engine, limit=args.limit)
    log.info(f"Fetched {len(items)} items from database")

    # Filter out trivial items
    min_chars = args.min_chars
    before = len(items)
    items = [it for it in items if it['char_count'] >= min_chars]
    log.info(f"After min_chars={min_chars} filter: {len(items)}/{before} items retained")

    # Stratified sampling
    sample = stratified_sample(items, target=args.target, seed=args.seed)

    if args.dry_run:
        log.info("Dry run — no output written.")
        return

    # Process for annotation
    if args.no_pre_annotate:
        annotation_records = []
        for idx, item in enumerate(sample, 1):
            annotation_records.append({
                "sample_id": idx,
                "item_id": item["id"],
                "title": item["title"],
                "text": item["text"],
                "char_count": item["char_count"],
                "density_score": item["density_score"],
                "entity_spans": [],
                "annotation_status": "unreviewed",
                "jurisdiction": item["jurisdiction"],
                "meeting_type": item["meeting_type_normalized"],
                "meeting_date": item["meeting_date"],
                "meeting_title": item["meeting_title"],
                "body_code": item["body_code"],
                "body_name": item["body_name"],
                "item_number": item["item_number"],
                "item_type": item["item_type"],
                "meeting_db_id": item["meeting_db_id"],
                "stratum": item.get("_stratum", ""),
                "human_spans": [],
                "notes": "",
            })
    else:
        annotation_records = process_mention_sample(sample)

    # Add timing metadata
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_size": args.target,
        "actual_size": len(annotation_records),
        "seed": args.seed,
        "pre_annotated": not args.no_pre_annotate,
        "schema_version": 1,
        "total_items_in_corpus": len(items),
    }

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "meta": meta,
            "items": annotation_records,
        }, f, indent=2, default=str)
    log.info(f"Wrote {len(annotation_records)} items to {output_path.resolve()}")

    # Summary
    log.info("─" * 50)
    log.info("Annotation summary:")
    log.info(f"  Items:           {len(annotation_records)}")
    log.info(f"  Pre-annotated:   {not args.no_pre_annotate}")
    total_spans = sum(len(r["entity_spans"]) for r in annotation_records)
    log.info(f"  Auto spans:      {total_spans}")
    annotated = sum(1 for r in annotation_records if r["human_spans"])
    log.info(f"  Human-reviewed:  {annotated}")

    elapsed = time.time() - start
    log.info(f"Total time: {elapsed:.1f}s ({elapsed / 60:.1f} min)")


if __name__ == "__main__":
    main()
