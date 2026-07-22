#!/usr/bin/env python3
"""
Dev → prod incremental sync via direct SQLAlchemy connection (no FDW).

Only copies rows that have changed since the last sync, using updated_at
timestamps.  Checkpoints are stored in a _sync_meta table on prod.

First run performs a full sync for all tables.  Subsequent runs only copy
rows where updated_at > last_sync_at for that table.

Usage:
    source .env && python3 -u scripts/db/sync_prod.py

Environment:
    DATABASE_URL          Dev database  (Windows via Tailscale, or local)
    PROD_DATABASE_URL     Prod database (DO Managed PostgreSQL)
    BATCH_SIZE            Rows per chunk (default: 2000)
    BATCH_SLEEP_MS        Sleep between chunks, milliseconds (default: 100)
    SYNC_MODE             "incremental" (default) or "full" (force full resync)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect as sa_inspect, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync")

LOCK_ID = 184_729_583  # arbitrary bigint for pg_advisory_lock
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2000"))
BATCH_SLEEP_S = int(os.environ.get("BATCH_SLEEP_MS", "100")) / 1000.0
SYNC_MODE = os.environ.get("SYNC_MODE", "incremental").lower()

ALL_SYNC_TABLES = [
    "agenda_item_votes",
    "agenda_items",
    "body_memberships",
    "body_seats",
    "case_events",
    "cases",
    "executive_session_participants",
    "jurisdictions",
    "meeting_attendance",
    "meeting_members",
    "meetings",
    "member_votes",
    "persons",
    "public_bodies",
    "pz_item_details",
    "supporting_documents",
]

# Tables that should always do a full sync (no updated_at, or very small)
FULL_SYNC_TABLES: set[str] = set()

# Auto-generated / derived columns to exclude from sync
AUTO_COLUMNS: dict[str, set[str]] = {
    "supporting_documents": {"search_vector"},
}

EXCLUDED_TABLES = {
    "admin_users", "admin_notifications", "article_sources", "article_tags",
    "articles", "dismissed_suggestions", "media_images", "public_body_members",
    "scanned_agenda_text", "skeet_drafts", "tags", "topic_weekly_reports",
    "topics",
}


# ── URL resolution ──


def _mask_url(url: str) -> str:
    """Mask the password portion of a PostgreSQL URL for logging."""
    return re.sub(r'(//[^:]+:).+?(@)', r'\1****\2', url)


def _resolve_dev_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("Set DATABASE_URL to your dev database")
        sys.exit(1)
    log.info("Dev:   %s", _mask_url(url))
    return url


def _resolve_prod_url() -> str:
    url = os.environ.get("PROD_DATABASE_URL")
    if not url:
        log.error("Set PROD_DATABASE_URL")
        sys.exit(1)
    log.info("Prod:  %s", _mask_url(url))
    return url


# ── Schema helpers ──


def _pk_cols(engine, table: str) -> list[str]:
    inspector = sa_inspect(engine)
    pk = inspector.get_pk_constraint(table)
    if pk and pk.get("constrained_columns"):
        return list(pk["constrained_columns"])
    # Fallback to "id"
    return ["id"]


def _quoted_cols(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


def _column_intersection(dev_engine, prod_engine, table: str) -> list[str]:
    dev_cols = {
        c["name"] for c in sa_inspect(dev_engine).get_columns(table)
        if c["name"] != "rowid"
    }
    prod_cols = {
        c["name"] for c in sa_inspect(prod_engine).get_columns(table)
        if c["name"] != "rowid"
    }
    return sorted(dev_cols & prod_cols)


def _table_has_updated_at(engine, table: str) -> bool:
    """Check if a table has an 'updated_at' column."""
    cols = {c["name"] for c in sa_inspect(engine).get_columns(table)}
    return "updated_at" in cols


def _ensure_updated_at_on_prod(prod_engine):
    """Add updated_at to tables that are missing it on prod."""
    needs_updated_at = [
        "agenda_items",
        "case_events",
    ]
    for table in needs_updated_at:
        if _table_has_updated_at(prod_engine, table):
            continue
        log.info("  Adding updated_at to %s on prod...", table)
        with prod_engine.begin() as c:
            c.execute(text(
                f'ALTER TABLE "{table}" '
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ))
            c.execute(text(
                f'UPDATE "{table}" SET updated_at = created_at '
                "WHERE updated_at != created_at"
            ))
        log.info("    done")


# ── Sync metadata (_sync_meta table on prod) ──


def _ensure_sync_meta_table(prod_engine):
    """Create _sync_meta table on prod if it doesn't exist."""
    inspector = sa_inspect(prod_engine)
    if "_sync_meta" in inspector.get_table_names():
        return
    log.info("  Creating _sync_meta table on prod...")
    with prod_engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS _sync_meta (
                table_name   TEXT PRIMARY KEY,
                last_sync_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
    log.info("    done")


def _get_last_sync(prod_engine, table: str) -> datetime | None:
    """Return the last sync timestamp for a table, or None if never synced."""
    with prod_engine.connect() as c:
        row = c.execute(
            text("SELECT last_sync_at FROM _sync_meta WHERE table_name = :t"),
            {"t": table},
        ).fetchone()
    return row[0] if row else None


def _set_last_sync(prod_engine, table: str, when: datetime | None = None):
    """Update (or insert) the last-sync timestamp for a table."""
    when = when or datetime.now(timezone.utc)
    with prod_engine.begin() as c:
        c.execute(
            text("""
                INSERT INTO _sync_meta (table_name, last_sync_at, updated_at)
                VALUES (:t, :w, NOW())
                ON CONFLICT (table_name) DO UPDATE
                SET last_sync_at = :w, updated_at = NOW()
            """),
            {"t": table, "w": when},
        )


# ── Secondary unique constraint handling ──


def _detect_secondary_uniques(engine, table: str) -> list[list[str]]:
    inspector = sa_inspect(engine)
    pk_cols = set(_pk_cols(engine, table))
    uniques = []
    for ix in inspector.get_indexes(table):
        cols = list(ix["column_names"])
        if ix.get("unique") and set(cols) != pk_cols:
            uniques.append(cols)
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT conname, pg_get_constraintdef(oid) "
            f"FROM pg_constraint WHERE conrelid = '{table}'::regclass "
            f"AND contype = 'u'"
        )).fetchall()
        for conname, defn in rows:
            m = re.search(r'UNIQUE\s*\(([^)]+)\)', defn)
            if m:
                cols = [c.strip().strip('"') for c in m.group(1).split(',')]
                if set(cols) != pk_cols and cols not in uniques:
                    uniques.append(cols)
    return uniques


