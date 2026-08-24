#!/usr/bin/env python3
"""
Entity resolution — deduplicate and merge entity records.

Runs as Phase 4 of the entity detection pipeline. Three sub-phases:

1. TYPE_CONFLICT — Merge same-normalized-name duplicates across types.
   When "Brandon McNeil" exists as both person(id=463) and organization(id=18399),
   the person wins and the org re-points to it.

2. COMPOSITE_SPLIT — Detect "Person, Firm" entity names from pattern_cascade
   overmatching. Split into separate person + firm entities and create a
   HAS_ATTORNEY / HAS_APPLICANT relationship.

3. NAME_VARIATION — Block candidates by Soundex + token similarity and merge
   probable duplicates like "Hitt-Zollars" ↔ "Huitt-Zollars".

Each sub-phase is idempotent with its own watermark.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone

from sqlalchemy import text

sys.path.insert(0, "scripts")
from db.core import get_engine

log = logging.getLogger("resolver")
WATERMARK_TABLE = "_resolver_watermark"
BATCH_SIZE = 100

# ── Phase 1: Type Conflict Resolution ──────────────────────────────────────

PHASE1_SAME_NAME_DUPES = """
    SELECT e1.id AS id1, e1.name AS name1, e1.entity_type AS type1,
           e1.mention_count AS cnt1,
           e2.id AS id2, e2.name AS name2, e2.entity_type AS type2,
           e2.mention_count AS cnt2
    FROM entities e1
    JOIN entities e2 ON e1.normalized_name = e2.normalized_name
                     AND e1.id < e2.id
    WHERE e1.resolution_status = 'unresolved'
      AND e2.resolution_status = 'unresolved'
      AND e1.entity_type != e2.entity_type
      AND e1.canonical_entity_id IS NULL
      AND e2.canonical_entity_id IS NULL
