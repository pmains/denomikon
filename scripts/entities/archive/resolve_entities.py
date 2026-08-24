#!/usr/bin/env python3
"""
resolve_entities.py — Entity resolution and dedup (Phase 6).

Steps:
  1. Name normalization (strip titles, suffixes, lowercase)
  2. Block duplicates by normalized form
  3. Merge: reassign entity_mentions to canonical entity
  4. Link entities to bodies table
  5. Fix misclassified entity types (org → person)

Usage:
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/resolve_entities.py
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/resolve_entities.py --dry-run
"""

import sys, os, re, logging, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("resolve_entities")

from db import get_engine
from sqlalchemy import text
from datetime import datetime, timezone

# ── Name normalization ──────────────────────────────────────────────
# Strip suffixes like ", Assistant County Manager"
SUFFIX_PATTERNS = [
    r",\s*(?:Attorney|Representative|Staff|Agent|Applicant|Owner|Planner|Manager|Director|Coordinator|Specialist|Officer|Engineer|Analyst|Supervisor|Advisor|Consultant)\b.*",
    r",\s*(?:Mayor|Councilmember|Councilman|Councilwoman|Vice\s+Mayor|Chair|Chairman|Chairwoman|Chairperson|Member|Secretary|Treasurer)\b.*",
    r",\s*(?:County\s+Manager|Assistant\s+County\s+Manager|Deputy\s+County\s+Manager|City\s+Manager|Assistant\s+City\s+Manager)\b.*",
    r",\s*(?:Chief|Deputy|Assistant)\s+\w+\s*(?:Officer|Attorney|Director|Engineer)?\b.*",
    r",\s*(?:PC|PE|JD|PhD|MD|Esq)\b\.?",
    r",\s*(?:MPA|MBA|MS|MA|BS|BA)\b\.?",
    r"\s+Jr\.?\s*,?$", r"\s+Sr\.?\s*,?$", r"\s+I{1,3}\s*,?$",
]

def normalize_name(name: str) -> str:
    """Normalize entity name for dedup blocking."""
    n = name.strip()
    # Strip suffixes
    for pat in SUFFIX_PATTERNS:
        n = re.sub(pat, "", n, flags=re.I)
    # Strip trailing punctuation
    n = re.sub(r"[\s,.;:]+$", "", n)
    return n.strip().lower()


# ── Step 1: Blocking ────────────────────────────────────────────────
def _is_garbage(name: str) -> bool:
    """Check if an entity name is a garbage text fragment rather than a real entity."""
    n = name.strip()
    if not n:
        return True
    # Too long (>80 chars is probably not a real entity name)
    if len(n) > 80:
        return True
    # Starts with lowercase letter (real names start uppercase)
    if n[0].islower():
        return True
    # Contains random preposition fragments
    if re.search(r'^(?:the |a |an |in |on |at |to |for |of |by |with |and |or |is |was |as )', n, re.I):
        return True
    # Contains trailing fragments
    if re.search(r', (?:and |or |the |a |for |in |on |by |to |of |from |with )', n, re.I):
        return True
    # Contains special chars that are unlikely in real names
    if '()' in n or '[]' in n:
        return True
    return False

def build_blocks(conn) -> dict[str, list[tuple]]:
    """Group entities by normalized name. Returns {normal: [(id, name, type)]}."""
    rows = conn.execute(text("""
        SELECT id, name, entity_type, mention_count
        FROM entities
        ORDER BY mention_count DESC NULLS LAST, id
    """)).fetchall()
    # Filter garbage in Python (can't use function in SQL)
    rows = [r for r in rows if not _is_garbage(str(r[1] or ''))]

    blocks: dict[str, list[tuple]] = {}
    for row in rows:
        norm = normalize_name(row[1] or "")
        if len(norm) > 1:
            blocks.setdefault(norm, []).append(row)
    return blocks


def get_canonical(group: list[tuple]) -> int:
    """Pick the canonical entity from a group — most mentions, then earliest id."""
    # Prefer entities with mention_count > 0, then by mention_count desc, then by id asc
    sorted_group = sorted(group, key=lambda r: (-(r[3] or 0), r[0]))
    return sorted_group[0][0]