def _cleanup_secondary_conflicts(
    prod_engine, table: str,
    chunk_rows: list[dict],
    secondary_uniques: list[list[str]],
    pk_cols: list[str],
):
    if not secondary_uniques or not chunk_rows:
        return
    with prod_engine.begin() as c:
        for uq_cols in secondary_uniques:
            if len(uq_cols) == 1:
                uq_col = uq_cols[0]
                pk_col = pk_cols[0]
                values = [row[uq_col] for row in chunk_rows if row.get(uq_col) is not None]
                pks = [row[pk_col] for row in chunk_rows if row.get(uq_col) is not None]
                if not values:
                    continue
                placeholders_val = ", ".join(f":v{i}" for i in range(len(values)))
                placeholders_pk = ", ".join(f":p{i}" for i in range(len(pks)))
                params = {}
                for i, v in enumerate(values):
                    params[f"v{i}"] = v
                for i, p in enumerate(pks):
                    params[f"p{i}"] = p
                deleted = c.execute(
                    text(
                        f'DELETE FROM public."{table}"\n'
                        f' WHERE "{uq_col}" IN ({placeholders_val})\n'
                        f'   AND "{pk_col}" NOT IN ({placeholders_pk})'
                    ),
                    params,
                ).rowcount
                if deleted:
                    log.info("    ─ cleaned %d prod row(s) for %s", deleted, uq_cols)
            else:
                for row in chunk_rows:
                    match = " AND ".join(
                        f'"{c}" = :_{i}' for i, c in enumerate(uq_cols)
                    )
                    not_pk = " OR ".join(
                        f'"{c}" <> :_{len(uq_cols) + i}' for i, c in enumerate(pk_cols)
                    )
                    params = {}
                    for i, col in enumerate(uq_cols):
                        params[f"_{i}"] = row[col]
                    for i, col in enumerate(pk_cols):
                        params[f"_{len(uq_cols) + i}"] = row[col]
                    deleted = c.execute(
                        text(
                            f'DELETE FROM public."{table}"\n'
                            f' WHERE {match}\n'
                            f'   AND ({not_pk})'
                        ),
                        params,
                    ).rowcount