"""

TYPE_PRIORITY = {"person": 0, "developer": 1, "planning_firm": 1,
                 "law_firm": 1, "organization": 2, "utility": 3,
                 "advocacy_group": 3, "case": 4, "parcel": 5,
                 "address": 6}


def _type_priority(t: str) -> int:
    return TYPE_PRIORITY.get(t, 99)


def _resolve_type_conflicts(conn, dry_run: bool = False, verbose: bool = False) -> dict:
    """Merge same-normalized-name entities where one has the wrong type."""
    rows = conn.execute(text(PHASE1_SAME_NAME_DUPES)).fetchall()
    if verbose:
        log.info("  Phase 1: %d type-conflict pairs found", len(rows))

    merged = 0
    for r in rows:
        id1, type1, cnt1 = r[0], r[2], r[3]
        id2, type2, cnt2 = r[4], r[6], r[7]
        # Determine survivor by priority, but allow mention-count override.
        # If the higher-priority entity has <= 2 mentions and the lower-priority
        # has significantly more, the lower-priority (more specific) entity wins.
        # This prevents junk "person" entries from absorbing real organization records.
        p1 = _type_priority(type1)
        p2 = _type_priority(type2)

        if p1 < p2:
            # type1 is higher priority (lower number)
            if cnt1 <= 2 and cnt2 > cnt1 * 3:
                survivor, victim = id2, id1
            else:
                survivor, victim = id1, id2
        elif p2 < p1:
            # type2 is higher priority
            if cnt2 <= 2 and cnt1 > cnt2 * 3:
                survivor, victim = id1, id2
            else:
                survivor, victim = id2, id1
        else:
            # Same priority — keep the one with more mentions
            if cnt1 >= cnt2:
                survivor, victim = id1, id2
            else:
                survivor, victim = id2, id1

        if dry_run:
            log.info("    Would merge %d(%s) → %d(%s)", victim, victim_type, survivor, survivor_type)
            merged += 1
            continue

        _merge(conn, victim, survivor, "type_conflict", 0.99)
        merged += 1

    return {"phase1_type_conflicts": merged}


def _merge(conn, victim_id: int, survivor_id: int,
           method: str, confidence: float) -> None:
    """Re-point all references from victim to survivor, then mark victim."""
    now = datetime.now(timezone.utc)

    # Re-point entity_mentions
    conn.execute(
        text("""
            UPDATE entity_mentions
            SET entity_id = :survivor
            WHERE entity_id = :victim
              AND NOT EXISTS (
                  SELECT 1 FROM entity_mentions em2
                  WHERE em2.entity_id = :survivor
                    AND em2.source_type = entity_mentions.source_type
                    AND em2.source_id = entity_mentions.source_id
                    AND em2.role_in_context IS NOT DISTINCT FROM entity_mentions.role_in_context
              )
        """),
        {"survivor": survivor_id, "victim": victim_id},
    )

    # Re-point entity_relationships (from_entity_id)
    conn.execute(
        text("""
            UPDATE entity_relationships
            SET from_entity_id = :survivor
            WHERE from_entity_id = :victim
              AND NOT EXISTS (
                  SELECT 1 FROM entity_relationships er2
                  WHERE er2.from_entity_id = :survivor
                    AND er2.relationship = entity_relationships.relationship
                    AND er2.to_entity_id = entity_relationships.to_entity_id
                    AND er2.provenance_type IS NOT DISTINCT FROM entity_relationships.provenance_type
                    AND er2.provenance_id IS NOT DISTINCT FROM entity_relationships.provenance_id
              )
        """),
        {"survivor": survivor_id, "victim": victim_id},
    )

    # Re-point entity_relationships (to_entity_id)
    conn.execute(
        text("""
            UPDATE entity_relationships
            SET to_entity_id = :survivor
            WHERE to_entity_id = :victim
              AND NOT EXISTS (
                  SELECT 1 FROM entity_relationships er2
                  WHERE er2.from_entity_id = entity_relationships.from_entity_id
                    AND er2.relationship = entity_relationships.relationship
                    AND er2.to_entity_id = :survivor
                    AND er2.provenance_type IS NOT DISTINCT FROM entity_relationships.provenance_type
                    AND er2.provenance_id IS NOT DISTINCT FROM entity_relationships.provenance_id
              )
        """),
        {"survivor": survivor_id, "victim": victim_id},
    )

    # Accumulate mention_count on survivor
    victim_cnt = conn.execute(
        text("SELECT mention_count FROM entities WHERE id = :vid"),
        {"vid": victim_id},
    ).scalar() or 0
    conn.execute(
        text("""
            UPDATE entities
            SET mention_count = mention_count + :vcnt,
                last_seen_at = GREATEST(last_seen_at, (
                    SELECT last_seen_at FROM entities WHERE id = :vid
                )),
                updated_at = :now
            WHERE id = :sid
        """),
        {"vcnt": victim_cnt, "vid": victim_id, "sid": survivor_id, "now": now},
    )

    # Mark victim as merged
    conn.execute(
        text("""
            UPDATE entities
            SET canonical_entity_id = :sid,
                resolution_status = 'merged',
                resolution_confidence = :conf,
                resolution_method = :method,
                resolved_at = :now,
                updated_at = :now
            WHERE id = :vid
        """),
        {"sid": survivor_id, "vid": victim_id, "conf": confidence,
         "method": method, "now": now},
    )

    # Mark survivor as resolved
    conn.execute(
        text("""
            UPDATE entities
            SET resolution_status = 'canonical',
                resolution_method = :method,
                resolved_at = :now,
                updated_at = :now
            WHERE id = :sid
              AND resolution_status = 'unresolved'
        """),
        {"sid": survivor_id, "method": method, "now": now},
    )


# ── Phase 2: Composite Entity Split ────────────────────────────────────────

COMPOSITE_PATTERN = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),\s+(.+)$"
)

PHASE2_COMPOSITES = """
    SELECT id, name, normalized_name, entity_type
    FROM entities
    WHERE resolution_status = 'unresolved'
      AND entity_type IN ('organization', 'person')
    ORDER BY id