# ── Step 2: Merge ────────────────────────────────────────────────────
def merge_duplicates(engine, blocks: dict, dry_run: bool = False) -> int:
    """Merge duplicate entities by reassigning entity_mentions to canonical."""
    merged = 0
    now = datetime.now(timezone.utc)

    for norm, group in blocks.items():
        if len(group) <= 1:
            continue

        canonical_id = get_canonical(group)
        canonical_name = group[0][1]

        for row in group:
            entity_id = row[0]
            entity_name = row[1]
            if entity_id == canonical_id:
                continue

            if not dry_run:
                with engine.begin() as conn:
                    # Reassign mentions to canonical entity
                    conn.execute(text("""
                        UPDATE entity_mentions
                        SET entity_id = :new_id, updated_at = :now
                        WHERE entity_id = :old_id
                    """), {"new_id": canonical_id, "old_id": entity_id, "now": now})

                    # Mark resolved
                    conn.execute(text("""
                        UPDATE entities SET
                            canonical_entity_id = :canon_id,
                            resolution_status = 'merged',
                            resolution_method = 'name_dedup',
                            resolution_confidence = 0.95,
                            resolved_at = :now,
                            updated_at = :now
                        WHERE id = :old_id
                    """), {"canon_id": canonical_id, "old_id": entity_id, "now": now})

                    # Update canonical's mention count
                    conn.execute(text("""
                        UPDATE entities SET
                            mention_count = (
                                SELECT COUNT(*) FROM entity_mentions
                                WHERE entity_id = :eid
                            ),
                            resolution_status = 'canonical',
                            updated_at = :now
                        WHERE id = :eid
                    """), {"eid": canonical_id, "now": now})

            merged += 1

    return merged


# ── Step 3: Body linking ────────────────────────────────────────────
def link_to_bodies(engine, dry_run: bool = False) -> int:
    """Match entity names to the bodies table and create IS_SAME_AS edges."""
    linked = 0
    now = datetime.now(timezone.utc)

    with engine.connect() as conn:
        bodies = conn.execute(text("""
            SELECT id, slug, display_name
            FROM bodies
        """)).fetchall()

        for bid, slug, display_name in bodies:
            if not display_name:
                continue
            # Fuzzy match: entity name contains body display_name or vice versa
            rows = conn.execute(text("""
                SELECT id, name FROM entities
                WHERE LOWER(name) LIKE :pat1
                   OR :name ILIKE '%' || name || '%'
                LIMIT 3
            """), {"pat1": f"%{display_name.lower()}%", "name": display_name}).fetchall()

            for entity_id, entity_name in rows:
                if bid and entity_id:
                    if not dry_run:
                        with engine.begin() as wc:
                            # Create relationship edge
                            wc.execute(text("""
                                INSERT INTO entity_relationships (
                                    source_entity_id, target_entity_id,
                                    relationship, source_type, confidence,
                                    created_at, updated_at
                                ) VALUES (
                                    :src, :tgt, 'IS_SAME_AS', 'bodies', 0.95,
                                    :now, :now
                                )
                                ON CONFLICT DO NOTHING
                            """), {"src": entity_id, "tgt": int(bid),
                                    "tgt2": int(bid) + 100000,  # offset for body IDs
                                    "now": now})
                    linked += 1

    return linked


# ── Main ────────────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv

    engine = get_engine()
    start_ts = time.time()

    # Phase 6a: Entity dedup
    log.info("Building entity blocks..." if not dry_run else "DRY RUN — building entity blocks...")
    with engine.connect() as conn:
        blocks = build_blocks(conn)

    dup_groups = sum(1 for g in blocks.values() if len(g) > 1)
    dup_entities = sum(len(g) - 1 for g in blocks.values() if len(g) > 1)
    log.info("Found %d duplicate groups (%d entities to merge)", dup_groups, dup_entities)

    merged = merge_duplicates(engine, blocks, dry_run=dry_run)
    log.info("Merged %d entities" if not dry_run else "DRY RUN — would merge %d entities", merged)

    elapsed = time.time() - start_ts
    log.info("DONE in %.0fs", elapsed)


if __name__ == "__main__":
    main()
