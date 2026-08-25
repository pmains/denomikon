# Brief — Prod schema migration: add dev-only columns (Data Engineer)

**Status:** Open — needed for data parity; not blocking the backfill.
**Context:** `briefs/20260824-prod-sync-schema-drift.md`.
**Target:** DigitalOcean prod DB (`PROD_DATABASE_URL`), public schema.

## Problem

Dev PostgreSQL has columns that prod is missing. `sync_prod.py`'s
`_column_intersection()` silently drops dev-only columns, so this drift
causes **silent data loss** on prod — no error, no alert, the data just never
arrives.

## Verified drift (dev → prod, 2026-08-24)

### `entities` — add 6 columns (entity resolution pipeline output)

| column | dev type |
|---|---|
| `canonical_entity_id` | integer |
| `resolution_block_key` | character varying |
| `resolution_status` | character varying NOT NULL default 'unresolved' |
| `resolution_confidence` | numeric |
| `resolution_method` | character varying |
| `resolved_at` | timestamp with time zone |

### `agenda_items` — add 2 columns

| column | dev type |
|---|---|
| `swept_at` | timestamp with time zone |
| `lifecycle_status` | character varying (default NULL) |

### `entity_relationships` — add 2 columns

| column | dev type |
|---|---|
| `source_type` | character varying |
| `source_id` | integer |

## Suggested migration (idempotent)

```sql
-- entities
ALTER TABLE entities
  ADD COLUMN IF NOT EXISTS canonical_entity_id INTEGER,
  ADD COLUMN IF NOT EXISTS resolution_block_key VARCHAR,
  ADD COLUMN IF NOT EXISTS resolution_status VARCHAR NOT NULL DEFAULT 'unresolved',
  ADD COLUMN IF NOT EXISTS resolution_confidence NUMERIC,
  ADD COLUMN IF NOT EXISTS resolution_method VARCHAR,
  ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- agenda_items
ALTER TABLE agenda_items
  ADD COLUMN IF NOT EXISTS swept_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR DEFAULT NULL;

-- entity_relationships
ALTER TABLE entity_relationships
  ADD COLUMN IF NOT EXISTS source_type VARCHAR,
  ADD COLUMN IF NOT EXISTS source_id INTEGER;
```

Verify exact dev types first via:
```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema='public' AND table_name IN ('entities','agenda_items','entity_relationships')
  AND column_name IN ('canonical_entity_id','resolution_block_key','resolution_status',
                      'resolution_confidence','resolution_method','resolved_at',
                      'swept_at','lifecycle_status','source_type','source_id');
```

## Notes / cautions

- `ALTER TABLE ... ADD COLUMN` on DO Managed PG takes an ACCESS EXCLUSIVE lock;
  tables are large (entities ~98k rows). Run in a maintenance window or use
  `pg_repack` if locking matters. Prod app reads continuously — brief Pete
  before running during peak hours. (`_ensure_updated_at_on_prod` in
  sync_prod.py already does ALTERs at 4 AM, so precedent exists.)
- After migration, run a full sync for affected tables (or wait for next
  full/nightly) so the new columns get populated.
- Re-run the column diff to confirm zero drift:
  `diff <(psql dev -Atc ...) <(psql prod -Atc ...)` per table.

## Acceptance criteria

- Column diff dev vs prod shows no missing columns for the 23 sync tables
  (excluding dev-only pipeline watermark tables, which are intentionally
  not synced).
- `entities`, `agenda_items`, `entity_relationships` rows on prod carry the
  new column values after the next full sync.
