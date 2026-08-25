# Brief — Entity pipeline dead (import bug) + prod sync stale rows (2026-08-25)

**Status:** Diagnosis ✅ | entity pipeline fix in progress | prod reconcile in progress
**Related:** `20260824-prod-sync-schema-drift.md` (yesterday's schema/backfill work),
`docs/roadmaps/GRAPH_BUILDER_FIXES.md` (known graph_builder bugs, still unfixed),
`docs/entities/PIPELINE.md`, `docs/roadmaps/ROADMAP.md`.

---

## Issue 1 — Entity pipeline has been dead for ~a month (P1)

### Symptom

Every nightly entities log since at least 2026-08-10 shows the same no-op:

```
[MISSING] Structured triples from DB tables — import error: No module named 'scripts'
[MISSING] Entity extraction from supporting_documents text content — import error: ...
[MISSING] Semi-structured header regex (Applicant:, Attorney:, etc.) — import error: ...
[MISSING] ML role classification ... — import error: ...
[MISSING] Entity resolution / dedup — import error: ...
[MISSING] 3-stage event extraction ... — import error: ...
DONE — 0 phase(s) ran | 6 skipped | 0s
```

Dev watermarks confirm the pipeline last actually ran **2026-07-28**:
`_graph_builder_watermark` (07-28 09:08), `_resolver_watermark` (07-28 09:38),
`_pattern_cascade_watermark` (07-28 17:03). Entity data on dev has been frozen
for a month. `_detect_entities_watermark` (the orchestrator's own table) does
**not exist** — the orchestrator has never completed a single run.

### Root cause

`scripts/entities/detect_entities.py` line 41:

```python
sys.path.insert(0, "scripts")   # relative path — resolves against CWD
```

Phase modules are imported via `importlib.import_module("scripts.entities.X")`
— that requires the **repo root** on `sys.path` (so `scripts` is a package).
Inserting the *relative* string `"scripts"` only works when CWD happens to be
the repo root, and even then it makes `db.core` importable, not
`scripts.entities.*`. The cron invocation
(`sync_log.sh` → `python3 -u <abs path>/detect_entities.py`) runs from an
unpredictable CWD → every phase import fails → whole pipeline no-ops.

Same bug pattern in `graph_builder.py`, `pattern_cascade.py`, `resolver.py`
(all do `sys.path.insert(0, "scripts")`).

### Fix (executed 2026-08-25)

Compute repo root + scripts dir from `__file__` (CWD-independent) and insert
both:

```python
_ENTITIES_DIR = os.path.dirname(os.path.abspath(__file__))   # .../scripts/entities
_SCRIPTS_DIR = os.path.dirname(_ENTITIES_DIR)                 # .../scripts
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)                    # repo root
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
```

Applied to `detect_entities.py`, `graph_builder.py`, `pattern_cascade.py`,
`resolver.py`.

### Latent bug found while in there (P1 — would have broken the first successful run)

`_get_watermarks()` in `detect_entities.py` creates watermark tables inside
`with engine.connect():` — **no commit**. SQLAlchemy 2.0 autobegin rolls the
DDL back on connection close, so `_detect_entities_watermark` never persists.
The phases' own watermark tables (created via `engine.begin()`) persist, but
the orchestrator's does not → `_mark_watermark()` would have hit
`UndefinedTable` on the first phase that actually ran. **Fix:** changed
`_get_watermarks` to `with engine.begin():` (commits DDL + returns watermarks).

### Secondary issues (roadmap items, not blockers)

- **role_classifier needs ML deps.** Module imports `sklearn` +
  `sentence_transformers` at top level; neither is installed in `.venv`
  (checked 2026-08-25). Phase will skip (`allow_skip=True`, non-critical)
  until deps installed.
- **GRAPH_BUILDER_FIXES.md bugs still open.** C1 (entity count vs edge count
  swapped in return), C2 (PRESENT_AT edges silently dropped — meeting entity
  never created), C3 (HAS_RECOMMENDATION edges dropped — recommendation
  entity never created, wrong type), S1 (entity cache mutated inside
  transaction — phantom entries on rollback). Status was "Draft — awaiting
  approval." These are real correctness bugs in the FIRST phase; they will
  produce incomplete/misreported graph data now that the pipeline runs.
  Recommend fixing before trusting Phase 1 output (see roadmap).

### Acceptance criteria

- `detect_entities.py` run shows phases importing and executing
  (`1 phase(s) ran` minimum, no `No module named 'scripts'`).
- `_detect_entities_watermark` exists in dev after a run.
- Nightly entities log stops showing "0 phase(s) ran | 6 skipped".

---

## Issue 2 — Prod sync validation fails nightly; prod accumulates stale rows (P1)

### Symptom

Nightly `maricopa-prod-sync` (4 AM) now exits non-zero (the loud-failure fix
from 08-24 works) because 5 tables never converge — all have **prod > dev**:

| table | dev | prod | delta | note |
|---|---|---|---|---|
| member_votes | 93,394 | 94,585 | **+1,191** | grew +111 overnight |
| agenda_items | 111,968 | 112,171 | +203 | stable |
| body_memberships | 73 | 197 | +124 | stable |
| entity_relationships | 9,871 | 10,068 | +197 | grew +8 (orphans deleted on dev 08-24) |
| cases | 10,313 | 10,346 | +33 | stable |

The 08-24 full re-sync (16:20, finished in 7,872s) upserted everything but the
deltas persisted — upsert can only add/update, never delete.

### Root cause

`sync_prod.py` is **upsert-only**. Rows deleted on dev (re-scraped meetings,
orphan cleanup, re-seeded memberships) are never removed from prod. Every dev
delete is a permanent prod zombie. Deltas grow exactly in step with dev
deletions: +8 entity_relationships overnight = the 8 orphans deleted on dev
08-24; +111 member_votes = dev re-scrape churn.

### Fix (executed 2026-08-25)

Added delete-propagation to `sync_prod.py`:

- New `_reconcile_table(dev_engine, prod_engine, table, dry_run)` — deletes
  prod rows whose single-column PK is absent from dev, in chunks, with
  per-table error isolation (FK-violations logged, not fatal).
- New CLI flags: `--reconcile` (reconcile after upserts), `--reconcile-only`
  (skip upserts, just reconcile + validate), `--reconcile-dry-run` (preview
  only).
- Tables processed in FK-safe order (children before parents) so deletes
  never violate prod FKs (`entity_relationships → entities`,
  `meeting_events → agenda_items/supporting_documents/meeting_event_types`).
- `sync.sh` updated to pass `--reconcile` so the nightly run converges.

### Acceptance criteria

- Dry-run shows exactly the 5 stale tables with expected deltas.
- Reconcile run brings all 5 tables to parity; `_validate` passes.
- Next nightly sync exits 0.

---

## Follow-ups / roadmap

1. ✅ Entity pipeline import fix (this brief).
2. ✅ sync_prod.py reconcile (this brief).
3. 🔲 Fix GRAPH_BUILDER_FIXES.md C1/C2/C3/S1 before trusting Phase 1 output.
4. 🔲 Install sklearn + sentence-transformers in `.venv` (or vendor models) to
   un-skip role_classifier.
5. 🔲 Add an entities-phase alert: if a nightly entities log shows
   "0 phase(s) ran", fire the error report. (Currently silent — same failure
   mode as the old prod sync.)
6. 🔲 Consider `_sync_meta` pending-retry tracking for skipped rows (from
   08-24 brief) — reconcile supersedes most of it, but skipped rows during
   upsert should still block checkpoint advance (already implemented 08-24).
