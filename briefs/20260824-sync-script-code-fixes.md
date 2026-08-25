# Brief — sync_prod.py code fixes (Software Engineer)

**Status:** Open — blocking reliable nightly prod sync.
**Context:** `briefs/20260824-prod-sync-schema-drift.md` (findings + backfill).
**File:** `scripts/db/sync_prod.py`

## Problem 1 — Checkpoints advance past skipped/failed rows (CRITICAL)

`_upsert_table()` calls `_set_last_sync(prod_engine, table)` unconditionally
after the chunk loop, even when:
- a chunk `INSERT` failed and fell back to row-by-row, and/or
- individual rows were skipped (`Skipped row id=...`).

Consequence: a row that fails once is never retried by incremental sync — the
`_sync_meta.last_sync_at` watermark moves past it. This is the mechanism that
permanently stranded 1,580 `supporting_documents` (created 2026-07-27) and the
8 orphaned `entity_relationships` rows.

### Required behavior

Only advance `last_sync_at` when the table's copy completed with **zero
skipped rows**. When any row is skipped:

- leave the checkpoint where it was (so the next run retries), **or**
- record skipped row ids in `_sync_meta` (e.g. a `pending_retry JSONB` column)
  and have the next run target exactly those ids.

Skips must also be surfaced to the run summary / exit code (see Problem 3).

Implementation notes:

- Track `skipped` count inside `_upsert_table()`; it already exists as a local
  in the row-by-row fallback — hoist it to function scope and use it in the
  checkpoint decision.
- `total == 0` early-return already skips checkpoint update correctly — keep
  that semantics.
- `FULL_SYNC_TABLES` (event tables) always re-sync everything, so checkpoint
  logic matters less there — but skipped rows there still deserve a retry the
  next night, which full-sync gives them for free. Still report them loudly.

## Problem 2 — Bulk INSERT can't adapt JSONB dicts (CRITICAL)

`entity_relationships.metadata` is `jsonb`. The bulk path builds
`params[f"r{i}_{c}"] = row[c]` and passes dicts straight to psycopg2 →
`can't adapt type 'dict'` on every 2,000-row chunk. The row-by-row fallback
works because it does `json.dumps(v) if isinstance(v, dict)`.

Consequence: `entity_relationships` has not completed a sync since 2026-08-11;
a full sync crawls at ~13 min per 2,000 rows.

### Required behavior

Serialize dict/list values in the **bulk path** exactly like the fallback:

```python
params[f"r{i}_{c}"] = json.dumps(v) if isinstance(v, (dict, list)) else v
```

Same for the `_cleanup_*` helpers if they ever pass JSON columns (they
currently only use unique/pk columns — verify).

## Problem 3 — Silent failures (HIGH)

- `_validate()` sets `ok = False` on count mismatch but the process still exits
  0. `main()` should return/expose validation failure so the nightly
  `maricopa-prod-sync` cron reports non-zero.
- `Skipped row` warnings are easy to miss in a multi-MB log. Add a final
  summary line per table: `X rows skipped` when X > 0.

### Required behavior

- `sync_prod.py` exits non-zero if any table skipped rows or validation
  failed.
- Log a per-table skip count in the `done:` line.
- (Optional) `--strict` flag: exit non-zero on skip without waiting for
  validation.

## Problem 4 — Delete propagation (MEDIUM, design question)

Sync is upsert-only; rows deleted on dev accumulate on prod forever
(`member_votes` +1,080, `agenda_items` +203, `body_memberships` +124,
`entity_relationships` +189, `cases` +33). Options:

1. Do nothing (accept stale rows) — current.
2. Full-table mirror for small tables (delete prod rows not in dev) — risky
   with FK order.
3. Soft-delete / tombstone columns on dev — needs scraper changes.

Recommend deferring; flag for Pete. Do not implement in this pass.

## Acceptance criteria

1. Re-run a sync with a deliberately skipped row → checkpoint does not
   advance past it; next run retries it.
2. `entity_relationships` full sync completes in bulk (no `can't adapt type
   'dict'`), no row-by-row fallback.
3. `sync_prod.py; echo $?` → non-zero when any rows skipped or counts differ.
4. Nightly run log shows per-table skip counts.