"""


def _resolve_composites(conn, dry_run: bool = False, verbose: bool = False) -> dict:
    """Find and split composite 'Person, Firm' entities."""
    # Load entity cache once
    cache_rows = conn.execute(
        text("SELECT normalized_name, entity_type, id FROM entities")
    ).fetchall()
    entity_cache = {(str(r[0]), str(r[1])): int(r[2]) for r in cache_rows}
    if verbose:
        log.info("  Phase 2: loaded %d entities into cache", len(entity_cache))

    rows = conn.execute(text(PHASE2_COMPOSITES)).fetchall()
    if verbose:
        log.info("  Phase 2: scanning %d entities for composite patterns", len(rows))

    ROLE_ONLY_ALL = {"agent", "agents", "rls", "esq", "jr", "sr", "pe",
                     "pls", "pc", "plc", "llc", "inc", "iii"}
    TITLE_WORDS = {"councilmember", "chairperson", "chairman", "chairwoman",
                   "mayor", "vice mayor", "councilman", "councilwoman",
                   "commissioner", "vice chair", "proposed request"}

    # Phase A: Identify all composites and collect operations
    ops = []  # list of (name, person_name, org_name, person_norm, org_norm, eid)

    for r in rows:
        eid = int(r[0])
        name = str(r[1] or "")

        m = COMPOSITE_PATTERN.match(name)
        if not m:
            continue

        person_name = m.group(1).strip()
        org_name = m.group(2).strip()
        if not person_name or not org_name:
            continue

        # Skip role-only suffixes
        org_check = re.sub(r"[^a-zA-Z ]", " ", org_name.lower()).strip()
        org_all_tokens = org_check.split()
        if org_all_tokens and all(t in ROLE_ONLY_ALL for t in org_all_tokens):
            continue

        # Skip title-like person names
        person_lower = re.sub(r"[^a-zA-Z ]", " ", person_name.lower()).strip()
        if person_lower in TITLE_WORDS or person_lower.startswith("proposed"):
            continue

        # Skip "Jr., X"
        if re.search(r'\bjr\.?$', person_name, re.I):
            continue

        if dry_run:
            if verbose:
                log.info("    Composite: '%s' → person='%s', org='%s'", name, person_name, org_name)
            continue

        person_norm = re.sub(r"\s+", " ", person_name.lower().strip())
        org_norm = re.sub(r"\s+", " ", org_name.lower().strip())
        ops.append((name, person_name, org_name, person_norm, org_norm, eid))

    if dry_run:
        return {"phase2_composites": len(ops)}

    # Phase B: Bulk-resolve/create entities
    now = datetime.now(timezone.utc)

    # Collect which entity keys we need to look up
    new_entities = []  # list of (entity_type, name, norm)

    for name, person_name, org_name, person_norm, org_norm, eid in ops:
        pkey = (person_norm, "person")
        okey = (org_norm, "organization")
        if pkey not in entity_cache:
            new_entities.append(("person", person_name, person_norm))
            # Placeholder in cache so we don't create duplicate
            entity_cache[pkey] = -(len(new_entities))
        if okey not in entity_cache:
            # Also check if org exists under any type
            found = False
            for cached_key, cached_id in entity_cache.items():
                if cached_key[0] == org_norm:
                    okey = cached_key
                    found = True
                    break
            if not found:
                new_entities.append(("organization", org_name, org_norm))
                entity_cache[okey] = -(len(new_entities))

    # Bulk insert new entities (in batches of 50 to avoid oversized queries)
    new_id_offset = 0
    BATCH_SIZE = 50
    if new_entities:
        for batch_start in range(0, len(new_entities), BATCH_SIZE):
            batch = new_entities[batch_start:batch_start + BATCH_SIZE]
            val_parts = []
            params = {}
            for bi, (etype, ename, enorm) in enumerate(batch):
                i = batch_start + bi
                val_parts.append(f"(:et{i}, :name{i}, :nn{i})")
                params[f"et{i}"] = etype
                params[f"name{i}"] = ename
                params[f"nn{i}"] = enorm
            val_clause = ", ".join(val_parts)
            result_rows = conn.execute(
                text(f"""
                    INSERT INTO entities
                        (entity_type, name, normalized_name, is_government,
                         first_seen_at, last_seen_at, mention_count,
                         resolution_status, created_at, updated_at)
                    SELECT v.et, v.name, v.nn, False,
                           :now, :now, 1, 'canonical', :now, :now
                    FROM (VALUES {val_clause}) AS v(et, name, nn)
                    RETURNING normalized_name, entity_type, id
                """),
                {**params, "now": now},
            ).fetchall()
            for r in result_rows:
                key = (str(r[0]), str(r[1]))
                entity_cache[key] = int(r[2])
                new_id_offset += 1

    # Phase C: Bulk-create relationships, re-point mentions, mark merged
    mention_updates = []
    mention_inserts = []
    relationship_inserts = []
    entity_merges = []

    for name, person_name, org_name, person_norm, org_norm, eid in ops:
        pkey = (person_norm, "person")
        person_id = entity_cache.get(pkey)
        # Try any type for org
        okey = (org_norm, "organization")
        org_id = entity_cache.get(okey)
        if not org_id:
            # Check other types
            for ck, cid in entity_cache.items():
                if ck[0] == org_norm:
                    org_id = cid
                    okey = ck
                    break

        if not person_id or not org_id:
            if verbose:
                log.warning("    Could not resolve IDs for '%s' (person=%s, org=%s)",
                          name, person_id, org_id)
            continue

        relationship_inserts.append({
            "from_entity_id": person_id, "to_entity_id": org_id,
            "relationship": "HAS_APPLICANT",
            "provenance_type": "entity_resolution", "provenance_id": eid,
            "source_label": f"Split from composite: {name[:100]}",
            "edge_kind": "relational", "confidence": 0.8,
        })
        mention_updates.append({"pid": person_id, "cid": eid})
        mention_inserts.append({
            "oid": org_id, "oname": org_name[:500], "pid": person_id,
        })
        entity_merges.append({"pid": person_id, "cid": eid})

    # Execute bulk operations
    if relationship_inserts:
        val_parts = []
        params = {}
        for i, row in enumerate(relationship_inserts):
            val_parts.append(
                f"(:fe{i}, :te{i}, 'HAS_APPLICANT', 'entity_resolution',"
                f" :pid{i}, :sl{i}, 'relational', 0.8, :now)"
            )
            params[f"fe{i}"] = row["from_entity_id"]
            params[f"te{i}"] = row["to_entity_id"]
            params[f"pid{i}"] = row["provenance_id"]
            params[f"sl{i}"] = row["source_label"][:200]
        params["now"] = now
    if relationship_inserts:
        # Per-row insert — avoids VALUES type inference issues
        for row in relationship_inserts:
            conn.execute(
                text("""
                    INSERT INTO entity_relationships
                        (from_entity_id, to_entity_id, relationship,
                         provenance_type, provenance_id, source_label,
                         edge_kind, confidence, created_at)
                    VALUES (:fe, :te, 'HAS_APPLICANT', 'entity_resolution',
                            :pid, :sl, 'relational', 0.8, now())
                    ON CONFLICT DO NOTHING
                """),
                {"fe": row["from_entity_id"], "te": row["to_entity_id"],
                 "pid": row["provenance_id"], "sl": row["source_label"][:200]},
            )

    if mention_updates:
        # Batch mention UPDATE
        BATCH_SIZE = 50
        for batch_start in range(0, len(mention_updates), BATCH_SIZE):
            batch = mention_updates[batch_start:batch_start + BATCH_SIZE]
            val_parts = []
            params = {}
            for bi, row in enumerate(batch):
                i = batch_start + bi
                val_parts.append(f"(:pid{i}, :cid{i})")
                params[f"pid{i}"] = row["pid"]
                params[f"cid{i}"] = row["cid"]
            val_clause = ", ".join(val_parts)
            conn.execute(
                text(f"""
                    UPDATE entity_mentions em
                    SET entity_id = v.pid
                    FROM (VALUES {val_clause}) AS v(pid, cid)
                    WHERE em.entity_id = v.cid
                      AND NOT EXISTS (
                          SELECT 1 FROM entity_mentions em2
                          WHERE em2.entity_id = v.pid
                            AND em2.source_type = em.source_type
                            AND em2.source_id = em.source_id
                            AND em2.role_in_context IS NOT DISTINCT FROM em.role_in_context
                      )
                """),
                params,
            )

        # Batch mention INSERT (INSERT...SELECT with VALUES-driven joins)
        for batch_start in range(0, len(mention_inserts), BATCH_SIZE):
            batch = mention_inserts[batch_start:batch_start + BATCH_SIZE]
            val_parts = []
            params = {}
            for bi, row in enumerate(batch):
                i = batch_start + bi
                val_parts.append(f"(:oid{i}, :oname{i}, :pid{i})")
                params[f"oid{i}"] = row["oid"]
                params[f"oname{i}"] = row["oname"][:500]
                params[f"pid{i}"] = row["pid"]
            params["now"] = now
            val_clause = ", ".join(val_parts)
            conn.execute(
                text(f"""
                    INSERT INTO entity_mentions
                        (entity_id, source_type, source_id, mention_text,
                         context_snippet, confidence, extracted_by, role_in_context, created_at)
                    SELECT DISTINCT ON (v.oid, em.source_type, em.source_id)
                           v.oid, em.source_type, em.source_id, v.oname,
                           em.context_snippet, 70, 'resolver', 'firm', :now
                    FROM (VALUES {val_clause}) AS v(oid, oname, pid)
                    JOIN entity_mentions em ON em.entity_id = v.pid
                        AND em.source_type = 'agenda_item'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM entity_mentions em2
                        WHERE em2.entity_id = v.oid
                          AND em2.source_type = em.source_type
                          AND em2.source_id = em.source_id
                    )
                """),
                params,
            )

    if entity_merges:
        val_parts = []
        params = {}
        for i, row in enumerate(entity_merges):
            val_parts.append(f"(:pid{i}, :cid{i})")
            params[f"pid{i}"] = row["pid"]
            params[f"cid{i}"] = row["cid"]
        val_clause = ", ".join(val_parts)
        conn.execute(
            text(f"""
                UPDATE entities
                SET canonical_entity_id = v.pid,
                    resolution_status = 'merged',
                    resolution_confidence = 0.95,
                    resolution_method = 'composite_split',
                    resolved_at = :now,
                    updated_at = :now
                FROM (VALUES {val_clause}) AS v(pid, cid)
                WHERE entities.id = v.cid
            """),
            {**params, "now": now},
        )

    return {"phase2_composites": len(ops)}


# ── Phase 3: Name Variation Matching ───────────────────────────────────────

PHASE3_ORGS = """
    SELECT id, name, normalized_name, entity_type, resolution_block_key
    FROM entities
    WHERE resolution_status = 'unresolved'
      AND entity_type IN ('organization', 'developer', 'planning_firm', 'law_firm')
    ORDER BY normalized_name
