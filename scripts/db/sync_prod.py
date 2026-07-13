#!/usr/bin/env python3
"""
Dev → prod sync for DO Managed PostgreSQL via postgres_fdw.

Connects to the DO `poliscopic` database, reads from the `dev` foreign schema
(which points to `poliscopic_dev`), and upserts rows into `public` tables in
chunks. All data transfer happens within DO's internal network — no round-trips
through the Mac.

This is designed for minimal CPU impact on the DO cluster, and runs safely
alongside the live application (no TRUNCATE, no ACCESS EXCLUSIVE locks).

Usage:
    export DATABASE_URL="postgresql://doadmin:...@host:25060/poliscopic?sslmode=verify-full"
    python scripts/db/sync_prod.py

Or:
    export DATABASE_URL="..."  # points to DO poliscopic_dev
    # Script auto-derives prod URL from dev URL

Environment:
    DATABASE_URL         DO prod database (or dev — auto-derived)
    PROD_DATABASE_URL    Explicit prod URL (overrides auto-detection)
    BATCH_SIZE           Rows per chunk (default: 2000)
    BATCH_SLEEP_MS       Sleep between chunks, milliseconds (default: 100)
    FDW_SCHEMA           Foreign schema name on prod (default: dev)
"""

from __future__ import annotations

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
FDW_SCHEMA = os.environ.get("FDW_SCHEMA", "dev")

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
    "meeting_supervisors",
    "meetings",
    "member_votes",
    "persons",
    "public_bodies",
    "pz_item_details",
    "supporting_documents",
]

EXCLUDED_TABLES = {
    "admin_users", "admin_notifications", "article_sources", "article_tags",
    "articles", "dismissed_suggestions", "media_images", "public_body_members",
    "scanned_agenda_text", "skeet_drafts", "tags", "topic_weekly_reports",
    "topics",
}


def _resolve_url() -> str:
    """Resolve the production database URL."""
    url = os.environ.get("PROD_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        log.error("Set PROD_DATABASE_URL or DATABASE_URL")
        sys.exit(1)
    # Ensure the dbname is 'poliscopic' (prod target)
    url2 = re.sub(r'/poliscopic_dev([?#]|$)', r'/poliscopic\1', url)
    if url2 == url and 'poliscopic_dev' in url:
        url2 = url.replace('poliscopic_dev', 'poliscopic')
    if 'poliscopic' not in url2:
        log.error("DATABASE_URL must contain 'poliscopic' (prod target)")
        sys.exit(1)
    log.info("Target: %s", re.sub(r'(//[^:]+:).+(@)', r'\1****\2', url2))
    return url2


def _ensure_fdw(engine):
    """Ensure the FDW foreign schema exists. Creates it if missing."""
    with engine.begin() as c:
        # Check if extension exists
        has_fdw = c.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'postgres_fdw'")
        ).scalar()
        if not has_fdw:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS postgres_fdw"))

        # Check if server exists
        has_srv = c.execute(
            text("SELECT count(*) FROM pg_foreign_server WHERE srvname = 'dev_fdw'")
        ).scalar()
        if not has_srv:
            host = os.environ.get("DO_HOST")
            port = os.environ.get("DO_PORT", "25060")
            pwd = os.environ.get("DO_PASSWORD")
            if not host or not pwd:
                raise ValueError("DO_HOST and DO_PASSWORD environment variables are required")
            c.execute(text(
                f"CREATE SERVER dev_fdw FOREIGN DATA WRAPPER postgres_fdw "
                f"OPTIONS (dbname 'poliscopic_dev', host '{host}', port '{port}')"
            ))
            c.execute(text(
                f"CREATE USER MAPPING FOR doadmin "
                f"SERVER dev_fdw OPTIONS (user 'doadmin', password '{pwd}')"
            ))

        # Re-import the schema
        c.execute(text(f"DROP SCHEMA IF EXISTS {FDW_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {FDW_SCHEMA}"))
        c.execute(text(
            f"IMPORT FOREIGN SCHEMA public "
            f"FROM SERVER dev_fdw INTO {FDW_SCHEMA}"
        ))

        # Verify we have tables
        cnt = c.execute(
            text(f"SELECT count(*) FROM information_schema.tables "
                 f"WHERE table_schema = '{FDW_SCHEMA}'")
        ).scalar()
        log.info("FDW ready: %d tables in schema '%s'", cnt, FDW_SCHEMA)
        if cnt == 0:
            log.error("No tables imported — FDW setup failed")
            sys.exit(1)


