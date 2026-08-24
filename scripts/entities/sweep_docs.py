#!/usr/bin/env python3
"""
sweep_docs.py — Entity extraction from supporting_documents text_content.

Sweeps all supporting_documents that have extracted text (text_content IS NOT NULL)
and haven't been swept yet (swept_at IS NULL). Runs the same extractors as
sweep_meetings.py (patterns + known org matching) but against document text.

Idempotent: skips docs that already have swept_at set, or where text_content
is NULL/empty.

Usage:
  DATABASE_URL=postgresql://... python scripts/entities/sweep_docs.py
  DATABASE_URL=postgresql://... python scripts/entities/sweep_docs.py --dry-run
  DATABASE_URL=postgresql://... python scripts/entities/sweep_docs.py --limit 500
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from entities.entity_utils import (
    clean_normalized_name, normalize_entity_name, classify_entity_type,
)

log = logging.getLogger("sweep_docs")
WATERMARK_TABLE = "_sweep_docs_watermark"
BATCH_SIZE = 200

# ── Known organizations (same list as sweep_meetings.py) ────────────────

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

KNOWN_ORG_RE = re.compile(
    r"(" + "|".join(re.escape(name) for name in KNOWN_ORGANIZATIONS) + r")",
    re.I,
)

MAX_MATCH_LEN = 100




def normalize_name(name: str, entity_type: str | None = None) -> str:
    """Normalize an entity name for dedup.
    Delegates to shared utility from entity_utils.py.
    For person entities, strips titles for cleaner merging.
    """
    if entity_type == 'person':
        return clean_normalized_name(name)
    return normalize_entity_name(name)


def validate_name(name: str) -> bool:
    if len(name) > MAX_MATCH_LEN or len(name) < 2:
        return False
    filler_starts = {"the ", "and ", "to ", "for ", "of ", "a ", "an ", "in ", "on ", "is "}
    if any(name.lower().startswith(f) for f in filler_starts):
        return False
    return True


def clean_text(text: str) -> str:
    """Pre-process pdftotext output before entity extraction.

    - Joins hyphenated line breaks ("Assess-\nment" → "Assessment")
    - Normalizes multiple blank lines to single newlines
    """
    text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)
    text = re.sub(r'\n{3,}', r'\n\n', text)
    return text


def is_garbage_text(name: str) -> bool:
    """Return True if the extracted name is obviously not a real entity."""
    n = name.strip()
    if not n:
        return True

    # Section headers and boilerplate often extracted as names
    garbage_prefixes = (
        'recommendation', 'recommends', 'staff', 'applicant', 'owner',
        'attorney', 'council', 'notice', 'meeting', 'minutes',
        'attachment', 'appealed', 'occupied', 'membership',
        'announcement', 'memo', 'office', 'department', 'solicited',
        's and the', 's, and', ', and continue', ', the case',
        'by: ___', 'by: ____', 'by: _____',
    )
    n_lower = n.lower()
    for prefix in garbage_prefixes:
        if n_lower.startswith(prefix):
            return True

    # Single-word sentence fragments
    words = n.split()
    if len(words) <= 2:
        if n_lower[0].islower() and n[0].islower():
            return True
        if n_lower in ('memo', 'attendance', 'report', 'general', 'liaison',
                       'person', 'overview', 'presentation', 'summary'):
            return True

    # Looks like a truncated sentence fragment (comma + lowercase continuation)
    if n.startswith(',') and len(n) > 5:
        return True
    if n.startswith('.') and len(n) > 2:
        return True
    if n.startswith("'"):
        return True

    return False


GENERAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("applicant", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Applicant|Applicant/Owner|Applicant/Agent|Petitioner)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("attorney", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Attorney|Represented by|Represented By|Counsel|Representative)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("staff", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Staff Contact|Staff|Presenter|Prepared by|Contact)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("owner", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Owner|Property Owner|Landowner)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
]

CASE_NUMBER_RE = re.compile(
    r"\b(ZON|PLN|CU|SPR|CPA|MCP|SPL|USE|Z|P|CASE)[-\s]?\d{2,}[-]\d{2,}\b",
    re.I,
)

BOS_CASE_RE = re.compile(
    r"\bC-\d{2}-\d{2}-\d{3}-[A-Z0-9]+-[A-Z0-9]+\b", re.I,
)


# ── Extraction ──────────────────────────────────────────────────────────

def extract_entities_from_doc(text: str) -> list[dict]:
    """Run all extractors against document text. Returns list of candidate dicts."""
    candidates: list[dict] = []
    seen_entity_keys: set[tuple[str, str]] = set()

    text = clean_text(text)
    text_truncated = text[:8000]  # Limit to 8K chars

    def _add_candidate(name: str, etype: str, role: str, confidence: int = 85):
        key = (normalize_name(name, etype), etype)
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

    # 1. General patterns
    for role, pattern in GENERAL_PATTERNS:
        for m in pattern.finditer(text_truncated):
            actor = m.group(1).strip()
            if validate_name(actor) and not is_garbage_text(actor):
                etype = classify_entity_type(actor)
                _add_candidate(actor, etype, role, 80)

    # 2. Known organization matching
    for m in KNOWN_ORG_RE.finditer(text_truncated):
        name = m.group(1).strip()
        etype = KNOWN_ORGANIZATIONS.get(name, "organization")
        _add_candidate(name, etype, "known_org", 95)

    # 3. Case / reference numbers
    for pattern in [CASE_NUMBER_RE, BOS_CASE_RE]:
        for m in pattern.finditer(text_truncated):
            case_str = m.group(0).strip()
            norm = normalize_name(case_str, 'case')
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


# ── Batch processing ────────────────────────────────────────────────────

def process_batch(conn, wm: int, entity_cache: dict,
                  dry_run: bool = False, verbose: bool = False) -> dict:
    """Process one batch of supporting documents. Returns stats dict."""
    query = f"""
        SELECT sd.id, COALESCE(sd.document_title, ''), COALESCE(sd.text_content, '')
        FROM supporting_documents sd
        WHERE sd.swept_at IS NULL
          AND sd.text_content IS NOT NULL AND sd.text_content != ''
          AND sd.id > :wm
        ORDER BY sd.id
        LIMIT :limit
    """
    rows = conn.execute(
        __import__("sqlalchemy").text(query),
        {"wm": wm, "limit": BATCH_SIZE},
    ).fetchall()

    if not rows:
        return {"processed": 0, "matches": 0, "entities": 0, "mentions": 0,
                "max_id": wm, "done": True}

    max_id = max(int(r[0]) for r in rows)
    if verbose:
        log.info("  scanning %d docs (id range %d-%d)", len(rows), rows[0][0], rows[-1][0])

    # Phase 1: Collect candidates
    all_candidates: list[dict] = []
    for r in rows:
        doc_id = int(r[0])
        title = str(r[1] or "")
        text = str(r[2] or "")
        try:
            cs = extract_entities_from_doc(text)
            if cs:
                for c in cs:
                    c["_source_id"] = doc_id
                all_candidates.extend(cs)
        except Exception:
            log.warning("  doc %d: extraction error, skipping", doc_id)

    if dry_run:
        if verbose:
            log.info("  → %d candidates found", len(all_candidates))
        return {"processed": len(rows), "matches": len(all_candidates),
                "entities": 0, "mentions": 0, "max_id": max_id, "done": False}

    if not all_candidates:
        # Mark as swept even with no matches
        batch_ids = [int(r[0]) for r in rows]
        for chunk_start in range(0, len(batch_ids), 100):
            chunk = batch_ids[chunk_start:chunk_start + 100]
            id_list = ", ".join(str(i) for i in chunk)
            conn.execute(
                __import__("sqlalchemy").text(
                    f"UPDATE supporting_documents SET swept_at = now() WHERE id IN ({id_list})"
                )
            )
        return {"processed": len(rows), "matches": 0, "entities": 0,
                "mentions": 0, "max_id": max_id, "done": False}

    # Phase 2: Bulk-create entities
    total_matches = 0
    total_entities = 0
    total_mentions = 0

    need_create: dict[str, tuple[str, str, str]] = {}
    for c in all_candidates:
        key = f"{c['normalized']}|{c['entity_type']}"
        if key not in entity_cache and key not in need_create:
            need_create[key] = (c['name'][:500], c['normalized'], c['entity_type'])

    if need_create:
        items = list(need_create.items())
        for ci in range(0, len(items), 100):
            chunk_items = items[ci:ci + 100]
            value_rows = []
            row_params = {}
            for j, (key, (name, nn, etype)) in enumerate(chunk_items):
                suffix = f"_{j}"
                value_rows.append(
                    f"(:et{suffix}, :name{suffix}, :nn{suffix}, False, "
                    f"'unresolved', "
                    f"now(), now(), 1, now(), now())"
                )
                row_params.update({
                    f"et{suffix}": etype,
                    f"name{suffix}": name,
                    f"nn{suffix}": nn,
                })
            sql = (
                "INSERT INTO entities "
                "(entity_type, name, normalized_name, is_government, "
                "resolution_status, "
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
                    eid, nn, etype_val = int(r[0]), str(r[1]), str(r[2])
                    entity_cache[f"{nn}|{etype_val}"] = eid
                    total_entities += 1
            except Exception:
                for key, (name, nn, etype_val) in chunk_items:
                    if key in entity_cache:
                        continue
                    try:
                        row = conn.execute(
                            __import__("sqlalchemy").text("""
                                INSERT INTO entities
                                    (entity_type, name, normalized_name, is_government,
                                     resolution_status,
                                     first_seen_at, last_seen_at, mention_count,
                                     created_at, updated_at)
                                VALUES (:et, :name, :nn, False,
                                        'unresolved',
                                        now(), now(), 1, now(), now())
                                ON CONFLICT (normalized_name, entity_type) DO UPDATE SET
                                    mention_count = entities.mention_count + 1,
                                    last_seen_at = now()
                                RETURNING id
                            """),
                            {"et": etype_val, "name": name, "nn": nn},
                        ).fetchone()
                        entity_cache[key] = row[0]
                        total_entities += 1
                    except Exception:
                        existing = conn.execute(
                            __import__("sqlalchemy").text(
                                "SELECT id FROM entities "
                                "WHERE normalized_name = :nn AND entity_type = :et"
                            ),
                            {"nn": nn, "et": etype_val},
                        ).fetchone()
                        if existing:
                            entity_cache[key] = existing[0]
                            total_entities += 1

    # Phase 3: Pre-load existing mentions
    source_ids = sorted({c["_source_id"] for c in all_candidates})
    existing_mentions: set[tuple[int, int, str]] = set()
    if len(source_ids) <= 100:
        placeholders = ", ".join(str(s) for s in source_ids)
        existing_rows = conn.execute(
            __import__("sqlalchemy").text(f"""
                SELECT entity_id, source_id, role_in_context
                FROM entity_mentions
                WHERE source_type = 'supporting_document'
                  AND source_id IN ({placeholders})
            """)
        ).fetchall()
        for er in existing_rows:
            existing_mentions.add((int(er[0]), int(er[1]), str(er[2])))

    # Phase 4: Bulk-insert new mentions
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
        for i in range(0, len(mention_params), 50):
            chunk = mention_params[i:i + 50]
            value_rows = []
            row_params = {}
            for j, p in enumerate(chunk):
                suffix = f"_{j}"
                value_rows.append(
                    f"(:eid{suffix}, 'supporting_document', :sid{suffix}, "
                    f":mt{suffix}, :cs{suffix}, :conf{suffix}, "
                    f"'sweep_docs', :role{suffix}, now())"
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

    # Phase 5: Mark all fetched docs as swept
    batch_ids = [int(r[0]) for r in rows]
    for chunk_start in range(0, len(batch_ids), 100):
        chunk = batch_ids[chunk_start:chunk_start + 100]
        id_list = ", ".join(str(i) for i in chunk)
        conn.execute(
            __import__("sqlalchemy").text(
                f"UPDATE supporting_documents SET swept_at = now() WHERE id IN ({id_list})"
            )
        )

    return {"processed": len(rows), "matches": total_matches,
            "entities": total_entities, "mentions": total_mentions,
            "max_id": max_id, "done": False}


# ── Library Entry Point ──────────────────────────────────────────────────

def run_sweep_docs(
    engine,
    dry_run: bool = False,
    verbose: bool = False,
    force: bool = False,
    limit: int | None = None,
    batch_size: int = 200,
    **kwargs,
) -> dict:
    """Run sweep_docs phase. Returns structured result dict.

    Sweeps supporting_documents.text_content for entity mentions,
    creates entities and entity_mentions, marks docs as swept.
    """
    global BATCH_SIZE
    BATCH_SIZE = batch_size

    # Ensure watermark table
    with engine.begin() as conn:
        conn.execute(__import__("sqlalchemy").text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                last_run_at TIMESTAMPTZ DEFAULT now(),
                last_processed_id INTEGER DEFAULT 0,
                docs_processed INTEGER DEFAULT 0,
                entities_created INTEGER DEFAULT 0,
                mentions_created INTEGER DEFAULT 0
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

    # Load watermark
    wm = 0
    if not dry_run:
        with engine.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    f"SELECT last_processed_id FROM {WATERMARK_TABLE} ORDER BY last_run_at DESC LIMIT 1"
                )
            ).fetchone()
            if row:
                wm = int(row[0])

    # Get total unscanned
    with engine.connect() as conn:
        total_unscanned = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM supporting_documents "
                "WHERE swept_at IS NULL "
                "AND text_content IS NOT NULL AND text_content != ''"
            )
        ).scalar()
        total_all = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM supporting_documents "
                "WHERE text_content IS NOT NULL AND text_content != ''"
            )
        ).scalar()
    log.info("Supporting docs: %d/%d with text unscanned (watermark id=%d)",
             total_unscanned, total_all, wm)

    if limit:
        total_unscanned = min(total_unscanned, limit)
        log.info("  (limited to %d docs)", total_unscanned)

    grand_total = {"processed": 0, "matches": 0, "entities": 0, "mentions": 0}
    done = False
    loops = 0

    while not done:
        if limit and grand_total["processed"] >= limit:
            log.info("  Reached limit of %d docs", limit)
            break

        try:
            with engine.begin() as conn:
                stats = process_batch(conn, wm, entity_cache,
                                      dry_run=dry_run, verbose=verbose)

            for k in grand_total:
                grand_total[k] += stats.get(k, 0)

            done = stats.get("done", True)
            wm = stats.get("max_id", wm)

            # Update watermark
            if not dry_run and stats["max_id"] > 0:
                with engine.begin() as conn:
                    conn.execute(
                        __import__("sqlalchemy").text(f"""
                            INSERT INTO {WATERMARK_TABLE}
                                (last_run_at, last_processed_id, docs_processed,
                                 entities_created, mentions_created)
                            VALUES (now(), :mid, :dp, :ec, :mc)
                        """),
                        {"mid": stats["max_id"],
                         "dp": stats["processed"],
                         "ec": stats["entities"],
                         "mc": stats["mentions"]},
                    )

            loops += 1
            if loops > 0 and loops % 50 == 0:
                log.info("  Progress: %d docs, %d matches, %d entities, %d mentions",
                         grand_total["processed"], grand_total["matches"],
                         grand_total["entities"], grand_total["mentions"])

        except Exception as e:
            log.error("  Batch error at wm=%d: %s", wm, e, exc_info=verbose)
            break

    return {
        "success": True,
        "docs_processed": grand_total["processed"],
        "matches": grand_total["matches"],
        "entities_created": grand_total["entities"],
        "mentions_created": grand_total["mentions"],
        "dry_run": dry_run,
    }


# ── CLI Entry Point ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sweep supporting documents for entity extraction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max documents to process (default: all)")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Docs per batch (default 200)")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S",
                        stream=sys.stdout,
                        force=True)

    from db.config import DATABASE_URL as _DB_URL
    from sqlalchemy import create_engine

    engine = create_engine(
        _DB_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        future=True,
    )

    result = run_sweep_docs(
        engine,
        dry_run=args.dry_run,
        verbose=args.verbose,
        limit=args.limit,
        batch_size=args.batch_size or 200,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d docs, %d matches, %d entities, %d mentions",
             mode, result["docs_processed"], result["matches"],
             result["entities_created"], result["mentions_created"])

    print(json.dumps({"phase": "sweep_docs", **result}))


if __name__ == "__main__":
    main()
