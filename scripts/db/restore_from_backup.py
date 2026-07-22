#!/usr/bin/env python3
"""
Restore production data from a pg_dump image into the dev database.

Usage:
  python restore_from_backup.py --dump=C:\path\to\prod-backup-20260721-2354-full.dump

Requirements: psycopg2-binary, PostgreSQL 18 pg_restore on PATH
"""

import argparse, logging, os, shlex, subprocess, sys, tempfile
from pathlib import Path

log = logging.getLogger("restore")

# Tables restored directly (same table, same columns — data-only pg_restore | psql)
DIRECT = [
    "meetings", "agenda_items", "agenda_item_votes",
    "member_votes", "supporting_documents",
    "persons", "cases", "case_events", "body_memberships",
    "pz_item_details", "public_bodies", "public_body_members", "body_seats",
    "jurisdictions", "articles", "article_sources", "article_tags", "tags",
    "topics", "topic_weekly_reports",
    "entities", "entity_mentions", "entity_relationships",
    "executive_session_participants", "meeting_attendance",
]

# Tables in prod that don't exist in dev — skip them
SKIP = ["supervisor_votes"]


def run(cmd, **kw):
    log.info("$ %s", " ".join(shlex.quote(str(c)) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def count_rows(dsn, table):
    import psycopg2
    with psycopg2.connect(dsn) as c:
        return c.cursor().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_exists(dsn, table):
    import psycopg2
    with psycopg2.connect(dsn) as c:
        c.cursor().execute(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s)", (table,))
        return c.cursor().fetchone()[0]


def restore_table(cmd_prefix, dsn, dump, prod_tbl, dev_tbl=None):
    """Pipe COPY data from pg_restore --data-only --table=X directly into psql."""
    dev_tbl = dev_tbl or prod_tbl
    if not table_exists(dsn, dev_tbl):
        log.warning("  SKIP %s → %s (target table missing)", prod_tbl, dev_tbl)
        return
    sql = tempfile.NamedTemporaryFile(suffix=".sql", delete=False, mode="w").name
    r = run(cmd_prefix + ["--data-only", "--table", prod_tbl, "--file", sql, dump])
    if r.returncode != 0:
        log.warning("  pg_restore exit %d for %s: %s", r.returncode, prod_tbl, r.stderr[:150])
        try:
            Path(sql).unlink()
        except OSError:
            pass
        return
    content = Path(sql).read_text().strip()
    if not content:
        try:
            Path(sql).unlink()
        except OSError:
            pass
        log.info("  %-30s (empty)", prod_tbl)
        return
    if prod_tbl != dev_tbl:
        # Rewrite COPY target table name
        content = content.replace(
            f"COPY public.{prod_tbl}",
            f"COPY public.{dev_tbl}"
        )
        Path(sql).write_text(content)
    r = run(["psql", dsn, "-f", sql, "-q"])
    try:
        Path(sql).unlink()
    except OSError:
        pass
    if r.returncode != 0:
        log.warning("  psql exit %d for %s: %s", r.returncode, prod_tbl, r.stderr[:200])
        cnt = count_rows(dsn, dev_tbl)
        log.info("  %-30s ~%s rows (post-load)", f"{prod_tbl}", cnt)
    else:
        cnt = count_rows(dsn, dev_tbl)
        log.info("  %-30s %s rows", f"{prod_tbl} → {dev_tbl}", cnt)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--dev-dsn",
        default=f"host=localhost dbname=poliscopic_dev user=poliscopic password={os.environ.get('DEV_PASS', 'CHANGE_ME')}")
    ap.add_argument("--pg-bin", default="", help="e.g. C:\\Program Files\\PostgreSQL\\18\\bin\\")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dump = Path(args.dump)
    if not dump.exists():
        log.error("Dump not found: %s", dump)
        sys.exit(1)

    # Verify dev connection
    import psycopg2
    try:
        psycopg2.connect(args.dev_dsn).close()
    except Exception as e:
        log.error("Cannot connect to dev: %s", e)
        sys.exit(1)

    bin_dir = args.pg_bin
    if bin_dir and not bin_dir.endswith(("\\", "/")):
        bin_dir += "\\"
    pg_restore = [bin_dir + "pg_restore"] if bin_dir else ["pg_restore"]

    dsn = args.dev_dsn
    log.info("Source: %s", dump)
    log.info("Target: %s", dsn.split("password=")[0] + "password=****")
    log.info("pg_restore: %s", pg_restore[0])

    if args.dry_run:
        log.info("Dry run — no changes made")
        return

    # Phase 1: Truncate tables that need full replacement
    log.info("\n── Truncating tables ──")
    truncate_list = [
        "meetings", "agenda_items", "agenda_item_votes",
        "member_votes", "supporting_documents", "meeting_members",
    ]
    with psycopg2.connect(dsn) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        for t in truncate_list:
            if table_exists(dsn, t):
                cur.execute(f'TRUNCATE TABLE public."{t}" CASCADE')
                log.info("  Truncated %s", t)

    # Phase 2: Direct 1:1 tables
    log.info("\n── Direct tables ──")
    for t in DIRECT:
        restore_table(pg_restore, dsn, str(dump), t)
        log.info("")

    # Phase 3: Mapped tables (meeting_supervisors → meeting_members)
    log.info("── Mapped tables ──")

    # Extract meeting_supervisors data into a temp table, then INSERT with column rename
    tmp_sql = tempfile.NamedTemporaryFile(suffix=".sql", delete=False, mode="w").name
    r = run(pg_restore + ["--data-only", "--table", "meeting_supervisors", "--file", tmp_sql, str(dump)])
    if r.returncode == 0 and Path(tmp_sql).stat().st_size > 0:
        # Create temp table matching old schema
        with psycopg2.connect(dsn) as conn:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                CREATE TEMP TABLE _ms (
                    id SERIAL PRIMARY KEY,
                    body VARCHAR(16) NOT NULL DEFAULT '',
                    meeting_id VARCHAR(32) NOT NULL,
                    meeting_db_id INTEGER NOT NULL DEFAULT 0,
                    supervisor_id INTEGER NOT NULL,
                    role VARCHAR(64),
                    present BOOLEAN,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                ) ON COMMIT DROP
            """)
            # Fix table name in COPY to match temp table
            sql = Path(tmp_sql).read_text()
            sql = sql.replace("COPY public.meeting_supervisors", "COPY pg_temp._ms")
            sql = sql.replace("\\.", "")  # strip end-of-data marker for INSERT path
            Path(tmp_sql).write_text(sql)

            # Load data into temp table
            r2 = run(["psql", dsn, "-f", tmp_sql, "-q"])
            if r2.returncode == 0:
                # Now INSERT into meeting_members with column rename
                cur.execute("""
                    INSERT INTO public.meeting_members
                        (body, meeting_id, meeting_db_id, member_id, role, present, created_at, updated_at)
                    SELECT body, meeting_id, meeting_db_id, supervisor_id, role, present, created_at, updated_at
                    FROM pg_temp._ms
                    ON CONFLICT (body, meeting_id, member_id) DO NOTHING
                """)
                log.info("  meeting_supervisors → meeting_members: %s rows", cur.rowcount)
            else:
                log.warning("  meeting_supervisors load failed: %s", r2.stderr[:200])
    else:
        log.warning("  meeting_supervisors extraction failed or empty")

    try:
        Path(tmp_sql).unlink()
    except OSError:
        pass

    # Phase 4: Update sequences
    log.info("\n── Sequences ──")
    with psycopg2.connect(dsn) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT 'SELECT SETVAL(' ||
                quote_literal(quote_ident(s.schemaname) || '.' || quote_ident(s.relname)) ||
                ', COALESCE(MAX(' || quote_ident(c.attname) || '), 1)) FROM ' ||
                quote_ident(s.schemaname) || '.' || quote_ident(t.relname) || ';'
            FROM pg_class s, pg_depend d, pg_class t, pg_attribute c, pg_tables pt
            WHERE s.relkind = 'S'
              AND s.oid = d.objid
              AND d.refobjid = t.oid
              AND d.refobjid = c.attrelid
              AND d.refobjsubid = c.attnum
              AND t.relname = pt.tablename
              AND pt.schemaname = 'public'
        """)
        for row in cur.fetchall():
            try:
                cur.execute(row[0])
            except Exception:
                pass
    log.info("  Sequences updated")

    log.info("\n✅ Restore complete")


if __name__ == "__main__":
    main()
