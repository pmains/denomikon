#!/usr/bin/env python3
"""
Pattern cascade — Phase 2 of the information extraction pipeline.

EXTRACT role-labeled actors from agenda item headers using per-body patterns.
STRATEGY: Only match items where the role label appears as a LINE-LEVEL header
field (e.g. "Applicant: John Smith" on its own line). Inline mentions
("the applicant is...") are left for the ML role classifier.

VALIDATION: Any captured name > 100 characters is discarded.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

from sqlalchemy import text

# CWD-independent path bootstrap (see detect_entities.py).
_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db.core import get_engine
from entities.entity_utils import clean_normalized_name, normalize_entity_name

log = logging.getLogger("pattern_cascade")
WATERMARK_TABLE = "_pattern_cascade_watermark"
MAX_MATCH_LEN = 100
BATCH_SIZE = 30

ROLE_EDGE_MAP = {"applicant": "HAS_APPLICANT", "attorney": "HAS_ATTORNEY",
                  "representative": "HAS_ATTORNEY", "owner": "HAS_OWNER",
                  "staff": "HAS_STAFF", "presenter": "HAS_STAFF"}

# ── Conservative Patterns ──────────────────────────────────────────────
# Only match labels at line-level with colon separator.
# Captured value is everything from label to end-of-line or next label.

def _line_field(label):
    """Label: Value at line-level. Stops at newline or next label."""
    return re.compile(
        rf"{label}:\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )

BODY_PATTERNS: dict[str, list[tuple[str, str, re.Pattern]]] = {
    "phoenix-cc": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "representative", _line_field("Representative")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
    # BOS — items use inline format: "Applicant & Owner: Name / Name Request: ..."
    # All labels on one line, so _line_field would capture past next label.
    # Instead, capture until the next known label or end of line.
    "bos": [
        ("text", "applicant", re.compile(
            r"Applicant(?:\s*&\s*Owner)?:\s*(.+?)(?=\s+(?:Request|Staff(?:\s+Contact)?|Site Location|Location|Commission Recommendation|Case)\s*:|\n|$)",
            re.I | re.M,
        )),
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
    # BOS items don't use Staff Contact: (0 matches), so no staff pattern for BOS.

    # Scottsdale — rich header format with Request:, Presenter(s):, Staff Contact(s):
    # All Scottsdale bodies share the same format; (s) is optional
    "scottsdale": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "presenter", _line_field(r"Presenter(?:\(s\))?")),
        ("text", "staff", _line_field(r"Staff Contact")),
        ("text", "request", _line_field("Request")),
        ("text", "location", _line_field("Location")),
    ],
    # Maricopa County-wide bodies
    "pz": [
        ("text", "applicant", _line_field("Applicant")),
        ("text", "attorney", _line_field("Attorney")),
        ("text", "staff", _line_field("Staff Contact")),
    ],
}

# ── Body resolution ────────────────────────────────────────────────────

def find_body_group(body_code: str) -> str:
    """Map a body code to its pattern group."""
    if body_code in BODY_PATTERNS:
        return body_code
    # Fallback: check for phoenix-* prefix
    for pattern_body in BODY_PATTERNS:
        if pattern_body == "pz" and body_code.endswith("-pz"):
            return "pz"
        if body_code.startswith(pattern_body.split("-")[0]):
            return pattern_body
    return None  # No patterns for this body


# ── Helpers ────────────────────────────────────────────────────────────

def normalize_name(name: str, entity_type: str | None = None) -> str:
    """Normalize an entity name for dedup.
    For person entities, strips titles for cleaner merging.
    """
    if entity_type == 'person':
        return clean_normalized_name(name)
    name = re.sub(r"\s+", " ", name.strip())
    return name.lower()


def is_probable_person(name: str) -> bool:
    n = name.lower()
    firm_kw = ["llc", "inc", "plc", "ltd", "group", "firm", "corporation",
               "company", "partnership", "consulting", "planning", "engineering",
               "law office", "pa", "pc", "llp", "association", "incorporated",
               "hospital", "university", "district", "city of", "town of",
               "county of", "department", "committee", "commission", "board of",
               "design", "architect", "construction", "development", "properties",
               "management", "services", "church", "assembly of god", "llp"]
    has_firm = any(kw in n for kw in firm_kw)
    has_comma = "," in name
    has_space = " " in name
    is_short = len(name.split()) <= 1
    return has_space and not has_firm and not has_comma and not is_short


def classify_entity_type(name: str) -> str:
    return "person" if is_probable_person(name) else "organization"


def validate_name(name: str) -> bool:
    """Reject matched names that are too long or contain junk."""
    if len(name) > MAX_MATCH_LEN:
        return False
    if len(name) < 2:
        return False
    # Reject if it starts with filler words that indicate bad match
    filler_starts = ["the ", "and ", "to ", "for ", "of ", "a ",
                     "an ", "in ", "on ", "is ", "it ", "be "]
    if any(name.lower().startswith(f) for f in filler_starts):
        # Allow "The Church of..." or "The City of..." but not "the applicant..."
        if not any(kw in name.lower() for kw in ["church", "city of", "town of", "county of", "state of"]):
            return False
    return True


# ── Batch Queries ──────────────────────────────────────────────────────

QUERY = """
    SELECT ai.id, ai.body, ai.meeting_db_id, ai.agenda_item_id,
           ai.agenda_item_number, ai.agenda_item_text,
           ai.agenda_item_title, ai.case_number, ai.c_number
    FROM agenda_items ai
    WHERE ai.id > :wm AND ai.body = :body
      AND ai.agenda_item_text IS NOT NULL
      AND length(ai.agenda_item_text) > 0
    ORDER BY ai.id
    LIMIT 10000
