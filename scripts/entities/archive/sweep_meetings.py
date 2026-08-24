#!/usr/bin/env python3
"""
sweep_meetings.py — Full sweep of all meeting agenda items for entities.

Phase 1 of the entity-mining plan: process every agenda_item across all 211
bodies and extract entities via:

  1. Body-specific labeled-field patterns (Applicant:, Attorney:, etc.)
  2. General labeled-field patterns (universal fallback)
  3. Known organization seed-list matching (titles + text)
  4. Case / reference number extraction

Idempotent: skips items that already have entity mentions unless --force.
Records per-body watermarks in _pattern_cascade_watermark.

Usage:
  DATABASE_URL=postgresql://... python scripts/entities/sweep_meetings.py
  DATABASE_URL=postgresql://... python scripts/entities/sweep_meetings.py --force
  DATABASE_URL=postgresql://... python scripts/entities/sweep_meetings.py --dry-run
  DATABASE_URL=postgresql://... python scripts/entities/sweep_meetings.py --body bos
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

log = logging.getLogger("sweep_meetings")
WATERMARK_TABLE = "_pattern_cascade_watermark"
BATCH_SIZE = 500
MAX_MATCH_LEN = 100


# ═══════════════════════════════════════════════════════════════════════════
#  Known organizations (seed list from extract.py)
# ═══════════════════════════════════════════════════════════════════════════

KNOWN_ORGANIZATIONS: dict[str, str] = {
    "Taylor Morrison": "developer",
    "Lennar": "developer",
    "Pulte Homes": "developer",
    "KB Home": "developer",
    "D.R. Horton": "developer",
    "Shea Homes": "developer",
    "Toll Brothers": "developer",
    "Fulton Homes": "developer",
    "Meritage Homes": "developer",
    "Richmond American Homes": "developer",
    "Beazer Homes": "developer",
    "Centex": "developer",
    "Clayton Homes": "developer",
    "Woodside Homes": "developer",
    "Ashton Woods": "developer",
    "M.D.C. Holdings": "developer",
    "LGI Homes": "developer",
    "Dream Finders Homes": "developer",
    "Landsea Homes": "developer",
    "Trilogy": "developer",
    "Viking Development": "developer",
    "SimonCRE": "developer",
    "Origis Development": "developer",
    "Plus Power": "developer",
    "Avantus": "developer",
    "Recurrent Energy": "developer",
    "RWE": "developer",
    "DCR Transmission": "developer",
    "Gust Rosenfeld": "law_firm",
    "Tiffany & Bosco": "law_firm",
    "Snell & Wilmer": "law_firm",
    "Rose Law Group": "law_firm",
    "Quarles & Brady": "law_firm",
    "Gammage & Burnham": "law_firm",
    "Burch & Cracchiolo": "law_firm",
    "May Potenza Baran & Gillespie": "law_firm",
    "Withey Morris Baugh": "law_firm",
    "Berry Riddell": "law_firm",
    "Pew & Lake": "law_firm",
    "Bergin Frakes Smalley Oberholtzer": "law_firm",
    "Smalley & Oberholtzer": "law_firm",
    "RVi Planning + Landscape Architecture": "planning_firm",
    "Logan Simpson": "planning_firm",
    "Kimley-Horn": "planning_firm",
    "Norris Design": "planning_firm",
    "Huitt-Zollars": "planning_firm",
    "EPS Group": "planning_firm",
    "Pinnacle Consulting": "planning_firm",
    "CVL Consultants": "planning_firm",
    "IPlan Consulting": "planning_firm",
    "Anderson Development Engineering": "planning_firm",
    "Keogh Engineering": "planning_firm",
    "Edifice Architecture": "planning_firm",
    "Arizona Public Service": "utility",
    "Salt River Project": "utility",
    "Southwest Gas": "utility",
    "Save Our Scottsdale": "advocacy_group",
}


# ═══════════════════════════════════════════════════════════════════════════
#  Patterns
# ═══════════════════════════════════════════════════════════════════════════

def _line_field(label: str) -> re.Pattern:
    """Label: Value  (line-level; stops at newline or next label)."""
    return re.compile(rf"{label}:\s*(.+?)(?:\n|$)", re.I | re.M)


# Body-specific patterns (extends pattern_cascade.py)
BODY_SPECIFIC_PATTERNS: dict[str, list[tuple[str, str, re.Pattern]]] = {
    "bos": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    "pz": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "attorney", _line_field("Attorney")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    "phoenix-cc": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "representative", _line_field("Representative")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    "phoenix-pc": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "representative", _line_field("Representative")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    "phoenix-ti": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "representative", _line_field("Representative")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    "phoenix-ps": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "representative", _line_field("Representative")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    "phoenix-ed": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "representative", _line_field("Representative")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
}

# General universal patterns — tried on every body
GENERAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("applicant", re.compile(
        r"(?:Applicant|Applicant/Owner|Applicant/Agent|Petitioner)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("attorney", re.compile(
        r"(?:Attorney|Represented by|Represented By|Counsel|Representative)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("staff", re.compile(
        r"(?:Staff Contact|Staff|Presenter|Prepared by|Contact)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("owner", re.compile(
        r"(?:Owner|Property Owner|Landowner)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
]

# Case / reference number pattern
CASE_NUMBER_RE = re.compile(
    r"\b(ZON|PLN|CU|SPR|CPA|MCP|SPL|USE|Z|P|CASE)[-\s]?\d{2,}[-]\d{2,}\b",
    re.I,
)

# BOS-style case number: C-XX-XX-XXX-X-00 (more specific to avoid backtracking)
BOS_CASE_RE = re.compile(
    r"\bC-\d{2}-\d{2}-\d{3}-[A-Z0-9]+-[A-Z0-9]+\b", re.I,
)

# Build known-org alternation pattern
KNOWN_ORG_RE = re.compile(
    r"(" + "|".join(re.escape(name) for name in KNOWN_ORGANIZATIONS) + r")",
    re.I,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Entity type heuristics
# ═══════════════════════════════════════════════════════════════════════════

FIRM_KEYWORDS = frozenset({
    "llc", "inc", "plc", "ltd", "corp", "corporation", "company",
    "group", "firm", "partnership", "consulting", "planning", "engineering",
    "law ", "office", "pa ", "pc ", "llp", "association", "incorporated",
    "development", "properties", "management", "services", "architecture",
    "construction", "design", "homes", "church", "assembly of god",
    "university", "hospital", "district", "department", "commission",
    "committee", "board of", "city of", "town of", "county of",
    "architect", "attorney", "engineer", "solutions",
})


def classify_entity_type(name: str) -> str:
    """Heuristic: person vs organization."""
    n = name.lower().strip()
    if any(kw in n for kw in FIRM_KEYWORDS):
        return "organization"
    if "," in name:
        return "organization"
    words = name.split()
    if len(words) <= 1:
        return "organization"
    return "person"


def normalize_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip())
    # Strip legal suffixes
    name = re.sub(
        r"\s+(P\.?L\.?C\.?|P\.?L\.?L\.?C\.?|P\.?C\.?|P\.?A\.?|L\.?L\.?C\.?|"
        r"I\.?N\.?C\.?|L\.?T\.?D\.?|C\.?O\.?R\.?P\.?|L\.?L\.?P\.?|C\.?O\.?)\.?\s*$",
        "", name, flags=re.I,
    )
    name = name.replace("&", " and ")
    name = re.sub(r"[^\w\s'\-]", "", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^the\s+", "", name)
    return name


def validate_name(name: str) -> bool:
    if len(name) > MAX_MATCH_LEN or len(name) < 2:
        return False
    filler_starts = {"the ", "and ", "to ", "for ", "of ", "a ", "an ", "in ", "on ", "is "}
    if any(name.lower().startswith(f) for f in filler_starts):
        return False
    return True


# ── Body group resolution ───────────────────────────────────────────────

def find_body_group(body: str) -> str | None:
    if body in BODY_SPECIFIC_PATTERNS:
        return body
    if body.endswith("-pz"):
        return "pz"
    if body.startswith("phoenix-") and "phoenix-cc" in BODY_SPECIFIC_PATTERNS:
        return "phoenix-cc"
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Extraction logic
# ═══════════════════════════════════════════════════════════════════════════

ROLE_EDGE_MAP = {
    "applicant": "HAS_APPLICANT",
    "attorney": "HAS_ATTORNEY",
    "representative": "HAS_ATTORNEY",
    "owner": "HAS_OWNER",
    "staff": "HAS_STAFF",
    "presenter": "HAS_STAFF",
}


def extract_entities_from_item(
    item_id: int,
    body: str,
    title: str,
    text: str,
    known_org_pattern: re.Pattern,
) -> list[dict]:
    """Run all extractors against one agenda item. Returns list of candidate dicts."""
    candidates: list[dict] = []
    seen_entity_keys: set[tuple[str, str]] = set()
    seen_roles: set[str] = set()

    # Text is already truncated before this call
    combined = f"{title}\n{text}"

    def _add_candidate(name: str, etype: str, role: str, confidence: int = 85):
        key = (normalize_name(name), etype)
        if key in seen_entity_keys:
            return
        seen_entity_keys.add(key)
        candidates.append({
            "name": name[:500],
            "normalized": key[0],
            "entity_type": etype,
            "role": role,
            "confidence": confidence,
            "source": "pattern",
        })

    # 1. Body-specific patterns
    body_group = find_body_group(body)
    if body_group:
        for field_name, role, pattern in BODY_SPECIFIC_PATTERNS[body_group]:
            if role in seen_roles:
                continue
            m = pattern.search(combined)
            if m:
                actor = m.group(1).strip()
                if validate_name(actor):
                    etype = classify_entity_type(actor)
                    _add_candidate(actor, etype, role, 90)
                    seen_roles.add(role)

    # 2. General patterns
    for role, pattern in GENERAL_PATTERNS:
        if role in seen_roles:
            continue
        for m in pattern.finditer(combined):
            actor = m.group(1).strip()
            if validate_name(actor):
                etype = classify_entity_type(actor)
                _add_candidate(actor, etype, role, 80)
                seen_roles.add(role)

    # 3. Known organization matching (title + text)
    for m in known_org_pattern.finditer(combined):
        name = m.group(1).strip()
        etype = KNOWN_ORGANIZATIONS.get(name, "organization")
        _add_candidate(name, etype, "known_org", 95)

    # 4. Case / reference numbers
    for pattern in [CASE_NUMBER_RE, BOS_CASE_RE]:
        for m in pattern.finditer(combined):
            case_str = m.group(0).strip()
            norm = normalize_name(case_str)
            if (norm, "case") not in seen_entity_keys:
                seen_entity_keys.add((norm, "case"))
                candidates.append({
                    "name": case_str.upper()[:500],
                    "normalized": norm,
                    "entity_type": "case",
                    "role": "reference",
                    "confidence": 95,
                    "source": "pattern",
                })

    return candidates


# ═══════════════════════════════════════════════════════════════════════════
#  IGA / intergovernmental agreement extraction
# ═══════════════════════════════════════════════════════════════════════════

# Title patterns that identify IGA/MOU/JPA items
IGA_TITLE_PATTERNS = [
    re.compile(r"intergovernmental\s+agreement", re.I),
    re.compile(r"\bIGA\b", re.I),
    re.compile(r"memorandum\s+of\s+understanding", re.I),
    re.compile(r"\bMOU\b", re.I),
    re.compile(r"joint\s+powers\s+agreement", re.I),
    re.compile(r"\bJPA\b", re.I),
    re.compile(r"agreement\s+(between|with)\s+(\w+)", re.I),
    re.compile(r"cooperative\s+purchasing\s+agreement", re.I),
    re.compile(r"mutual\s+aid\s+agreement", re.I),
]

# Extract counterparty from "with [Name]" or "between [A] and [B]"
IGA_COUNTERPARTY_PATTERNS = [
    # "with [Name]" — grab to end or next dash/pipe
    re.compile(r"(?:with|by)\s+([A-Z][A-Za-z0-9 .&'-]+?)(?:\s*[–—-]|\s*\||\s*\(|\s*$)", re.I),
    # "between [A] and [B]" — grab B
    re.compile(r"between\s+[^.]+?\s+and\s+([A-Z][A-Za-z0-9 .&'-]+?)(?:\s*[–—-]|\s*\(|\s*$)", re.I),
    # "Agreement with [Name] (Agreement No.)"
    re.compile(r"agreement\s+(?:no\.?|number)?\s*[\d-]+\s+(?:with|between)\s+([A-Z][A-Za-z0-9 .&'-]+?)(?:\s*[–—-]|\s*\(|\s*$)", re.I),
]

# Items to skip — not real counterparties
IGA_SKIP_COUNTERPARTIES = frozenset({
    "the county", "the city", "the town", "the state",
    "maricopa county", "this agreement", "the agreement",
    "applicant", "no", "n/a",
})

# Government suffixes to help classify entities
GOVT_SUFFIXES = {
    "city of", "town of", "county of", "village of",
    "state of", "district", "department of", "board of",
    "unified school", "elementary school", "high school",
    "fire district", "water district", "sanitation district",
    "community college", "university of",
    "maricopa", "arizona",
}


def classify_government_entity(name: str) -> bool:
    """Heuristic: check if name looks like a government entity."""
    n = name.lower().strip()
    if any(n.startswith(g) for g in GOVT_SUFFIXES):
        return True
    if any(n.endswith(g) for g in ["city", "county", "town", "village", "district"]):
        return True
    return False


def extract_iga_from_item(
    item_id: int,
    body: str,
    title: str,
    text: str,
) -> list[dict]:
    """Detect IGA items and extract counterparty entities.

    Returns candidate dicts with role='iga_counterparty' if the
    item is an intergovernmental agreement. Also records the IGA
    indicator as a flag on the candidate.
    """
    combined = f"{title}\
{text}"
    candidates: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    # Step 1: Is this an IGA item?
    is_iga = any(p.search(title) for p in IGA_TITLE_PATTERNS)
    if not is_iga:
        return candidates

    # Step 2: Extract counterparty from title (preferred) or text summary
    counterparty = None
    for pat in IGA_COUNTERPARTY_PATTERNS:
        m = pat.search(title)
        if m:
            raw = m.group(1).strip().rstrip(".;,")
            if raw.lower() not in IGA_SKIP_COUNTERPARTIES and len(raw) > 3:
                counterparty = raw
                break

    if not counterparty:
        # Fallback: try in text (first 200 chars)
        for pat in IGA_COUNTERPARTY_PATTERNS:
            m = pat.search(text[:800])
            if m:
                raw = m.group(1).strip().rstrip(".;,")
                if raw.lower() not in IGA_SKIP_COUNTERPARTIES and len(raw) > 3:
                    counterparty = raw
                    break

    if not counterparty:
        # IGA detected but couldn't parse counterparty — still record it
        return candidates

    # Step 3: Create candidate dict for the counterparty
    etype = "organization"
    is_gov = classify_government_entity(counterparty)
    norm = normalize_name(counterparty)
    key = (norm, etype)

    if key not in seen_keys:
        seen_keys.add(key)
        candidates.append({
            "name": counterparty[:500],
            "normalized": norm,
            "entity_type": etype,
            "role": "iga_counterparty",
            "confidence": 85,
            "source": "iga_detection",
            "_is_government": is_gov,
            "_iga_item": True,
        })

    return candidates


# ═══════════════════════════════════════════════════════════════════════════
#  Body processor
# ═══════════════════════════════════════════════════════════════════════════

def process_body(conn, body: str, wm: int, entity_cache: dict,
                 force: bool = False, dry_run: bool = False,
                 verbose: bool = False) -> dict:
    """Process one batch of agenda items for a body.

    Normal mode: uses swept_at IS NULL for idempotency.
    Force mode: ignores swept_at, uses watermark id for pagination.
    Updates swept_at = NOW() on each processed row after extraction.
    """
    if force:
        query = f"""
            SELECT ai.id, COALESCE(ai.agenda_item_title, ''),
                   COALESCE(ai.agenda_item_text, '')
            FROM agenda_items ai
            WHERE ai.id > :wm AND ai.body = :body
            ORDER BY ai.id LIMIT :limit
        """
    else:
        query = f"""
            SELECT ai.id, COALESCE(ai.agenda_item_title, ''),
                   COALESCE(ai.agenda_item_text, '')
            FROM agenda_items ai
            WHERE ai.swept_at IS NULL AND ai.body = :body
              AND ai.id > :wm
            ORDER BY ai.id LIMIT :limit
        """
        if dry_run:
            # In dry-run mode, paginate via watermark since swept_at isn't updated
            pass
        else:
            wm = 0  # Normal mode: don't filter by watermark, rely on swept_at

    rows = conn.execute(
        __import__("sqlalchemy").text(query),
        {"wm": wm, "body": body, "limit": BATCH_SIZE},
    ).fetchall()

    if not rows:
        return {"processed": 0, "matches": 0, "entities": 0, "mentions": 0,
                "max_id": wm, "done": True}

    log.info("  %s: scanning %d items (from id %d)", body, len(rows), wm)

    # ── Phase 1: Collect ALL candidates (no DB writes) ────────────
    known_org_pattern = KNOWN_ORG_RE
    all_candidates: list[dict] = []
    max_id = wm
    for r in rows:
        item_id = int(r[0])
        max_id = max(max_id, item_id)
        title = str(r[1] or "")[:2000]
        text = str(r[2] or "")[:5000]
        try:
            cs = extract_entities_from_item(item_id, body, title, text,
                                             known_org_pattern)
            if cs:
                for c in cs:
                    c["_source_id"] = item_id
                all_candidates.extend(cs)

            # IGA detection (counterparty extraction)
            iga_cs = extract_iga_from_item(item_id, body, title, text)
            if iga_cs:
                for c in iga_cs:
                    c["_source_id"] = item_id
                all_candidates.extend(iga_cs)
        except Exception:
            log.warning("  %s item %d: extraction error, skipping", body, item_id)

    if dry_run:
        return {"processed": len(rows), "matches": len(all_candidates),
                "entities": 0, "mentions": 0, "max_id": max_id, "done": False}

    if not all_candidates:
        # Mark all items as swept even if no entities found
        batch_ids = [int(r[0]) for r in rows]
        id_list = ", ".join(str(i) for i in batch_ids)
        conn.execute(
            __import__("sqlalchemy").text(f"""
                UPDATE agenda_items
                SET swept_at = now()
                WHERE id IN ({id_list})
            """)
        )
        return {"processed": len(rows), "matches": 0, "entities": 0,
                "mentions": 0, "max_id": max_id, "done": False}

    # ── Phase 2: Bulk-create entities (deduplicated) ──────────────
    total_matches = 0
    total_entities = 0
    total_mentions = 0

    # Collect unique entity keys not yet in cache
    need_create: dict[str, tuple[str, str, str, bool]] = {}  # key -> (name, nn, etype, is_gov)
    for c in all_candidates:
        key = f"{c['normalized']}|{c['entity_type']}"
        if key not in entity_cache and key not in need_create:
            is_gov = bool(c.get("_is_government", False))
            need_create[key] = (c['name'][:500], c['normalized'], c['entity_type'], is_gov)

    # Create new entities (multi-row INSERT)
    if need_create:
        items = list(need_create.items())
        chunk_size = 100
        for ci in range(0, len(items), chunk_size):
            chunk_items = items[ci:ci + chunk_size]
            value_rows = []
            row_params = {}
            for j, (key, (name, nn, etype, is_gov)) in enumerate(chunk_items):
                suffix = f"_{j}"
                value_rows.append(
                    f"(:et{suffix}, :name{suffix}, :nn{suffix}, :gov{suffix}, "
                    f"now(), now(), 1, now(), now())"
                )
                row_params.update({
                    f"et{suffix}": etype,
                    f"name{suffix}": name,
                    f"nn{suffix}": nn,
                    f"gov{suffix}": is_gov,
                })
            sql = (
                "INSERT INTO entities "
                "(entity_type, name, normalized_name, is_government, "
                "first_seen_at, last_seen_at, mention_count, "
                "created_at, updated_at) "
                f"VALUES {', '.join(value_rows)} "
                "ON CONFLICT (normalized_name, entity_type) DO UPDATE SET "
                "mention_count = entities.mention_count + 1, "
                "last_seen_at = now() "
                "RETURNING id, normalized_name, entity_type"
            )
            try:
                result_rows = conn.execute(
                    __import__("sqlalchemy").text(sql), row_params
                ).fetchall()
                for r in result_rows:
                    eid, nn, etype = int(r[0]), str(r[1]), str(r[2])
                    entity_cache[f"{nn}|{etype}"] = eid
                    total_entities += 1
            except Exception:
                # Fall back to individual inserts for any problematic rows
                for key, (name, nn, etype, is_gov) in chunk_items:
                    if key in entity_cache:
                        continue
                    try:
                        row = conn.execute(
                            __import__("sqlalchemy").text("""
                                INSERT INTO entities
                                    (entity_type, name, normalized_name, is_government,
                                     first_seen_at, last_seen_at, mention_count,
                                     created_at, updated_at)
                                VALUES (:et, :name, :nn, :gov,
                                        now(), now(), 1, now(), now())
                                ON CONFLICT (normalized_name, entity_type) DO UPDATE SET
                                    mention_count = entities.mention_count + 1,
                                    last_seen_at = now()
                                RETURNING id
                            """),
                            {"et": etype, "name": name, "nn": nn, "gov": is_gov},
                        ).fetchone()
                        entity_cache[key] = row[0]
                        total_entities += 1
                    except Exception:
                        existing = conn.execute(
                            __import__("sqlalchemy").text(
                                "SELECT id FROM entities "
                                "WHERE normalized_name = :nn AND entity_type = :et"
                            ),
                            {"nn": nn, "et": etype},
                        ).fetchone()
                        if existing:
                            entity_cache[key] = existing[0]
                            total_entities += 1

    # ── Phase 3: Pre-load existing mentions for this batch ────────
    source_ids = list({c["_source_id"] for c in all_candidates})
    existing_mentions: set[tuple[int, int, str]] = set()
    if len(source_ids) <= 100:
        placeholders = ", ".join(str(s) for s in source_ids)
        existing_rows = conn.execute(
            __import__("sqlalchemy").text(f"""
                SELECT entity_id, source_id, role_in_context
                FROM entity_mentions
                WHERE source_type = 'agenda_item'
                  AND source_id IN ({placeholders})
            """)
        ).fetchall()
        for er in existing_rows:
            existing_mentions.add((int(er[0]), int(er[1]), str(er[2])))

    # ── Phase 4: Bulk-insert new mentions (collected, then batch INSERT) ─
    mention_params: list[dict] = []
    for c in all_candidates:
        key = f"{c['normalized']}|{c['entity_type']}"
        eid = entity_cache.get(key)
        if eid is None:
            continue
        sid = c["_source_id"]
        role = c["role"]
        if (eid, sid, role) in existing_mentions:
            continue
        existing_mentions.add((eid, sid, role))
        mention_params.append({
            "eid": eid, "sid": sid,
            "mt": c["name"][:500], "cs": c["name"][:300],
            "conf": c["confidence"], "role": role,
        })

    if mention_params:
        # Multi-row INSERT (one round-trip per chunk)
        chunk_size = 50
        for i in range(0, len(mention_params), chunk_size):
            chunk = mention_params[i:i + chunk_size]
            # Build multi-row VALUES
            value_rows = []
            row_params = {}
            for j, p in enumerate(chunk):
                suffix = f"_{j}"
                value_rows.append(
                    f"(:eid{suffix}, 'agenda_item', :sid{suffix}, :mt{suffix}, "
                    f":cs{suffix}, :conf{suffix}, 'sweep_meetings', :role{suffix}, now())"
                )
                row_params.update({
                    f"eid{suffix}": p["eid"],
                    f"sid{suffix}": p["sid"],
                    f"mt{suffix}": p["mt"],
                    f"cs{suffix}": p["cs"],
                    f"conf{suffix}": p["conf"],
                    f"role{suffix}": p["role"],
                })
            sql = (
                "INSERT INTO entity_mentions "
                "(entity_id, source_type, source_id, mention_text, "
                "context_snippet, confidence, extracted_by, "
                "role_in_context, created_at) "
                f"VALUES {', '.join(value_rows)}"
            )
            conn.execute(__import__("sqlalchemy").text(sql), row_params)
        total_mentions += len(mention_params)
        total_matches += len(mention_params)

    # ── Phase 5: Mark items as swept (all fetched items) ──
    if not dry_run:
        batch_ids = [int(r[0]) for r in rows]
        for chunk_start in range(0, len(batch_ids), 100):
            chunk = batch_ids[chunk_start:chunk_start + 100]
            id_list = ", ".join(str(i) for i in chunk)
            conn.execute(
                __import__("sqlalchemy").text(f"""
                    UPDATE agenda_items
                    SET swept_at = now()
                    WHERE id IN ({id_list})
                """)
            )

    return {"processed": len(rows), "matches": total_matches,
            "entities": total_entities, "mentions": total_mentions,
            "max_id": max_id, "done": False}


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
#  IGA-only sweep (retroactive pass on already-swept items)
# ═══════════════════════════════════════════════════════════════════════════

IGA_TITLE_MATCH_SQL = """
    SELECT ai.id, COALESCE(ai.agenda_item_title, ''),
           COALESCE(ai.agenda_item_text, '')
    FROM agenda_items ai
    WHERE ai.body = :body AND ai.swept_at IS NOT NULL
      AND ai.id > :wm
      AND NOT EXISTS (
          SELECT 1 FROM entity_mentions em
          WHERE em.source_type = 'agenda_item'
            AND em.source_id = ai.id
            AND em.role_in_context = 'iga_counterparty'
      )
      AND (
          LOWER(COALESCE(ai.agenda_item_title, '')) ~ 'intergovernmental|\\biga\\b|memorandum of understanding|\\bmou\\b|joint powers|\\bjpa\\b|cooperative purchasing|mutual aid agreement'
      )
    ORDER BY ai.id
    LIMIT :limit