# ── Table sync (incremental) ──


def _upsert_table(
    dev_engine, prod_engine, table: str, cols: list[str],
    *, is_full_sync: bool = False,
):
    """Upsert changed rows from dev → prod for one table.

    When is_full_sync is True (or no checkpoint exists), syncs ALL rows.
    Otherwise only syncs rows where updated_at > last_sync_at.
    """
    pk_cols = _pk_cols(prod_engine, table)
    pk_col = pk_cols[0] if pk_cols else "id"
    pk_sql = _quoted_cols(pk_cols)
    col_sql = _quoted_cols(cols)

    secondary_uniques = _detect_secondary_uniques(prod_engine, table)

    update_set = ", ".join(
        f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in pk_cols
    )
    conflict_clause = (
        f"ON CONFLICT ({pk_sql}) DO UPDATE SET {update_set}"
        if update_set else "ON CONFLICT DO NOTHING"
    )

    # Determine sync range
    last_sync = _get_last_sync(prod_engine, table) if not is_full_sync else None
    incremental = last_sync is not None and not is_full_sync

    # Count rows to sync
    with dev_engine.connect() as c:
        if incremental:
            count_sql = text(
                f'SELECT COUNT(*) FROM public."{table}" WHERE updated_at > :since'
            )
            total = c.execute(count_sql, {"since": last_sync}).scalar()
        else:
            total = c.execute(
                text(f'SELECT COUNT(*) FROM public."{table}"')
            ).scalar()

    if total == 0:
        log.info("  %-35s  no new rows (last_sync=%s)", table,
                 last_sync.isoformat() if last_sync else "never")
        return

    # Count rows on prod (for logging delta)
    with prod_engine.connect() as c:
        prod_before = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()

    if incremental:
        log.info("  %-35s  dev=%d new since %s  prod=%d  (upsert %d at a time)",
                 table, total, last_sync.strftime("%Y-%m-%d %H:%M:%S"),
                 prod_before, BATCH_SIZE)
    else:
        log.info("  %-35s  dev=%d  prod=%d  (full sync, %d at a time)",
                 table, total, prod_before, BATCH_SIZE)

    # Build the SELECT query with optional filter
    if incremental:
        select_sql = text(
            f'SELECT {col_sql} FROM public."{table}"\n'
            f'  WHERE updated_at > :since\n'
            f'  ORDER BY "{pk_col}"\n'
            f'  LIMIT :limit OFFSET :offset'
        )
    else:
        select_sql = text(
            f'SELECT {col_sql} FROM public."{table}"\n'
            f'  ORDER BY "{pk_col}"\n'
            f'  LIMIT :limit OFFSET :offset'
        )

    offset = 0
    chunk_count = 0
    while offset < total:
        try:
            # Read chunk from dev
            with dev_engine.connect() as c:
                params = {"limit": BATCH_SIZE, "offset": offset}
                if incremental:
                    params["since"] = last_sync
                chunk = c.execute(select_sql, params).mappings().fetchall()

            if not chunk:
                break

            # Clean secondary unique conflicts in prod for this chunk
            if secondary_uniques:
                _cleanup_secondary_conflicts(
                    prod_engine, table, chunk, secondary_uniques, pk_cols
                )

            # Bulk INSERT
            col_names = list(chunk[0].keys())
            values_clause_parts = []
            params = {}
            for i, row in enumerate(chunk):
                placeholders = [f":r{i}_{c}" for c in col_names]
                values_clause_parts.append(f"({', '.join(placeholders)})")
                for c in col_names:
                    params[f"r{i}_{c}"] = row[c]

            values_clause = ",\n  ".join(values_clause_parts)

            with prod_engine.begin() as c:
                c.execute(
                    text(
                        f'INSERT INTO public."{table}" ({col_sql})\n'
                        f'  VALUES\n  {values_clause}\n'
                        f'  {conflict_clause}'
                    ),
                    params,
                )

            offset += BATCH_SIZE
            chunk_count += 1
            if chunk_count % 5 == 0:
                log.info("    chunk %3d: %6d / %d", chunk_count, min(offset, total), total)
            time.sleep(BATCH_SLEEP_S)

        except Exception as e:
            log.error("    FAILED at offset %d: %s", offset, e)
            skipped = 0
            for row in chunk:
                try:
                    with prod_engine.begin() as c:
                        c.execute(
                            text(
                                f'INSERT INTO public."{table}" ({col_sql})\n'
                                f'  VALUES ({", ".join(f":{k}" for k in row.keys())})\n'
                                f"  {conflict_clause}"
                            ),
                            dict(row),
                        )
                except Exception as e2:
                    row_id = row.get("id", "?")
                    log.warning("    Skipped row id=%s: %s", row_id, e2)
                    skipped += 1
            if skipped:
                log.warning("    Row-by-row: %d row(s) skipped", skipped)
            offset += BATCH_SIZE
            chunk_count += 1
            time.sleep(BATCH_SLEEP_S * 2)

    # Reset sequence
    with prod_engine.connect() as c:
        seq_name = f"{table}_id_seq"
        try:
            max_id = c.execute(
                text(f'SELECT COALESCE(MAX(id), 0) FROM public."{table}"')
            ).scalar()
            if max_id and max_id > 0:
                c.execute(text(f"SELECT setval('{seq_name}', :max_id)"), {"max_id": max_id})
        except Exception:
            pass

        prod_after = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()
        log.info("    done: prod now %d rows (%+d)", prod_after, prod_after - prod_before)

    # Record sync checkpoint
    _set_last_sync(prod_engine, table)


