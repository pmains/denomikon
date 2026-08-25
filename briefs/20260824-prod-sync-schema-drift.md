# Brief — Prod sync schema drift: findings, backfill & fixes (2026-08-24)

**Status:** Backfill ✅ | event tables at parity ✅ | code fix executed ✅ |
migration + orphan cleanup executed ✅ | full re-sync launched.
**Supersedes:** the 15:36 version of this brief (stale after the 15:38 failure).
**Related:** `20260824-prod-schema-migration.md`, `20260824-sync-script-code-fixes.md`,
`20260824-dev-data-cleanup.md`, `20260824-prod-sync-root-cause.md`.

## Timeline (2026-08-24)

- **14:21** Full sync launched (`SYNC_MODE=full`, `data/sync-full-20260824-1421.log`).
  Killed 15:23 per Pete. Not hung — crawling through `entity_relationships`
  (~13 min / 2,000-row chunk) because of the JSONB bulk-insert bug.
- **15:35** `supporting_documents` schema widened on prod (varchar(64) → 256 for
  `agenda_item_id`, `body`); backfill ran (`data/backfill-sd-20260824-1535.log`):
  **1,580 rows copied, prod = 64,325 = dev ✅**.
- **15:36** Incremental sync (`data/sync-incremental-20260824-1536.log`):
  copied `meeting_events` (+7,984), `meeting_event_extractions` (+7,985),
  `event_participants` (→ 15,941). **Sync then died at 15:38:10** — see below.
- **16:1x** Post-mortem: the failure was a **sync script bug, not schema drift**.
  Data copy had completed. Event tables verified at parity on prod.

## Root cause of the 15:38 failure (NEW — this was the live blocker)

`event_participants` is a **join table with a composite PK and NO `id` column**
(verified identical on dev and prod):

```
PRIMARY KEY (meeting_event_id, entity_id, role_in_event)
```

`_upsert_table()`'s post-loop block in `sync_prod.py` unconditionally runs:

```python
max_id = c.execute(SELECT COALESCE(MAX(id), 0) FROM event_participants)  # ✗ no id column
```

`UndefinedColumn` is raised, swallowed by `except Exception: pass` — but
PostgreSQL has now **aborted the connection's transaction**, so the very next
statement on that connection (`SELECT COUNT(*)`) fails with
`InFailedSqlTransaction`, the sync exits non-zero, and every full-sync run of
`event_participants` fails *after* copying all rows. The checkpoints for
`meeting_events` / `meeting_event_extractions` / `event_participants` never
advance, so the next run re-copies everything — wasteful but not lossy.

**Fix (executed 16:1x in `scripts/db/sync_prod.py`):**
sequence reset is now skipped when the table has no `id` column, and the
sequence block rolls back on error so it can't poison the count query.

## Verified schema drift — dev vs prod (column-level, 15:35 + 16:1x)

### 1. `supporting_documents` — varchar length drift (CAUSAL, FIXED ✅)

| column | dev | prod (before) |
|---|---|---|
| `agenda_item_id` | varchar(256) | varchar(64) |
| `body` | varchar(256) | varchar(64) |

Every insert of a doc with a long `agenda_item_id`/`body` failed
(`StringDataRightTruncation`), the row was skipped, and the `_sync_meta`
checkpoint advanced anyway → 1,580 docs (created 2026-07-27, ids 110897+)
permanently missing on prod → nightly `meeting_events` FK-fails on missing
parents → cascade to extractions / participants.
**Fix:** widened prod to varchar(256); backfilled 1,580 rows. Done.

### 2. `entities` — dev-only resolution columns (silent data drop, FIXED ✅)

Prod was missing: `canonical_entity_id`, `resolution_block_key`,
`resolution_status`, `resolution_confidence`, `resolution_method`,
`resolved_at`. `_column_intersection()` silently drops dev-only columns →
entity resolution data never reached prod. **Fix:** ALTER TABLE executed
16:1x (migration brief). Columns populate on next full sync.

### 3. `agenda_items` — dev-only columns (FIXED ✅)

Prod was missing: `swept_at`, `lifecycle_status`. ALTER TABLE executed.

### 4. `entity_relationships` — dev-only columns + FK asymmetry (FIXED ✅)

- Prod was missing: `source_type`, `source_id`. ALTER TABLE executed.
- **Dev has NO FKs; prod has FKs** (`from_entity_id`/`to_entity_id` →
  `entities(id)`). Dev holds **8 orphaned rows** (ids 3372–3379 referencing
  entity ids 20523–20535, 113, 143, 161 that don't exist even on dev). Prod
  rejects them every run → skipped forever. **Fix:** 8 orphans deleted on dev
  16:1x (cleanup brief); FK parity on dev deferred (flagged for Pete).

### 5. `meeting_members` — benign default drift

Prod has defaults (`body=''`, `meeting_db_id=0`, `created_at/updated_at=now()`)
that dev lacks. Harmless.

## Bugs in `scripts/db/sync_prod.py` (confirmed from logs; FIXED 16:1x)

1. **Sequence reset assumes an `id` column** (NEW) — killed every
   `event_participants` sync after the copy; aborted the transaction so the
   post-loop count failed. Fixed: skip when no `id` column; rollback on error.
2. **Bulk INSERT can't adapt JSONB dicts** — `entity_relationships.metadata`
   (jsonb, 9,879 non-null dicts) → every 2,000-row chunk fails with
   `can't adapt type 'dict'`; falls back to row-by-row (`json.dumps` in the
   fallback path only) → ~13 min/chunk. Fixed: bulk path now serializes
   dict/list values like the fallback.
3. **Checkpoint advances past skipped/failed rows** — `_set_last_sync` ran
   unconditionally. Fixed: checkpoint only advances when the table copied
   with zero skipped rows; per-table skip counts logged in the done line.
4. **Validation failure exits 0 / silent** — `_validate` mismatch only logged ⚠.
   Fixed: sync now exits non-zero when any table skipped rows or validation
   fails (so the nightly error report fires).

## Prod data state vs dev (verified 16:1x, after backfill + event copy)

| table | dev | prod | status |
|---|---|---|---|
| supporting_documents | 64,325 | 64,325 | ✅ |
| meeting_events | 19,544 | 19,544 | ✅ |
| meeting_event_extractions | 19,544 | 19,544 | ✅ |
| event_participants | 15,941 | 15,941 | ✅ |
| meeting_event_types | 17 | 17 | ✅ |
| entity_relationships | 9,879 | ~9,690 | ⏳ re-syncing (was 2026-08-11) |
| entities / agenda_items | — | — | ⏳ full re-sync populates new columns |

Prod stale (sync never deletes — deferred design question): member_votes +1,080;
agenda_items +203; body_memberships +124; entity_relationships +189; cases +33.

## Follow-ups

1. ✅ Backfill supporting_documents.
2. ✅ Fix sync_prod.py (sequence reset, JSONB bulk, checkpoint integrity, exit code).
3. ✅ Migrate prod schema (entities, agenda_items, entity_relationships).
4. ✅ Delete 8 orphaned entity_relationships on dev.
5. ⏳ Full re-sync launched (nohup) — confirm all tables `done` + validation pass.
6. Confirm tonight's 4 AM incremental run passes validation.
7. Flag for Pete: dev has no FKs on entity_relationships (parity vs prod);
   delete-propagation for stale prod rows; alert on nightly sync non-zero.