"""


def run_iga_sweep(engine, entity_cache, watermarks, all_bodies, args):
    """Retroactive IGA-only sweep: find IGA items that are already swept
    but don't yet have IGA counterparty mentions, and add them."""
    log.info("  IGA sweep scanning %d bodies...", len(all_bodies))
    grand_total = {"processed": 0, "matches": 0, "entities": 0, "mentions": 0}
    bodies_iga_found = 0

    for body in sorted(all_bodies):
        wm = 0  # Don't use watermarks — we're doing a title-match pass, not id-based
        body_total = {"processed": 0, "matches": 0, "entities": 0, "mentions": 0}
        body_done = False

        while not body_done:
            try:
                with engine.begin() as conn:
                    rows = conn.execute(
                        __import__("sqlalchemy").text(IGA_TITLE_MATCH_SQL),
                        {"body": body, "wm": wm, "limit": BATCH_SIZE},
                    ).fetchall()

                    if not rows:
                        body_done = True
                        continue

                    max_id = max(int(r[0]) for r in rows)
                    wm = max_id
                    body_total["processed"] += len(rows)

                    if args.verbose:
                        log.info("  %s: scanning %d items for IGAs (id range %d-%d)",
                                 body, len(rows), rows[0][0], rows[-1][0])

                    # Collect IGA candidates for all items in this batch
                    batch_candidates: list[dict] = []
                    for r in rows:
                        item_id, title, text_content = int(r[0]), str(r[1] or ""), str(r[2] or "")
                        try:
                            iga_cs = extract_iga_from_item(
                                item_id, body, title, text_content
                            )
                            if iga_cs:
                                for c in iga_cs:
                                    c["_source_id"] = item_id
                                batch_candidates.extend(iga_cs)
                        except Exception:
                            log.warning("  %s item %d: IGA extraction error", body, item_id)

                    if not batch_candidates:
                        continue

                    if args.dry_run:
                        body_total["matches"] += len(batch_candidates)
                        continue

                    # ── Create entities + mentions for this batch ──
                    # Collect unique entities
                    need_create: dict[str, tuple[str, str, str, bool]] = {}
                    for c in batch_candidates:
                        key = f"{c['normalized']}|{c['entity_type']}"
                        if key not in entity_cache and key not in need_create:
                            is_gov = bool(c.get("_is_government", False))
                            need_create[key] = (c['name'][:500], c['normalized'], c['entity_type'], is_gov)

                    # Create entities
                    if need_create:
                        items = list(need_create.items())
                        for ci in range(0, len(items), 100):
                            chunk = items[ci:ci + 100]
                            value_rows = []
                            row_params = {}
                            for j, (key, (name, nn, etype_val, is_gov)) in enumerate(chunk):
                                suffix = f"_{j}"
                                value_rows.append(f"(:et{suffix}, :name{suffix}, :nn{suffix}, :gov{suffix}, now(), now(), 1, now(), now())")
                                row_params.update({
                                    f"et{suffix}": etype_val,
                                    f"name{suffix}": name,
                                    f"nn{suffix}": nn,
                                    f"gov{suffix}": is_gov,
                                })
                            sql = (
                                "INSERT INTO entities "
                                "(entity_type, name, normalized_name, is_government, "
                                "first_seen_at, last_seen_at, mention_count, "
                                "created_at, updated_at) "
                                f"VALUES {', '.join(value_rows)} "
                                "ON CONFLICT (normalized_name, entity_type) DO UPDATE SET "
                                "mention_count = entities.mention_count + 1, "
                                "last_seen_at = now() "
                                "RETURNING id, normalized_name, entity_type"
                            )
                            try:
                                result_rows = conn.execute(
                                    __import__("sqlalchemy").text(sql), row_params
                                ).fetchall()
                                for r in result_rows:
                                    eid, nn, et = int(r[0]), str(r[1]), str(r[2])
                                    entity_cache[f"{nn}|{et}"] = eid
                                    body_total["entities"] += 1
                            except Exception:
                                for key, (name, nn, etype_val, is_gov) in chunk:
                                    if key in entity_cache:
                                        continue
                                    row = conn.execute(
                                        __import__("sqlalchemy").text("""
                                            INSERT INTO entities
                                                (entity_type, name, normalized_name, is_government,
                                                 first_seen_at, last_seen_at, mention_count,
                                                 created_at, updated_at)
                                            VALUES (:et, :name, :nn, :gov,
                                                    now(), now(), 1, now(), now())
                                            ON CONFLICT (normalized_name, entity_type) DO UPDATE SET
                                                mention_count = entities.mention_count + 1,
                                                last_seen_at = now()
                                            RETURNING id
                                        """),
                                        {"et": etype_val, "name": name, "nn": nn, "gov": is_gov},
                                    ).fetchone()
                                    entity_cache[key] = row[0]
                                    body_total["entities"] += 1

                    # Create mentions
                    mention_params = []
                    for c in batch_candidates:
                        key = f"{c['normalized']}|{c['entity_type']}"
                        eid = entity_cache.get(key)
                        if eid is None:
                            continue
                        mention_params.append({
                            "eid": eid, "sid": c["_source_id"],
                            "mt": c["name"][:500], "cs": c["name"][:300],
                            "conf": c["confidence"], "role": c["role"],
                        })

                    if mention_params:
                        for i in range(0, len(mention_params), 50):
                            chunk = mention_params[i:i + 50]
                            vrows = []
                            rparams = {}
                            for j, p in enumerate(chunk):
                                s = f"_{j}"
                                vrows.append(f"(:eid{s}, 'agenda_item', :sid{s}, :mt{s}, :cs{s}, :conf{s}, 'sweep_meetings', :role{s}, now())")
                                rparams.update({
                                    f"eid{s}": p["eid"], f"sid{s}": p["sid"],
                                    f"mt{s}": p["mt"], f"cs{s}": p["cs"],
                                    f"conf{s}": p["conf"], f"role{s}": p["role"],
                                })
                            conn.execute(
                                __import__("sqlalchemy").text(
                                    "INSERT INTO entity_mentions "
                                    "(entity_id, source_type, source_id, mention_text, "
                                    "context_snippet, confidence, extracted_by, "
                                    "role_in_context, created_at) "
                                    f"VALUES {', '.join(vrows)}"
                                ),
                                rparams,
                            )
                        body_total["matches"] += len(mention_params)
                        body_total["mentions"] += len(mention_params)

            except Exception as e:
                log.error("  %s IGA batch error at wm=%d: %s", body, wm, e, exc_info=args.verbose)
                break

        for k in grand_total:
            grand_total[k] += body_total[k]
        if body_total["matches"] > 0:
            bodies_iga_found += 1
            log.info("  %s: %d items, %d IGA matches, %d entities, %d mentions",
                     body, body_total["processed"], body_total["matches"],
                     body_total["entities"], body_total["mentions"])

    mode = "DRY RUN" if args.dry_run else "DONE"
    log.info("IGA sweep %s — %d items, %d matches, %d entities, %d mentions across %d bodies",
             mode, grand_total["processed"], grand_total["matches"],
             grand_total["entities"], grand_total["mentions"], bodies_iga_found)


