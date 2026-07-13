#!/usr/bin/env python3
"""
Dev → prod sync via direct SQLAlchemy connection (no FDW).

Connects to the dev database (Windows via Tailscale) and the prod database
(DO Managed PostgreSQL), reads data from dev in chunks, and upserts into
prod using INSERT … ON CONFLICT DO UPDATE.

All data transfer goes through the Mac — dev queries over Tailscale, then
prod upserts over SSL.  Chunked and paced to keep DO's 1GB RAM cluster happy.

Usage:
    source .env && python3 scripts/db/sync_prod.py

Environment:
    DATABASE_URL         Dev database  (Windows via Tailscale)
    PROD_DATABASE_URL    Prod database (DO Managed PostgreSQL)
    BATCH_SIZE           Rows per chunk (default: 2000)
    BATCH_SLEEP_MS       Sleep between chunks, milliseconds (default: 100)
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


# ── URL resolution ──


def _resolve_dev_url() -> str:
    """Return the dev database URL (DATABASE_URL from .env)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("Set DATABASE_URL to your dev database (Windows via Tailscale)")
        sys.exit(1)
    log.info("Dev:   %s", re.sub(r'(//[^:]+:).+(@)', r'\1****\2', url))
    return url


def _resolve_prod_url() -> str:
    """Return the prod database URL (PROD_DATABASE_URL from .env)."""
    url = os.environ.get("PROD_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        log.error("Set PROD_DATABASE_URL or DATABASE_URL")
        sys.exit(1)
    # Derive prod URL from dev URL (swap db name)
    url2 = re.sub(r'/poliscopic_dev([?#]|$)', r'/poliscopic\1', url)
    if url2 == url and 'poliscopic_dev' in url:
        url2 = url.replace('poliscopic_dev', 'poliscopic')
    if 'poliscopic' not in url2:
        log.error("Cannot derive prod URL — set PROD_DATABASE_URL explicitly")
        sys.exit(1)
    log.info("Prod:  %s", re.sub(r'(//[^:]+:).+(@)', r'\1****\2', url2))
    return url2


# ── Schema inspection helpers ──


def _pk_cols(engine, table: str) -> list[str]:
    """Return the primary key columns for a table."""
    inspector = sa_inspect(engine)
    pk = inspector.get_pk_constraint(table)
    if pk and pk.get("constrained_columns"):
        return list(pk["constrained_columns"])
    return ["id"]


def _quoted_cols(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


def _column_intersection(dev_engine, prod_engine, table: str) -> list[str]:
    """Return columns present in BOTH dev and prod."""
    dev_cols = {
        c["name"] for c in sa_inspect(dev_engine).get_columns(table)
        if c["name"] != "rowid"
    }
    prod_cols = {
        c["name"] for c in sa_inspect(prod_engine).get_columns(table)
        if c["name"] != "rowid"
    }
    common = sorted(dev_cols & prod_cols)
    return common


# ── Secondary unique constraint handling ──


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
    """Delete prod rows whose secondary unique keys conflict with chunk rows.

    This prevents UniqueViolation errors when the upsert by PK hits a row
    where different IDs share the same unique key value.

    Uses batch IN() queries per unique constraint for efficiency.
    """
    if not secondary_uniques or not chunk_rows:
        return
    with prod_engine.begin() as c:
        for uq_cols in secondary_uniques:
            # Build a list of (unique_col_values, pk_col_values) tuples
            # so we can do a single bulk DELETE per unique constraint.
            if len(uq_cols) == 1:
                uq_col = uq_cols[0]
                pk_col = pk_cols[0]
                values = [row[uq_col] for row in chunk_rows if row.get(uq_col) is not None]
                pks = [row[pk_col] for row in chunk_rows if row.get(uq_col) is not None]
                if not values:
                    continue
                # Build positionally-parameterized IN clauses
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
                # Composite unique — do row-by-row (usually rare)
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





# ── Table sync ──


def _upsert_table(dev_engine, prod_engine, table: str, cols: list[str]):
    """Upsert all rows from dev → prod for one table, chunked.

    Reads from dev in chunks, upserts into prod in bulk. Handles secondary
    UNIQUE constraints by pre-deleting conflicting rows per chunk.
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

    # Count rows on dev
    with dev_engine.connect() as c:
        total = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()

    if total == 0:
        log.info("  %-35s  dev=0  (empty, skipping)", table)
        return

    # Count rows on prod
    with prod_engine.connect() as c:
        prod_before = c.execute(
            text(f'SELECT COUNT(*) FROM public."{table}"')
        ).scalar()

    log.info("  %-35s  dev=%d  prod=%d  (upsert %d at a time)",
             table, total, prod_before, BATCH_SIZE)

    offset = 0
    chunk_count = 0
    while offset < total:
        try:
            # Read chunk from dev
            with dev_engine.connect() as c:
                chunk = c.execute(
                    text(
                        f'SELECT {col_sql} FROM public."{table}"\n'
                        f'  ORDER BY "{pk_col}"\n'
                        f'  LIMIT :limit OFFSET :offset'
                    ),
                    {"limit": BATCH_SIZE, "offset": offset},
                ).mappings().fetchall()

            if not chunk:
                break

            # Clean secondary unique conflicts in prod for this chunk
            if secondary_uniques:
                _cleanup_secondary_conflicts(
                    prod_engine, table, chunk, secondary_uniques, pk_cols
                )

            # Build a single bulk INSERT with multiple VALUES rows
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
            # Fall back to row-by-row for this chunk on error
            try:
                with prod_engine.begin() as c:
                    for row in chunk:
                        c.execute(
                            text(
                                f'INSERT INTO public."{table}" ({col_sql})\n'
                                f'  VALUES ({", ".join(f":{k}" for k in row.keys())})\n'
                                f'  {conflict_clause}'
                            ),
                            dict(row),
                        )
            except Exception as e2:
                log.error("    Row-by-row also failed: %s", e2)
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


def main():
    dev_url = _resolve_dev_url()
    prod_url = _resolve_prod_url()

    dev_engine = create_engine(dev_url, pool_size=2)
    prod_engine = create_engine(prod_url, pool_size=2)

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
        # ── 2. Upsert each table ──
        log.info("── Syncing tables (direct, chunked upsert) ──")
        for table in ALL_SYNC_TABLES:
            if table in EXCLUDED_TABLES:
                continue
            cols = _column_intersection(dev_engine, prod_engine, table)
            if not cols:
                log.warning("  Skipping %s — no common columns between dev and prod", table)
                continue
            t0 = time.time()
            _upsert_table(dev_engine, prod_engine, table, cols)
            elapsed = time.time() - t0
            log.info("    ─ took %.1fs\n", elapsed)

        # ── 3. Validate ──
        _validate(dev_engine, prod_engine)

    except BaseException as e:
        log.error("Sync failed: %s", e)
        raise
    finally:
        # ── 4. Release advisory lock on prod ──
        with prod_engine.connect() as c:
            c.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
        log.info("Lock released")


if __name__ == "__main__":
    t0 = time.time()
    log.info("Starting dev → prod sync (batch=%d, sleep=%.1fs)", BATCH_SIZE, BATCH_SLEEP_S)
    main()
    elapsed = time.time() - t0
    log.info("Sync finished in %.1f seconds", elapsed)
