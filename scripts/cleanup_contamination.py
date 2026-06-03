#!/usr/bin/env python3
"""
One-time data cleanup for data contamination issues discovered 2026-05-30.

Issues fixed:
1. mesa-city-council → mesa-cc: 1,910 items stored with stale body name
2. Delete orphan items with no matching (body, meeting_id) that can't be reassigned

This should be run AFTER the query joins are fixed (queries.py, backfill_dom_votes.py,
housing_hearings.py) and BEFORE the DB constraint is added.
"""

import sqlite3
import sys
import os

# Add parent of scripts/ to Python path (for prod env detection)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_db() -> sqlite3.Connection:
    db_path = os.environ.get(
        "DATABASE_URL",
        "sqlite:///data/maricopa.sqlite",
    ).replace("sqlite:///", "")
    return sqlite3.connect(db_path)


def reassign_mesa_city_council(conn: sqlite3.Connection) -> int:
    """Reassign mesa-city-council items → mesa-cc where mesa-cc meetings exist."""
    cur = conn.execute(
        """UPDATE agenda_items
           SET body = 'mesa-cc',
               source_body = 'mesa-cc',
               _body_backfilled = 0
           WHERE body = 'mesa-city-council'
             AND meeting_id IN (SELECT meeting_id FROM meetings WHERE body = 'mesa-cc')"""
    )
    conn.commit()
    return cur.rowcount


def delete_orphan_items(conn: sqlite3.Connection) -> int:
    """Delete items that have no matching (body, meeting_id) in meetings.

    These are items from scraper bugs that stored the wrong body/meeting_id combo.
    They can't be reassigned because we don't know which body they actually belong to.
    """
    cur = conn.execute(
        """DELETE FROM agenda_items
           WHERE (body, meeting_id) NOT IN (
               SELECT body, meeting_id FROM meetings
           )"""
    )
    conn.commit()
    return cur.rowcount


def delete_orphan_supporting_docs(conn: sqlite3.Connection) -> int:
    """Clean up supporting_documents that reference orphan items."""
    cur = conn.execute(
        """DELETE FROM supporting_documents
           WHERE (body, meeting_id) NOT IN (
               SELECT body, meeting_id FROM meetings
           )"""
    )
    conn.commit()
    return cur.rowcount


def delete_orphan_votes(conn: sqlite3.Connection) -> int:
    """Clean up agenda_item_votes that reference orphan meetings."""
    cur = conn.execute(
        """DELETE FROM agenda_item_votes
           WHERE (body, meeting_id) NOT IN (
               SELECT body, meeting_id FROM meetings
           )"""
    )
    conn.commit()
    return cur.rowcount


def count_mesa_city_council(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM agenda_items WHERE body = 'mesa-city-council'"
    ).fetchone()[0]


def count_orphans(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM agenda_items
           WHERE (body, meeting_id) NOT IN (
               SELECT body, meeting_id FROM meetings
           )"""
    ).fetchone()[0]


def count_orphan_docs(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM supporting_documents
           WHERE (body, meeting_id) NOT IN (
               SELECT body, meeting_id FROM meetings
           )"""
    ).fetchone()[0]


def count_orphan_votes(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM agenda_item_votes
           WHERE (body, meeting_id) NOT IN (
               SELECT body, meeting_id FROM meetings
           )"""
    ).fetchone()[0]


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — no changes will be made")

    conn = get_db()

    before_mcc = count_mesa_city_council(conn)
    before_orphans = count_orphans(conn)
    before_docs = count_orphan_docs(conn)
    before_votes = count_orphan_votes(conn)

    print(f"Before cleanup:")
    print(f"  mesa-city-council items: {before_mcc}")
    print(f"  orphan agenda_items:     {before_orphans}")
    print(f"  orphan supporting_docs:  {before_docs}")
    print(f"  orphan votes:            {before_votes}")
    print()

    if dry_run:
        print("Dry run complete. Pass without --dry-run to apply.")
        conn.close()
        return 0

    # 1. Reassign mesa-city-council → mesa-cc
    mcc_reassigned = reassign_mesa_city_council(conn)
    print(f"Reassigned mesa-city-council → mesa-cc: {mcc_reassigned} items")

    # 2. Delete orphan agenda_items
    orphan_deleted = delete_orphan_items(conn)
    print(f"Deleted orphan agenda_items: {orphan_deleted}")

    # 3. Delete orphan supporting_documents
    doc_deleted = delete_orphan_supporting_docs(conn)
    print(f"Deleted orphan supporting_documents: {doc_deleted}")

    # 4. Delete orphan votes
    vote_deleted = delete_orphan_votes(conn)
    print(f"Deleted orphan agenda_item_votes: {vote_deleted}")

    print()

    after_mcc = count_mesa_city_council(conn)
    after_orphans = count_orphans(conn)
    after_docs = count_orphan_docs(conn)
    after_votes = count_orphan_votes(conn)

    print(f"After cleanup:")
    print(f"  mesa-city-council items: {after_mcc}")
    print(f"  orphan agenda_items:     {after_orphans}")
    print(f"  orphan supporting_docs:  {after_docs}")
    print(f"  orphan votes:            {after_votes}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