# ── Validation ──


def _validate(dev_engine, prod_engine):
    """Post-sync sanity checks — compare row counts between dev and prod."""
    log.info("── Post-sync validation ──")
    ok = True

    for table in ALL_SYNC_TABLES:
        try:
            with dev_engine.connect() as c:
                dev_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()
            with prod_engine.connect() as c:
                prod_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()
            status = "✅" if dev_cnt == prod_cnt else "⚠"
            if dev_cnt != prod_cnt:
                ok = False
            log.info("  %s %-35s  dev=%7d  prod=%7d", status, table, dev_cnt, prod_cnt)
        except Exception as e:
            log.warning("  ⚠ %-35s  error: %s", table, e)

    if ok:
        log.info("  ✅ All row counts match dev")
    else:
        log.warning("  ⚠ Some row counts differ from dev — investigate")
    return ok


# ── Main ──


def _sync_status(dev_engine, prod_engine):
    """Print a status report showing sync lag per table."""
    print(f"\n{'=' * 72}")
    print(f"  Dev → Prod Sync Status")
    print(f"  DEV:  {_mask_url(str(dev_engine.url))}")
    print(f"  PROD: {_mask_url(str(prod_engine.url))}")
    print(f"{'=' * 72}")

    header = (
        f"  {'Table':<32s} {'Dev':>7} {'Prod':>7} {'Delta':>7}"
        f"  {'Pending':>7}  {'Last Synced'}"
    )
    print(header)
    print(f"  {'-' * len(header)}")

    total_pending = 0
    total_dev = 0
    total_prod = 0

    for table in ALL_SYNC_TABLES:
        if table in EXCLUDED_TABLES:
            continue

        try:
            with dev_engine.connect() as c:
                dev_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()

            with prod_engine.connect() as c:
                prod_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM public."{table}"')
                ).scalar()

            # Rows changed since last sync
            last_sync = _get_last_sync(prod_engine, table)
            if last_sync and _table_has_updated_at(dev_engine, table):
                with dev_engine.connect() as c:
                    pending = c.execute(
                        text(
                            f'SELECT COUNT(*) FROM public."{table}"'
                            f' WHERE updated_at > :since'
                        ),
                        {"since": last_sync},
                    ).scalar()
            else:
                pending = dev_cnt  # full sync needed

            total_pending += pending
            total_dev += dev_cnt
            total_prod += prod_cnt

            delta = dev_cnt - prod_cnt
            last_sync_str = (
                last_sync.strftime("%Y-%m-%d %H:%M") if last_sync else "(never)"
            )

            flag = " ⚠" if pending > 1000 else ""
            print(
                f"  {table:<32s} {dev_cnt:>7} {prod_cnt:>7} {delta:+>7}"
                f"  {pending:>7}{flag}  {last_sync_str}"
            )

        except Exception as e:
            brief = e.args[0] if e.args else str(e)
            print(f"  {table:<32s}  {'ERROR':>7}  {brief[:80]}")

    print(f"  {'─' * len(header)}")
    print(
        f"  {'TOTAL':<32s} {total_dev:>7} {total_prod:>7}"
        f"  {total_pending:>7}  {'':12s}"
    )
    print(f"{'=' * 72}\n")

    if total_pending == 0:
        print("  ✅ Dev and prod are in sync.")
    else:
        tail_count = min(total_pending, 99999)
        print(f"  📦 {total_pending} row(s) pending sync.")
        print(f"     BATCH_SIZE={BATCH_SIZE}  BATCH_SLEEP_MS={int(BATCH_SLEEP_S * 1000)}")
        print(f"     Estimated chunks: {(total_pending + BATCH_SIZE - 1) // BATCH_SIZE}")
        print(f"     Estimated time:  ~{(total_pending // BATCH_SIZE) * BATCH_SLEEP_S + 1}s")
    print()


