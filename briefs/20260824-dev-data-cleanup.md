# Brief — Dev data cleanup: orphaned entity_relationships (Data Engineer)

**Status:** Open — 8 rows; blocks nothing today but fails every prod sync.
**Context:** `briefs/20260824-prod-sync-schema-drift.md`.
**Target:** dev DB (`DATABASE_URL`, poliscopic_dev).

## Problem

Dev `entity_relationships` has **no FK constraints** (prod has FKs:
`from_entity_id`/`to_entity_id` → `entities(id)`). 8 rows on dev reference
entity ids that don't exist **even on dev** — dev has no FK to prevent them,
prod rejects them on every sync (`entity_relationships_source_entity_id_fkey`
violation), and they get skipped forever.

## Verified orphans (2026-08-24)

```sql
SELECT er.id, er.from_entity_id, er.to_entity_id
FROM entity_relationships er
LEFT JOIN entities e1 ON e1.id = er.from_entity_id
LEFT JOIN entities e2 ON e2.id = er.to_entity_id
WHERE e1.id IS NULL OR e2.id IS NULL;
-- → 8 rows: ids 3372–3379; from_entity_id 20523–20535, 113, 143, 161 missing
```

## Suggested fix

```sql
BEGIN;
DELETE FROM entity_relationships
WHERE id IN (3372, 3373, 3374, 3375, 3376, 3377, 3378, 3379);
-- verify: SELECT COUNT(*) FROM entity_relationships er
--   LEFT JOIN entities e1 ON e1.id = er.from_entity_id
--   LEFT JOIN entities e2 ON e2.id = er.to_entity_id
--   WHERE e1.id IS NULL OR e2.id IS NULL;  -- → 0
COMMIT;
```

## Deeper question (flag for Pete / Software Engineer)

Dev has **no FK constraints at all** on `entity_relationships` while prod does.
Options:

1. Add the same FKs on dev (parity, prevents future orphans). Requires the
   orphan cleanup first. Note: dev may hold other latent orphans in other
   tables if FKs are broadly missing — worth an audit.
2. Drop FKs on prod (match dev) — reduces prod safety; not recommended.

Recommend option 1 after cleanup, but confirm with Pete before adding
constraints to dev (could surface more historical orphans).

## Acceptance criteria

- Orphan count = 0 on dev.
- Next prod sync of `entity_relationships` shows no FK-violation skips.
- (Optional) FK constraints on dev match prod.
