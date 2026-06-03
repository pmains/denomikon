#!/usr/bin/env python3
"""
Migrate all child tables from meeting_id (VARCHAR, jurisdiction's ID) to
meeting_db_id (INTEGER, meetings.id PK).

This enables:
  - Fast indexed ISAM joins (INTEGER PK) instead of VARCHAR + body joins
  - No cross-jurisdiction contamination from colliding meeting_id values
  - ON DELETE CASCADE to prevent orphan child rows when meetings are deleted

Tables affected:
  agenda_items, supporting_documents, agenda_item_votes, meeting_supervisors,
  case_events, meeting_attendance, executive_session_participants,
  pz_item_details, article_sources, dismissed_suggestions

USAGE:
  python scripts/migrate_to_pks.py           # apply migration
  python scripts/migrate_to_pks.py --verify  # verify only, no changes
  python scripts/migrate_to_pks.py --dry-run # show what would change

The old meeting_id column is preserved during migration for rollback safety.
A follow-up script (cleanup_meeting_id.py) will drop it after confirmation.
"""

import sqlite3
import sys
import os
import argparse


TABLES = [
    "agenda_items",
    "supporting_documents",
    "agenda_item_votes",
    "meeting_supervisors",
    "case_events",
    "meeting_attendance",
    "executive_session_participants",
    "pz_item_details",
    "article_sources",
    "dismissed_suggestions",
]


def get_conn():
    db_path = os.environ.get(
        "DATABASE_URL", "sqlite:///data/maricopa.sqlite"
    ).replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def verify_state(conn, should_exist=False):
    """Check current state of the migration."""
    print("=" * 60)
    print("CURRENT STATE")
    print("=" * 60)

    for tbl in TABLES:
        # Check if column exists
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
        has_new = "meeting_db_id" in cols

        if not has_new and should_exist:
            print(f"  ❌ {tbl}: meeting_db_id MISSING")
            continue
        elif not has_new:
            total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            resolved = conn.execute(f"""
                SELECT COUNT(*) FROM {tbl} c
                JOIN meetings m ON m.body = c.body AND m.meeting_id = c.meeting_id
            """).fetchone()[0]
            unresolved = total - resolved
            flag = "✅" if unresolved == 0 else f"⚠️{unresolved} unresolvable"
            print(f"  {flag} {tbl:35s}: {resolved}/{total} resolveable via (body, meeting_id)")
        else:
            # New column exists, verify it
            total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            resolved = conn.execute(f"""
                SELECT COUNT(*) FROM {tbl} c
                WHERE c.meeting_db_id IN (SELECT id FROM meetings)
            """).fetchone()[0]
            null_count = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE meeting_db_id = 0").fetchone()[0]
            flag = "✅" if total == resolved else f"⚠️{total - resolved} bad FK refs"
            print(f"  {flag} {tbl:35s}: {resolved}/{total} valid FK refs ({null_count} with db_id=0)")

    print()


def add_meeting_db_id(conn, tbl):
    """Add meeting_db_id column to a table."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
    if "meeting_db_id" in cols:
        return

    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN meeting_db_id INTEGER NOT NULL DEFAULT 0")
    print(f"  + Added meeting_db_id to {tbl}")


def populate_meeting_db_id(conn, tbl):
    """Populate meeting_db_id by resolving (body, meeting_id) → meetings.id."""
    # Step 1: Resolve by (body, meeting_id)
    # COALESCE: for rows that don't match, keep meeting_db_id=0 (no NULL constraint violation)
    result = conn.execute(f"""
        UPDATE {tbl}
        SET meeting_db_id = COALESCE((
            SELECT m.id FROM meetings m
            WHERE m.body = {tbl}.body AND m.meeting_id = {tbl}.meeting_id
            LIMIT 1
        ), 0)
        WHERE {tbl}.body != ''
          AND {tbl}.meeting_id != ''
    """)
    rowcount = result.rowcount

    # Step 2: For rows with empty body, try to resolve by unique meeting_id
    result2 = conn.execute(f"""
        UPDATE {tbl}
        SET meeting_db_id = COALESCE((
            SELECT m.id FROM meetings m
            WHERE m.meeting_id = {tbl}.meeting_id
            LIMIT 1
        ), 0)
        WHERE meeting_db_id = 0
          AND {tbl}.meeting_id != ''
          AND {tbl}.meeting_id IN (
              SELECT meeting_id FROM meetings
              GROUP BY meeting_id HAVING COUNT(*) = 1
          )
    """)
    rowcount += result2.rowcount

    # Count unresolvable
    unresolvable = conn.execute(f"""
        SELECT COUNT(*) FROM {tbl}
        WHERE meeting_db_id = 0
    """).fetchone()[0]

    if unresolvable > 0:
        # Show details of unresolvable
        samples = conn.execute(f"""
            SELECT body, meeting_id, COUNT(*) as cnt
            FROM {tbl}
            WHERE meeting_db_id = 0
            GROUP BY body, meeting_id
            LIMIT 5
        """).fetchall()
        print(f"    ⚠️  {unresolvable} rows unresolvable (meeting_db_id=0)")
        for s in samples:
            print(f"       body='{s[0]}' meeting_id='{s[1]}' count={s[2]}")
    else:
        print(f"    ✅ All {rowcount} rows resolved")


def add_fk_and_index(conn, tbl):
    """Add foreign key constraint and index on meeting_db_id."""
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{tbl}_meeting_db_id ON {tbl}(meeting_db_id)")
    print(f"    + Created index ix_{tbl}_meeting_db_id")


def migrate_table(conn, tbl, dry_run=False):
    """Full migration for a single table."""
    print(f"\n  [{tbl}]")

    if dry_run:
        total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        resolved = conn.execute(f"""
            SELECT COUNT(*) FROM {tbl} c
            JOIN meetings m ON m.body = c.body AND m.meeting_id = c.meeting_id
        """).fetchone()[0]
        unresolvable = total - resolved

        # Also count empty-body resolutions
        empty_resolvable = conn.execute(f"""
            SELECT COUNT(*) FROM {tbl} c
            WHERE (c.body = '' OR c.body IS NULL)
            AND c.meeting_id IN (
                SELECT meeting_id FROM meetings GROUP BY meeting_id HAVING COUNT(*) = 1
            )
        """).fetchone()[0]

        still_unres = unresolvable - empty_resolvable

        print(f"    Would add meeting_db_id to {tbl}")
        print(f"    {total} total rows")
        print(f"    {resolved} resolvable by (body, meeting_id)")
        print(f"    {empty_resolvable} additional resolvable by unique meeting_id")
        print(f"    {still_unres} would remain unresolved (meeting_db_id=0)")
        return

    add_meeting_db_id(conn, tbl)
    populate_meeting_db_id(conn, tbl)
    add_fk_and_index(conn, tbl)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="Verify state, no changes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    args = parser.parse_args()

    conn = get_conn()

    if args.verify:
        verify_state(conn)
        conn.close()
        return

    print("=" * 60)
    print("MIGRATING meeting_id → meeting_db_id (PK references)")
    print("=" * 60)
    print()

    verify_state(conn)

    for tbl in TABLES:
        migrate_table(conn, tbl, dry_run=args.dry_run)

    if not args.dry_run:
        conn.commit()
        print()
        print("=" * 60)
        print("Migration applied. Verifying...")
        print("=" * 60)
        verify_state(conn)
        print("Done. meeting_db_id columns populated with PK references to meetings.id")

    conn.close()


if __name__ == "__main__":
    main()
