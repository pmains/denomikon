# Brief — Prod sync root cause & fix (2026-08-24)

**Status:** Operational fix in progress; code fix needed (specialist).

## Symptom

Nightly prod sync (`maricopa-prod-sync`, 4 AM) has been failing since at least
Aug 18 (logs: `data/sync/prod-sync-2026-08-{18,19,23,24}.log`).

Failure chain each night:

1. `meeting_events` (full-sync table) fails chunk inserts with
   `ForeignKeyViolation: meeting_events_supporting_doc_id_fkey` — e.g.
   `Key (supporting_doc_id)=(110897) is not present in table "supporting_documents"`.
2. Script retries chunk row-by-row, permanently skipping offending rows.
3. `event_participants` then FK-fails against the missing `meeting_events`.
4. Validation `SELECT COUNT(*) FROM event_participants` aborts the transaction
   (`InFailedSqlTransaction`), sync exits non-zero.
5. Nightly run took ~4h (04:00 → 07:56) due to row-by-row skip retries.

## Root cause

Prod is missing **1,580 supporting_documents** (62,745 vs 64,325 on dev; doc
110897, created 2026-07-27, is one). Event tables (`meeting_events`,
`event_participants`) are in `FULL_SYNC_TABLES` (no `updated_at`), so they
re-sync every night and collide with the missing parents.

The gap persists because `_sync_meta.last_sync_at` for `supporting_documents`
advanced to 2026-08-24 11:01 UTC **despite** missing rows — i.e. checkpoints
advance even when rows fail or are skipped. Incremental tables therefore never
retry the missing rows. Matches Pete's hypothesis: incomplete previous syncs.

Secondary finding (from July 13 logs): prod `case_events` lacks the
`updated_at` column (`UndefinedColumn` in incremental bootstrap). Column
drift; `_column_intersection` sidesteps it in full mode but the drift is real.

## Fix executed (ops, 2026-08-24 ~14:21)

```bash
set -a && source .env && set +a
SYNC_MODE=full nohup python3 -u scripts/db/sync_prod.py > data/sync-full-$(date +%Y%m%d-%H%M).log 2>&1 &
```

Backfills every table unconditionally (chunked, upsert-only, advisory-locked,
app stays live). Verify after completion:
- Log shows all tables `done` + validation pass.
- `SELECT count(*) FROM supporting_documents` on prod == 64,325 (dev).

## Code fix needed (Software Engineer)

1. **Checkpoint integrity:** only advance `_sync_meta.last_sync_at` for a
   table when its copy completes with zero skipped/failed rows; otherwise
   leave the checkpoint behind (or set a `dirty` flag) so the next run
   retries the missing rows.
2. **Loud failure:** validation failure must exit non-zero / signal the
   nightly error report. It has been failing silently for a week.
3. **Optional:** alert (Slack/email) when `maricopa-prod-sync` exits non-zero.

## Follow-ups

- Confirm tonight's 4 AM incremental run succeeds (parents now on prod).
- Audit `case_events` column drift on prod vs dev; run migration if needed.