def _pk_cols(engine, table: str) -> list[str]:
    """Return the primary key columns for a table."""
    inspector = sa_inspect(engine)
    pk = inspector.get_pk_constraint(table)
    if pk and pk.get("constrained_columns"):
        return list(pk["constrained_columns"])
    return ["id"]


def _quoted_cols(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


def _column_intersection(engine, table: str) -> list[str]:
    """Return columns present in BOTH public and dev schemas."""
    dev_cols = {
        c["name"] for c in sa_inspect(engine).get_columns(table, schema=FDW_SCHEMA)
        if c["name"] != "rowid"
    }
    prod_cols = {
        c["name"] for c in sa_inspect(engine).get_columns(table, schema="public")
        if c["name"] != "rowid"
    }
    common = sorted(dev_cols & prod_cols)
    return common


def _detect_secondary_uniques(engine, table: str) -> list[list[str]]:
    """Return unique constraint column lists beyond the PK."""
    inspector = sa_inspect(engine)
    pk_cols = set(_pk_cols(engine, table))
    uniques = []
    for ix in inspector.get_indexes(table):
        cols = list(ix["column_names"])
        if ix.get("unique") and set(cols) != pk_cols:
            uniques.append(cols)
    # Also check table constraints via pg_constraint
    import re
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


def _cleanup_secondary_uniques(engine, table: str, secondary_uniques: list[list[str]]):
    """Delete prod rows whose secondary unique keys conflict with dev rows.

    This prevents UniqueViolation errors when the upsert by PK hits a row
    where different IDs share the same unique key value.
    """
    if not secondary_uniques:
        return
    with engine.begin() as c:
        for uq_cols in secondary_uniques:
            uq_sql = ", ".join(f'"{c}"' for c in uq_cols)
            join_clause = " AND ".join(
                f'dev."{c}" = prod."{c}"' for c in uq_cols
            )
            pk_cols = _pk_cols(engine, table)
            pk_noteq = " OR ".join(
                f'dev."{c}" <> prod."{c}"' for c in pk_cols
            )
            deleted = c.execute(text(
                f'DELETE FROM public."{table}" prod\n'
                f' WHERE EXISTS (\n'
                f'   SELECT 1 FROM "{FDW_SCHEMA}"."{table}" dev\n'
                f'   WHERE {join_clause}\n'
                f'     AND ({pk_noteq})\n'
                f' )'
            )).rowcount
            if deleted:
                log.info("    ─ cleaned up %d prod rows with conflicting %s", deleted, uq_cols)


def _upsert_table(engine, table: str, cols: list[str]):
    """Upsert all rows from dev → prod for one table, chunked.

    All data transfer happens within DO's internal network via FDW.
    Each chunk is committed independently with a sleep between for pacing.

    Handles secondary UNIQUE constraints by pre-deleting conflicting rows.
    """
    pk_cols = _pk_cols(engine, table)
    pk_col = pk_cols[0] if pk_cols else "id"
    pk_sql = _quoted_cols(pk_cols)
    col_sql = _quoted_cols(cols)

    # Detect secondary unique constraints that might cause conflicts
    secondary_uniques = _detect_secondary_uniques(engine, table)

    update_set = ", ".join(
        f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in pk_cols
    )
    conflict_clause = f"ON CONFLICT ({pk_sql}) DO UPDATE SET {update_set}" if update_set else "ON CONFLICT DO NOTHING"

    # Count total on dev
    with engine.connect() as c:
        total = c.execute(
            text(f'SELECT COUNT(*) FROM "{FDW_SCHEMA}"."{table}"')
        ).scalar()
        prod_before = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()

    if total == 0:
        log.info("  %-35s  dev=%d  prod=%d  (empty, skipping)", table, total, prod_before)
        return

    if secondary_uniques:
        _cleanup_secondary_uniques(engine, table, secondary_uniques)

    log.info("  %-35s  dev=%d  prod=%d  (upsert %d at a time)",
             table, total, prod_before, BATCH_SIZE)

    offset = 0
    chunk_count = 0
    while offset < total:
        try:
            with engine.begin() as c:
                c.execute(
                    text(
                        f'INSERT INTO public."{table}" ({col_sql})\n'
                        f'  SELECT {col_sql} FROM "{FDW_SCHEMA}"."{table}"\n'
                        f'  ORDER BY "{pk_col}"\n'
                        f'  LIMIT :limit OFFSET :offset\n'
                        f'  {conflict_clause}'
                    ),
                    {"limit": BATCH_SIZE, "offset": offset},
                )
            offset += BATCH_SIZE
            chunk_count += 1
            if chunk_count % 5 == 0:
                log.info("    chunk %3d: %6d / %d", chunk_count, min(offset, total), total)
            time.sleep(BATCH_SLEEP_S)
        except Exception as e:
            log.error("    FAILED at offset %d: %s", offset, e)
            # Move past problematic row(s) — better to skip than infinite loop
            offset += BATCH_SIZE
            chunk_count += 1
            if "connection" in str(e).lower():
                log.warning("    Re-creating FDW schema after connection error...")
                _ensure_fdw(engine)
                time.sleep(BATCH_SLEEP_S * 2)

    # Reset sequence
    with engine.connect() as c:
        seq_name = f"{table}_id_seq"
        try:
            max_id = c.execute(
                text(f'SELECT COALESCE(MAX(id), 0) FROM public."{table}"')
            ).scalar()
            if max_id and max_id > 0:
                c.execute(text(f"SELECT setval('{seq_name}', :max_id)"), {"max_id": max_id})
        except Exception:
            pass

        prod_after = c.execute(text(f'SELECT COUNT(*) FROM public."{table}"')).scalar()
        log.info("    done: prod now %d rows (%+d)", prod_after, prod_after - prod_before)


def _validate(engine):
    """Post-sync sanity checks."""
    log.info("── Post-sync validation ──")
    ok = True

    for table in ALL_SYNC_TABLES:
        try:
            with engine.connect() as c:
                dev_cnt = c.execute(
                    text(f'SELECT COUNT(*) FROM "{FDW_SCHEMA}"."{table}"')
                ).scalar()
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


def main():
    prod_url = _resolve_url()
    engine = create_engine(prod_url, pool_size=2)

    # ── 1. Ensure FDW schema ──
    _ensure_fdw(engine)

    # ── 2. Acquire advisory lock ──
    log.info("Acquiring sync lock...")
    with engine.connect() as c:
        acquired = c.execute(text(f"SELECT pg_try_advisory_lock({LOCK_ID})")).scalar()
        if not acquired:
            log.error("Another sync is already running (lock held)")
            sys.exit(1)

    try:
        # ── 3. Upsert each table ──
        log.info("── Syncing tables (FDW, chunked upsert) ──")
        for table in ALL_SYNC_TABLES:
            if table in EXCLUDED_TABLES:
                continue
            cols = _column_intersection(engine, table)
            if not cols:
                log.warning("  Skipping %s — no common columns", table)
                continue
            t0 = time.time()
            _upsert_table(engine, table, cols)
            elapsed = time.time() - t0
            log.info("    ─ took %.1fs\n", elapsed)

        # ── 4. Validate ──
        _validate(engine)

    except BaseException as e:
        log.error("Sync failed: %s", e)
        raise
    finally:
        # ── 5. Release advisory lock ──
        with engine.connect() as c:
            c.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
        log.info("Lock released")


if __name__ == "__main__":
    t0 = time.time()
    log.info("Starting dev → prod sync (batch=%d, sleep=%.1fs)", BATCH_SIZE, BATCH_SLEEP_S)
    main()
    elapsed = time.time() - t0
    log.info("Sync finished in %.1f seconds", elapsed)