"""


def _token_normalize(s: str) -> str:
    """Remove punctuation, normalize whitespace, lowercase."""
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _token_set_similarity(a: str, b: str) -> float:
    """Jaccard similarity on token sets."""
    ta = set(_token_normalize(a).split())
    tb = set(_token_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _token_sort_similarity(a: str, b: str) -> float:
    """Check if sorted-token strings match (same words, different order)."""
    ta = sorted(_token_normalize(a).split())
    tb = sorted(_token_normalize(b).split())
    return 1.0 if ta == tb and ta else 0.0


def _substring_match(a: str, b: str) -> float:
    """Score 0.9 if one name is a clear substring of the other."""
    na = _token_normalize(a)
    nb = _token_normalize(b)
    if len(na) < 3 or len(nb) < 3:
        return 0.0
    # One is fully contained in the other (e.g., "Vertical Bridge" in
    # "Annmarie Beckett, Vertical Bridge/Clear Blue Services")
    if na in nb or nb in na:
        # But only if the longer name isn't drastically longer
        ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        if ratio > 0.25:
            return 0.85
    return 0.0


def _acronym_match(a: str, b: str) -> float:
    """Score if acronym of one matches the other (e.g., 'JCJ' == 'JCJ Services')."""
    na = _token_normalize(a)
    nb = _token_normalize(b)

    def acronym(s: str) -> str:
        return "".join(w[0] for w in s.split() if w)

    acr_a = acronym(na)
    acr_b = acronym(nb)
    if acr_a and acr_b and (acr_a == acr_b or acr_a in nb or acr_b in na):
        # Check it's not just a single-letter match
        if len(acr_a) >= 2:
            return 0.75
    return 0.0


def _resolve_name_variations(conn, dry_run: bool = False, verbose: bool = False) -> dict:
    """Block and merge similar organization names."""
    rows = conn.execute(text(PHASE3_ORGS)).fetchall()
    if verbose:
        log.info("  Phase 3: %d organization entities to scan", len(rows))

    entities = []
    for r in rows:
        entities.append({
            "id": int(r[0]),
            "name": str(r[1] or ""),
            "norm": str(r[2] or ""),
            "type": str(r[3] or ""),
            "block": str(r[4] or ""),
        })

    merged = 0
    compared = 0

    for i in range(len(entities)):
        e1 = entities[i]
        if e1.get("_dead"):
            continue
        for j in range(i + 1, len(entities)):
            e2 = entities[j]
            if e2.get("_dead"):
                continue

            # Skip same-type same-name (already caught by Phase 1)
            if e1["norm"] == e2["norm"] and e1["type"] == e2["type"]:
                continue

            # Only compare same-resolution-block candidates
            block_key = _make_block_key(e1["norm"], e2["norm"])
            if not block_key:
                continue

            compared += 1

            # Compute similarity scores
            token_sim = _token_set_similarity(e1["name"], e2["name"])
            sort_sim = _token_sort_similarity(e1["norm"], e2["norm"])
            sub_sim = _substring_match(e1["norm"], e2["norm"])
            acr_sim = _acronym_match(e1["name"], e2["name"])

            # Combined score — weighted
            score = max(token_sim, sort_sim, sub_sim, acr_sim)

            if score >= 0.85:
                if dry_run:
                    log.info("    MATCH (%.2f): '%s'(%d, %s) ↔ '%s'(%d, %s)",
                             score, e1["name"], e1["id"], e1["type"],
                             e2["name"], e2["id"], e2["type"])
                    merged += 1
                    e2["_dead"] = True
                    continue

                # Merge lower-mention into higher-mention
                cnt1 = conn.execute(
                    text("SELECT mention_count FROM entities WHERE id = :id"),
                    {"id": e1["id"]},
                ).scalar() or 0
                cnt2 = conn.execute(
                    text("SELECT mention_count FROM entities WHERE id = :id"),
                    {"id": e2["id"]},
                ).scalar() or 0

                if cnt1 >= cnt2:
                    survivor, victim = e1["id"], e2["id"]
                    survivor_name, victim_name = e1["name"], e2["name"]
                else:
                    survivor, victim = e2["id"], e1["id"]
                    survivor_name, victim_name = e2["name"], e1["name"]

                if verbose:
                    log.info("    MERGE (%.2f): '%s'(%d) ← '%s'(%d)",
                             score, survivor_name, survivor, victim_name, victim)

                _merge(conn, victim, survivor, "name_variation", score)
                merged += 1
                e2["_dead"] = True

    return {"phase3_name_variations": merged, "compared": compared}


def _make_block_key(a: str, b: str) -> str | None:
    """Determine if two names should be compared. Returns block key or None."""
    na = _token_normalize(a)
    nb = _token_normalize(b)
    if na == nb:
        return f"exact:{na}"

    # Same first token (usually the most distinctive: "Hitt" ≈ "Huitt")
    a_tokens = na.split()
    b_tokens = nb.split()
    if a_tokens and b_tokens and a_tokens[0] == b_tokens[0]:
        return f"first:{a_tokens[0]}"

    # Same last token
    if len(a_tokens) > 0 and len(b_tokens) > 0 and a_tokens[-1] == b_tokens[-1]:
        return f"last:{a_tokens[-1]}"

    # Token subset — one is wholly contained in the other
    set_a, set_b = set(a_tokens), set(b_tokens)
    if set_a and set_b and (set_a <= set_b or set_b <= set_a):
        return f"subset:{min(len(set_a), len(set_b))}"

    # Acronym match — first letters of each token
    def acronym(s: str) -> str:
        return "".join(w[0] for w in s.split() if w)
    acr_a, acr_b = acronym(na), acronym(nb)
    if acr_a and acr_b and (acr_a == acr_b or acr_a in nb or acr_b in na):
        return f"acr:{acr_a}"

    return None


# ── Orchestrator ───────────────────────────────────────────────────────────

PHASES = {
    "type_conflict": (_resolve_type_conflicts, "Type conflict resolution"),
    "composite_split": (_resolve_composites, "Composite entity split"),
    "name_variation": (_resolve_name_variations, "Name variation matching"),
}

PHASE_ORDER = ["type_conflict", "composite_split", "name_variation"]


def run_resolver(engine, phases: list[str] | None = None,
                 dry_run: bool = False, force: bool = False,
                 verbose: bool = False) -> dict:
    """Run entity resolution. Returns summary dict."""
    results = {"errors": []}

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                phase VARCHAR(32) PRIMARY KEY,
                last_run_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))

    watermarks = {}
    if not force:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT phase, last_run_at FROM {WATERMARK_TABLE}")
            ).fetchall()
            watermarks = {r[0]: r[1] for r in rows}

    # Check if there are any unresolved entities to process
    with engine.connect() as conn:
        pending = conn.execute(text("""
            SELECT COUNT(*) FROM entities
            WHERE resolution_status IS NULL OR resolution_status = 'unresolved'
        """)).scalar()

    target_phases = phases or PHASE_ORDER
    for phase_name in target_phases:
        if phase_name not in PHASES:
            log.warning("  Unknown phase: %s", phase_name)
            continue

        if phase_name in watermarks and pending == 0:
            log.info("  [SKIP] %s — no pending entities", PHASES[phase_name][1])
            continue
        elif phase_name in watermarks:
            log.info("  %s — watermark exists but %d entities pending",
                     PHASES[phase_name][1], pending)

        log.info("  %s", PHASES[phase_name][1])
        try:
            with engine.begin() as conn:
                phase_fn = PHASES[phase_name][0]
                phase_results = phase_fn(conn, dry_run=dry_run, verbose=verbose)
                if not dry_run:
                    conn.execute(
                        text(f"""
                            INSERT INTO {WATERMARK_TABLE} (phase, last_run_at)
                            VALUES (:pn, now())
                            ON CONFLICT (phase) DO UPDATE SET last_run_at = now()
                        """),
                        {"pn": phase_name},
                    )
                results.update(phase_results)
        except Exception as e:
            log.error("  ✗ Phase %s failed: %s", phase_name, e, exc_info=verbose)
            results["errors"].append({"phase": phase_name, "error": str(e)})
            if not force:
                raise

    return results


def main():
    parser = argparse.ArgumentParser(description="Entity resolution pipeline")
    parser.add_argument("--phase", type=str, help="Run only one phase")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--force", action="store_true", help="Force re-run")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    engine = get_engine()

    phases = [args.phase] if args.phase else None
    results = run_resolver(engine, phases=phases,
                           dry_run=args.dry_run, force=args.force,
                           verbose=args.verbose)

    # Summary
    dry = " (DRY RUN)" if args.dry_run else ""
    parts = []
    for k, v in results.items():
        if k == "errors":
            continue
        if isinstance(v, int) and v > 0:
            parts.append(f"{k}={v}")
    log.info("DONE%s — %s", dry, " | ".join(parts))

    has_errors = bool(results.get("errors"))
    if has_errors:
        log.error("Errors: %d", len(results["errors"]))

    print(json.dumps({
        "phase": "resolver",
        "success": not has_errors,
        **{k: v for k, v in results.items() if isinstance(v, (int, float)) and k != "errors"},
    }))

    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