def main():
    parser = argparse.ArgumentParser(
        description="Sweep all meetings for entity extraction")
    parser.add_argument("--body", type=str, help="Process only matching body (substring)")
    parser.add_argument("--force", action="store_true",
                        help="Re-process items even if they have mentions")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Items per batch (default 500)")
    parser.add_argument("--batch-timeout", type=int, default=120,
                        help="Max seconds per batch (default 120)")
    parser.add_argument("--body-timeout", type=int, default=600,
                        help="Max seconds per body (default 600)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retries on transient error (default 3)")
    parser.add_argument("--iga-only", action="store_true",
                        help="Retroactive IGA detection on already-swept items")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S",
                        stream=sys.stdout,
                        force=True)

    from db.config import DATABASE_URL as _DB_URL
    from sqlalchemy import create_engine

    # Apply batch size override before any process_body calls
    global BATCH_SIZE
    BATCH_SIZE = args.batch_size

    engine = create_engine(
        _DB_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        future=True,
    )

    # Ensure watermark table
    with engine.begin() as conn:
        conn.execute(__import__("sqlalchemy").text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                body VARCHAR(64) PRIMARY KEY,
                last_run_at TIMESTAMPTZ DEFAULT now(),
                last_processed_id INTEGER DEFAULT 0,
                items_processed INTEGER DEFAULT 0,
                entities_created INTEGER DEFAULT 0,
                edges_created INTEGER DEFAULT 0
            );
        """))

    # Load entity cache
    entity_cache: dict[str, int] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT normalized_name, entity_type, id FROM entities"
            )
        ).fetchall()
        for r in rows:
            entity_cache[f"{str(r[0])}|{str(r[1])}"] = int(r[2])
    log.info("Loaded %d entities into cache", len(entity_cache))

    # Load watermarks
    watermarks: dict[str, int] = {}
    if not args.force:
        with engine.connect() as conn:
            wrows = conn.execute(
                __import__("sqlalchemy").text(
                    f"SELECT body, last_processed_id FROM {WATERMARK_TABLE}"
                )
            ).fetchall()
            watermarks = {r[0]: int(r[1]) for r in wrows}

    # Get bodies to process
    with engine.connect() as conn:
        all_bodies = [
            r[0] for r in conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT DISTINCT body FROM agenda_items ORDER BY body"
                )
            ).fetchall()
        ]

    if args.body:
        all_bodies = [b for b in all_bodies if args.body.lower() in b.lower()]

    if args.iga_only:
        log.info("IGA-only mode: scanning already-swept items for IGA counterparties")
        run_iga_sweep(engine, entity_cache, watermarks, all_bodies, args)
        return

    log.info("Found %d bodies to process", len(all_bodies))
    grand_total = {"processed": 0, "matches": 0, "entities": 0, "mentions": 0}
    bodies_skipped = 0
    bodies_errored = 0

    for body in sorted(all_bodies):
        wm = watermarks.get(body, 0)
        # Quick check: if body has no unscanned items, skip
        if not args.force:
            with engine.connect() as conn:
                remaining = conn.execute(
                    __import__("sqlalchemy").text(
                        "SELECT COUNT(*) FROM agenda_items "
                        "WHERE body = :body AND swept_at IS NULL"
                    ),
                    {"body": body},
                ).scalar()
                if remaining == 0:
                    if args.verbose:
                        log.info("  %s: skipped (all swept)", body)
                    bodies_skipped += 1
                    continue
                if args.verbose:
                    log.info("  %s: %d remaining, watermark=%d", body, remaining, wm)

        body_done = False
        body_total = {"processed": 0, "matches": 0, "entities": 0, "mentions": 0}
        body_start = time.time()
        consecutive_errors = 0

        while not body_done:
            # Check body-level timeout
            if time.time() - body_start > args.body_timeout:
                log.warning("  %s: body timeout after %ds (processed %d items)",
                            body, int(time.time() - body_start), body_total["processed"])
                break

            try:
                batch_start = time.time()
                with engine.begin() as conn:
                    stats = process_body(
                        conn, body, wm, entity_cache,
                        force=args.force, dry_run=args.dry_run,
                        verbose=args.verbose,
                    )

                batch_elapsed = time.time() - batch_start
                if batch_elapsed > args.batch_timeout:
                    log.warning("  %s: batch took %ds (timeout=%ds), marking slow",
                                body, int(batch_elapsed), args.batch_timeout)

                for k in body_total:
                    body_total[k] += stats.get(k, 0)

                body_done = stats.get("done", True)
                wm = stats.get("max_id", wm)

                # Reset error counter on success
                consecutive_errors = 0

                # Update watermark after each batch
                if not args.dry_run and stats["max_id"] > 0:
                    with engine.begin() as conn:
                        conn.execute(
                            __import__("sqlalchemy").text(f"""
                                INSERT INTO {WATERMARK_TABLE}
                                    (body, last_run_at, last_processed_id,
                                     items_processed, entities_created, edges_created)
                                VALUES (:body, now(), :mid, :ip, :ec, 0)
                                ON CONFLICT (body) DO UPDATE SET
                                    last_run_at = now(),
                                    last_processed_id = :mid,
                                    items_processed = {WATERMARK_TABLE}.items_processed + :ip,
                                    entities_created = {WATERMARK_TABLE}.entities_created + :ec
                            """),
                            {"body": body, "mid": stats["max_id"],
                             "ip": stats["processed"], "ec": stats["entities"]},
                        )

            except Exception as e:
                consecutive_errors += 1
                elapsed_in_body = int(time.time() - body_start)
                log.error("  ✗ %s batch error (attempt %d/%d, %ds into body): %s",
                          body, consecutive_errors, args.max_retries,
                          elapsed_in_body, e)

                if consecutive_errors >= args.max_retries:
                    log.error("  ✗ %s: exceeded max retries (%d), skipping body",
                              body, args.max_retries)
                    bodies_errored += 1
                    break

                # Exponential backoff: 2^attempt seconds
                backoff = 2 ** consecutive_errors
                log.info("  %s: backing off %ds before retry", body, backoff)
                time.sleep(backoff)

        if body_total["processed"] == 0 and consecutive_errors == 0:
            continue  # No work done, nothing to log

        # If body had work but body_done is still False after loop,
        # the body timed out and may have remaining items.
        if not body_done and not args.force:
            log.info("  %s: incomplete — %d items processed, %d matches, %d entities, %d mentions (%.0fs)",
                     body, body_total["processed"], body_total["matches"],
                     body_total["entities"], body_total["mentions"],
                     time.time() - body_start)
        elif body_total["matches"] > 0:
            log.info("  %s: %d items, %d matches, %d entities, %d mentions (%.0fs)",
                     body, body_total["processed"], body_total["matches"],
                     body_total["entities"], body_total["mentions"],
                     time.time() - body_start)

        for k in grand_total:
            grand_total[k] += body_total[k]

    mode = "DRY RUN" if args.dry_run else "DONE"
    parts = [f"{mode}",
             f"{grand_total['processed']} items",
             f"{grand_total['matches']} matches",
             f"{grand_total['entities']} entities",
             f"{grand_total['mentions']} mentions",
             f"{len(all_bodies)} bodies"]
    if bodies_skipped:
        parts.append(f"{bodies_skipped} skipped")
    if bodies_errored:
        parts.append(f"{bodies_errored} errored")
    log.info(" — ".join(parts))


if __name__ == "__main__":
    main()