"""


def _bulk_insert(conn, table: str, columns: list[str], vals: list[dict],
                 entity_cache: dict, returning: str = "") -> list:
    """Bulk INSERT using batched VALUES clauses. Returns list of row dicts."""
    if not vals:
        return []
    all_results = []
    for batch_start in range(0, len(vals), BATCH_SIZE):
        batch = vals[batch_start:batch_start + BATCH_SIZE]
        col_list = ", ".join(columns)
        val_parts = []
        params = {}
        for bi, row in enumerate(batch):
            i = batch_start + bi
            val_parts.append(f"({', '.join(f':{col}{i}' for col in columns)})")
            for col in columns:
                params[f"{col}{i}"] = row.get(col)
        val_clause = ", ".join(val_parts)
        ret = " RETURNING *" if returning else ""
        rows = conn.execute(
            text(f"INSERT INTO {table} ({col_list}) SELECT {', '.join(columns)} "
                 f"FROM (VALUES {val_clause}) AS v({', '.join(columns)}){ret}"),
            params,
        ).fetchall() if returning else conn.execute(
            text(f"INSERT INTO {table} ({col_list}) SELECT {', '.join(columns)} "
                 f"FROM (VALUES {val_clause}) AS v({', '.join(columns)})"),
            params,
        )
        if returning:
            all_results.extend(rows)
    return all_results


# ── Process ────────────────────────────────────────────────────────────

def process_body(conn, body: str, wm: int, entity_cache: dict,
                 dry_run: bool = False, verbose: bool = False) -> dict:
    """Process one chunk of items for a body. Returns stats dict."""
    body_group = find_body_group(body)
    if not body_group:
        return {"processed": 0, "matches": 0, "entities": 0, "edges": 0, "max_id": wm}

    patterns = BODY_PATTERNS[body_group]
    rows = conn.execute(text(QUERY), {"wm": wm, "body": body}).fetchall()
    if not rows:
        return {"processed": 0, "matches": 0, "entities": 0, "edges": 0, "max_id": wm}

    # Phase 1: Scan
    new_entities: dict[tuple[str, str], str] = {}
    new_mention_rows: list[dict] = []
    new_edge_rows: list[dict] = []
    max_id = wm
    total_matches = 0

    for r in rows:
        item_id = int(r[0])
        item_body = str(r[1])
        text_content = str(r[5] or "")
        case_number = str(r[7] or "").strip()
        c_number = str(r[8] or "").strip()
        max_id = max(max_id, item_id)

        identifier = case_number or c_number
        case_norm = normalize_name(identifier) if identifier else None

        roles_found = set()
        for field_name, role, pattern in patterns:
            if role in roles_found:
                continue
            src = text_content if field_name == "text" else ""
            m = pattern.search(src)
            if not m:
                continue
            actor_name = m.group(1).strip()
            if not validate_name(actor_name):
                continue
            roles_found.add(role)
            total_matches += 1

            if dry_run:
                continue

            etype = classify_entity_type(actor_name)
            norm = normalize_name(actor_name, etype)
            entity_key = (norm, etype)

            if entity_key not in entity_cache and entity_key not in new_entities:
                new_entities[entity_key] = actor_name

            new_mention_rows.append({
                "entity_id": None,  # resolved after entity creation
                "source_type": "agenda_item", "source_id": item_id,
                "mention_text": actor_name[:500],
                "context_snippet": actor_name[:300],
                "confidence": 90, "extracted_by": "pattern_cascade",
                "role_in_context": role,
            })

            edge_type = ROLE_EDGE_MAP.get(role, "")
            if case_norm and edge_type:
                case_key = (case_norm, "case")
                if case_key not in entity_cache and case_key not in new_entities:
                    new_entities[case_key] = identifier
                # Store entity keys (norm + type) for resolution in Phase 4
                actor_type = classify_entity_type(actor_name)
                actor_norm = normalize_name(actor_name, actor_type)
                new_edge_rows.append({
                    "from_norm": actor_norm,
                    "from_type": actor_type,
                    "to_norm": case_norm,
                    "to_type": "case",
                    "relationship": edge_type,
                    "provenance_type": "agenda_item",
                    "provenance_id": item_id,
                    "source_label": f"Pattern: {edge_type}",
                    "edge_kind": "relational",
                    "confidence": 0.9,
                })

    # Phase 2: Entities
    for (norm, etype), name in new_entities.items():
        if (norm, etype) in entity_cache:
            continue
        sub = conn.execute(
            text("""
                INSERT INTO entities
                    (entity_type, name, normalized_name, is_government,
                     resolution_status,
                     first_seen_at, last_seen_at, mention_count,
                     created_at, updated_at)
                VALUES (:et, :name, :nn, False,
                        'unresolved',
                        now(), now(), 1, now(), now())
                ON CONFLICT (normalized_name, entity_type) DO UPDATE SET
                    last_seen_at = now()
                RETURNING id
            """),
            {"et": etype, "name": name, "nn": norm},
        ).fetchone()
        if sub:
            entity_cache[(norm, etype)] = sub[0]
        else:
            # Race: entity was inserted between our check and the INSERT.
            existing = conn.execute(
                text("SELECT id FROM entities WHERE normalized_name = :nn AND entity_type = :et"),
                {"nn": norm, "et": etype},
            ).fetchone()
            if existing:
                entity_cache[(norm, etype)] = existing[0]

    # Phase 3: Mentions (one at a time to resolve entity_id)
    for row in new_mention_rows:
        etype = "person" if is_probable_person(str(row["mention_text"])) else "organization"
        norm = normalize_name(str(row["mention_text"]), etype)
        eid = entity_cache.get((norm, etype))
        if not eid:
            continue
        # Check for duplicate
        dup = conn.execute(
            text("SELECT 1 FROM entity_mentions WHERE entity_id = :eid "
                 "AND source_type = :st AND source_id = :sid AND role_in_context = :role"),
            {"eid": eid, "st": row["source_type"], "sid": row["source_id"],
             "role": row["role_in_context"]},
        ).fetchone()
        if dup:
            continue
        conn.execute(
            text("""
                INSERT INTO entity_mentions
                    (entity_id, source_type, source_id, mention_text,
                     context_snippet, confidence, extracted_by, role_in_context,
                     created_at)
                VALUES (:eid, :st, :sid, :mt, :cs, :conf, :eb, :role, now())
            """),
            {"eid": eid, "st": row["source_type"], "sid": row["source_id"],
             "mt": row["mention_text"], "cs": row["context_snippet"],
             "conf": 90, "eb": "pattern_cascade", "role": row["role_in_context"]},
        )

    # Phase 4: Edges — resolve entity IDs and bulk insert
    resolved_edges: list[tuple[int, int, dict]] = []
    edges_skipped = 0
    if new_edge_rows and entity_cache:
        # Pre-load existing edges for this body's agenda items
        existing_edge_set: set[tuple[int, str, int, str, int]] = set()
        item_ids = list({r["provenance_id"] for r in new_edge_rows})
        if item_ids:
            # Batch in chunks to avoid overly long IN clauses
            for chunk_start in range(0, len(item_ids), 100):
                chunk = item_ids[chunk_start:chunk_start + 100]
                id_list = ", ".join(str(i) for i in chunk)
                try:
                    existing_rows = conn.execute(text(f"""
                        SELECT from_entity_id, relationship, to_entity_id,
                               provenance_type, provenance_id
                        FROM entity_relationships
                        WHERE provenance_type = 'agenda_item'
                          AND provenance_id IN ({id_list})
                    """)).fetchall()
                    for r in existing_rows:
                        existing_edge_set.add((
                            int(r[0]), str(r[1]), int(r[2]), str(r[3]), int(r[4])
                        ))
                except Exception:
                    pass  # Table may not exist yet

        for row in new_edge_rows:
            from_key = (row["from_norm"], row["from_type"])
            to_key = (row["to_norm"], row["to_type"])
            from_id = entity_cache.get(from_key)
            to_id = entity_cache.get(to_key)
            if not from_id or not to_id:
                edges_skipped += 1
                continue
            key = (from_id, row["relationship"], to_id,
                   row["provenance_type"], row["provenance_id"])
            if key in existing_edge_set:
                edges_skipped += 1
                continue
            resolved_edges.append((from_id, to_id, row))

        if resolved_edges:
            val_parts = []
            params = {}
            for i, (from_id, to_id, row) in enumerate(resolved_edges):
                val_parts.append(
                    f"(:feid{i}, :teid{i}, :rel{i}, :pt{i}, :pid{i}, :sl{i}, :ek{i})"
                )
                params[f"feid{i}"] = from_id
                params[f"teid{i}"] = to_id
                params[f"rel{i}"] = row["relationship"]
                params[f"pt{i}"] = row["provenance_type"]
                params[f"pid{i}"] = row["provenance_id"]
                params[f"sl{i}"] = row["source_label"]
                params[f"ek{i}"] = row["edge_kind"]

            val_clause = ", ".join(val_parts)
            conn.execute(text(f"""
                INSERT INTO entity_relationships
                    (from_entity_id, to_entity_id, relationship,
                     provenance_type, provenance_id, source_label,
                     edge_kind, confidence, created_at)
                SELECT v.feid, v.teid, v.rel, v.pt, v.pid, v.sl,
                       v.ek, 0.9, now()
                FROM (VALUES {val_clause})
                AS v(feid, teid, rel, pt, pid, sl, ek)
            """), params)

    return {
        "processed": len(rows), "matches": total_matches,
        "entities": len(new_entities), "edges": len(resolved_edges),
        "edges_skipped": edges_skipped, "max_id": max_id,
    }


def run_pattern_cascade(
    engine,
    body_filter: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Run pattern cascade phase. Returns structured result dict.

    Scans agenda items for role-labeled headers ("Applicant:", "Staff:", etc.)
    and creates entity mentions and entities for each match.
    """
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                body VARCHAR(64) PRIMARY KEY, last_run_at TIMESTAMPTZ DEFAULT now(),
                last_processed_id INTEGER DEFAULT 0, items_processed INTEGER DEFAULT 0,
                entities_created INTEGER DEFAULT 0, edges_created INTEGER DEFAULT 0
            );
        """))

    watermarks = {}
    if not force:
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT body, last_processed_id FROM {WATERMARK_TABLE}")).fetchall()
            watermarks = {r[0]: int(r[1]) for r in rows}

    # Load entity cache
    with engine.connect() as conn:
        entity_cache = {}
        rows = conn.execute(text("SELECT normalized_name, entity_type, id FROM entities")).fetchall()
        for r in rows:
            entity_cache[(str(r[0]), str(r[1]))] = int(r[2])

    # Get all body codes
    with engine.connect() as conn:
        all_bodies = [r[0] for r in conn.execute(
            text("SELECT DISTINCT body FROM agenda_items ORDER BY body")
        ).fetchall()]

    if body_filter:
        all_bodies = [b for b in all_bodies if body_filter in b]

    # Pre-filter: only process bodies with patterns, skip watermarked
    targeted_bodies = [b for b in all_bodies if find_body_group(b)]
    if not force:
        targeted_bodies = [b for b in targeted_bodies if b not in watermarks]

    total = {"processed": 0, "matches": 0, "entities": 0, "edges": 0}

    for body in sorted(targeted_bodies):
        wm = watermarks.get(body, 0)

        try:
            with engine.begin() as conn:
                stats = process_body(conn, body, wm, entity_cache,
                                     dry_run=dry_run, verbose=verbose)
                if not dry_run and stats["max_id"] > wm:
                    conn.execute(
                        text(f"""
                            INSERT INTO {WATERMARK_TABLE} (body, last_run_at, last_processed_id,
                                items_processed, entities_created, edges_created)
                            VALUES (:body, now(), :mid, :ip, :ec, :edc)
                            ON CONFLICT (body) DO UPDATE SET
                                last_run_at = now(), last_processed_id = :mid,
                                items_processed = {WATERMARK_TABLE}.items_processed + :ip,
                                entities_created = {WATERMARK_TABLE}.entities_created + :ec,
                                edges_created = {WATERMARK_TABLE}.edges_created + :edc
                        """),
                        {"body": body, "mid": stats["max_id"], "ip": stats["processed"],
                         "ec": stats["entities"], "edc": stats["edges"]},
                    )
            for k in total:
                total[k] += stats.get(k, 0)
        except Exception as e:
            log.error("  ✗ %s: %s", body, e, exc_info=verbose)
            if not force:
                raise

    return {
        "success": True,
        "items_processed": total["processed"],
        "matches": total["matches"],
        "entities_created": total["entities"],
        "edges_created": total["edges"],
        "bodies_processed": len(targeted_bodies),
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--body", type=str)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    engine = get_engine()
    result = run_pattern_cascade(
        engine,
        body_filter=args.body,
        dry_run=args.dry_run,
        force=args.force,
        verbose=args.verbose,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d items, %d matches, %d entities, %d edges",
             mode, result["items_processed"], result["matches"],
             result["entities_created"], result["edges_created"])

    print(json.dumps({"phase": "pattern_cascade", **result}))


if __name__ == "__main__":
    main()