def main():
    dev_url = _resolve_dev_url()
    prod_url = _resolve_prod_url()

    dev_engine = create_engine(dev_url, pool_size=2, connect_args={"connect_timeout": 10})
    prod_engine = create_engine(prod_url, pool_size=2, connect_args={"connect_timeout": 10})

    is_full_sync = SYNC_MODE == "full"

    # ── 1. Acquire advisory lock on prod ──
    log.info("Acquiring sync lock on prod...")
    with prod_engine.connect() as c:
        acquired = c.execute(
            text(f"SELECT pg_try_advisory_lock({LOCK_ID})")
        ).scalar()
        if not acquired:
            log.error("Another sync is already running (lock held)")
            sys.exit(1)

    try:
        # ── 2. Bootstrap prod schema (idempotent) ──
        log.info("── Bootstrap ──")
        _ensure_sync_meta_table(prod_engine)
        _ensure_updated_at_on_prod(prod_engine)

        # ── 3. Upsert each table ──
        run_type = "full" if is_full_sync else "incremental"
        log.info("── Syncing tables (%s) ──", run_type)

        for table in ALL_SYNC_TABLES:
            if table in EXCLUDED_TABLES:
                continue

            cols = _column_intersection(dev_engine, prod_engine, table)
            auto_exclude = AUTO_COLUMNS.get(table, set())
            if auto_exclude:
                cols = [c for c in cols if c not in auto_exclude]
            if not cols:
                log.warning("  Skipping %s — no common columns between dev and prod", table)
                continue

            # Determine if this table should do a full sync
            do_full = is_full_sync or table in FULL_SYNC_TABLES

            t0 = time.time()
            _upsert_table(dev_engine, prod_engine, table, cols, is_full_sync=do_full)
            elapsed = time.time() - t0
            log.info("    ─ took %.1fs\n", elapsed)

        # ── 4. Validate ──
        _validate(dev_engine, prod_engine)

    except BaseException as e:
        log.error("Sync failed: %s", e)
        raise
    finally:
        with prod_engine.connect() as c:
            c.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
        log.info("Lock released")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dev → prod incremental database sync"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print sync lag report and exit (no data transfer)",
    )
    args = parser.parse_args()

    if args.status:
        dev_url = os.environ.get("DATABASE_URL", "")
        prod_url = os.environ.get("PROD_DATABASE_URL", "")
        dev_engine = create_engine(
            dev_url, pool_size=2, connect_args={"connect_timeout": 10}
        )
        prod_engine = create_engine(
            prod_url, pool_size=2, connect_args={"connect_timeout": 10}
        )
        _sync_status(dev_engine, prod_engine)
        dev_engine.dispose()
        prod_engine.dispose()
        sys.exit(0)

    t0 = time.time()
    log.info("Starting dev → prod sync (mode=%s, batch=%d, sleep=%.1fs)",
             SYNC_MODE, BATCH_SIZE, BATCH_SLEEP_S)
    main()
    elapsed = time.time() - t0
    log.info("Sync finished in %.1f seconds", elapsed)
