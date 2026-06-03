#!/usr/bin/env python3
"""
Fix orphan body codes — normalize aliases, resolve duplicates, create missing public_bodies.

This handles:
1. Body code aliases (surprise-planning-zoning → surprise-pz, etc.)
2. Duplicate meetings from multiple scrapers using different body codes for same body
3. Missing public_body records (Phoenix subcommittees, Paradise Valley, Queen Creek)
4. Wrong jurisdiction_id / public_body_id on meetings

USAGE:
  python scripts/fix_body_codes.py          # apply fixes
  python scripts/fix_body_codes.py --verify # check only, no changes
"""

import sqlite3
import sys
import os


TARGET_BODIES = {
    # Body code alias → correct code
    'surprise-planning-zoning': 'surprise-pz',
    'surprise-city-council': 'surprise-cc',
    'avondale-quorum': 'avondale-cc',
    'buckeye-water-rate': 'buckeye-cc',
    'gilbert-red': 'gilbert-tc',
    'gilbert-pf': 'gilbert-tc',
    'gilbert-water': 'gilbert-tc',
}

NEW_PUBLIC_BODIES = [
    # code, name, slug, type, jurisdiction_slug
    ('phoenix-ti', 'Phoenix Transportation, Infrastructure & Planning Subcommittee', 'phoenix-ti', 'subcommittee', 'phoenix'),
    ('phoenix-ps', 'Phoenix Public Safety & Justice Subcommittee', 'phoenix-ps', 'subcommittee', 'phoenix'),
    ('phoenix-ed', 'Phoenix Economic Development & the Arts Subcommittee', 'phoenix-ed', 'subcommittee', 'phoenix'),
    ('phoenix-cs', 'Phoenix Community Services & Education Subcommittee', 'phoenix-cs', 'subcommittee', 'phoenix'),
    ('phoenix-bh', 'Phoenix Budget Hearing', 'phoenix-bh', 'hearing', 'phoenix'),
    ('paradise-valley-boa', 'Paradise Valley Board of Adjustment', 'paradise-valley-boa', 'board_of_adjustment', 'paradise-valley'),
    ('paradise-valley-pc', 'Paradise Valley Planning Commission', 'paradise-valley-pc', 'planning_commission', 'paradise-valley'),
    ('queen-creek-cc', 'Queen Creek Town Council', 'queen-creek-cc', 'city_council', 'queen-creek'),
]


def get_conn():
    db_path = os.environ.get("DATABASE_URL", "sqlite:///data/maricopa.sqlite").replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def verify(conn):
    """Show current state without changes."""
    print("=== CURRENT STATE ===\n")

    # Orphan body codes
    orphan = conn.execute("""
        SELECT body, COUNT(*) FROM meetings m
        LEFT JOIN public_bodies pb ON pb.body_code = m.body
        WHERE pb.id IS NULL AND m.body != ''
        GROUP BY m.body ORDER BY COUNT(*) DESC
    """).fetchall()
    if orphan:
        total = sum(r[1] for r in orphan)
        print(f"❌ {total} meetings with orphan body codes ({len(orphan)} body codes):")
        for r in orphan:
            print(f"   body={r[0]:35s} count={r[1]:>4}")
    else:
        print("✅ No orphan body codes")

    # Wrong jurisdiction
    bad_jur = conn.execute("""
        SELECT COUNT(*) FROM meetings m
        JOIN public_bodies pb ON pb.body_code = m.body
        WHERE m.jurisdiction_id != pb.jurisdiction_id OR m.public_body_id != pb.id
    """).fetchone()[0]
    if bad_jur:
        print(f"\n❌ {bad_jur} meetings with wrong jurisdiction_id or public_body_id")
    else:
        print(f"\n✅ All meeting jurisdiction/public_body IDs correct")

    # Duplicate meeting_id pairs
    dups = 0
    for old, new in TARGET_BODIES.items():
        cnt = conn.execute("""
            SELECT COUNT(*) FROM meetings m1
            JOIN meetings m2 ON m1.meeting_id = m2.meeting_id
            WHERE m1.body = ? AND m2.body = ? AND m1.id != m2.id
        """, (old, new)).fetchone()[0]
        if cnt:
            dups += cnt
    if dups:
        print(f"\n❌ {dups} duplicate meeting pairs across body code aliases")
    else:
        print(f"\n✅ No duplicate meeting pairs")

    # Missing public bodies
    for code, name, slug, btype, jur_slug in NEW_PUBLIC_BODIES:
        exists = conn.execute("SELECT id FROM public_bodies WHERE body_code = ?", (code,)).fetchone()
        if not exists:
            print(f"❌ Missing public_body: {code}")


def apply(conn):
    """Apply all fixes."""
    # 1. Create missing public bodies
    jur_map = {row[1]: row[0] for row in conn.execute("SELECT id, slug FROM jurisdictions")}
    created = 0
    for code, name, slug, btype, jur_slug in NEW_PUBLIC_BODIES:
        conn.execute("""INSERT OR IGNORE INTO public_bodies
            (body_code, name, slug, body_type, jurisdiction_id, description, website_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', '', datetime('now'), datetime('now'))""",
            (code, name, slug, btype, jur_map.get(jur_slug, 1)))
        if conn.execute("SELECT changes()").fetchone()[0]:
            created += 1
    print(f"Created {created} new public_bodies")

    # 2. Handle duplicate meetings (same meeting_id, different body codes)
    deleted_old = 0
    deleted_new = 0
    kept_and_renamed = 0

    for old_body, new_body in TARGET_BODIES.items():
        dupes = conn.execute("""
            SELECT m1.id as old_id, m1.meeting_id,
                   (SELECT COUNT(*) FROM agenda_items WHERE meeting_db_id = m1.id) as old_items,
                   (SELECT COUNT(*) FROM agenda_items WHERE meeting_db_id = m2.id) as new_items,
                   m2.id as new_id
            FROM meetings m1
            JOIN meetings m2 ON m1.meeting_id = m2.meeting_id
            WHERE m1.body = ? AND m2.body = ? AND m1.id != m2.id
        """, (old_body, new_body)).fetchall()

        for old_id, mid, old_items, new_items, new_id in dupes:
            if old_items > 0 and new_items > 0:
                # Both have data — keep the one with more items
                if old_items >= new_items:
                    conn.execute("DELETE FROM meetings WHERE id = ?", (new_id,))
                    deleted_new += 1
                else:
                    conn.execute("DELETE FROM meetings WHERE id = ?", (old_id,))
                    deleted_old += 1

            elif old_items > 0:
                # Only old has data — keep old, delete new
                conn.execute("DELETE FROM meetings WHERE id = ?", (new_id,))
                deleted_new += 1

            elif new_items > 0:
                # Only new has data — keep new, delete old
                conn.execute("DELETE FROM meetings WHERE id = ?", (old_id,))
                deleted_old += 1

            else:
                # Neither has items — delete old
                conn.execute("DELETE FROM meetings WHERE id = ?", (old_id,))
                deleted_old += 1

    conn.commit()
    print(f"Resolved duplicates: deleted {deleted_old} old + {deleted_new} new duplicates")

    # 3. Update remaining meetings to use correct body code
    updated = 0
    for old_body, new_body in TARGET_BODIES.items():
        cnt = conn.execute("SELECT COUNT(*) FROM meetings WHERE body = ?", (old_body,)).fetchone()[0]
        if cnt > 0:
            # Check for conflicts before updating
            conflicts = conn.execute("""
                SELECT m1.id, m1.meeting_id FROM meetings m1
                JOIN meetings m2 ON m1.meeting_id = m2.meeting_id
                WHERE m1.body = ? AND m2.body = ? AND m1.id != m2.id
            """, (old_body, new_body)).fetchall()

            if conflicts:
                # Still have conflicts — delete old (they have no items at this point)
                for cid, cmid in conflicts:
                    conn.execute("DELETE FROM meetings WHERE id = ?", (cid,))
                    deleted_old += 1

            # Now safe to update
            conn.execute("UPDATE meetings SET body = ? WHERE body = ?", (new_body, old_body))
            conn.execute("UPDATE agenda_items SET body = ?, source_body = ? WHERE body = ?",
                        (new_body, new_body, old_body))
            updated += cnt

    conn.commit()
    print(f"Updated body code on {updated} remaining meetings + their agenda_items")

    # 4. Backfill jurisdiction_id and public_body_id on all meetings
    conn.execute("""
        UPDATE meetings
        SET jurisdiction_id = COALESCE(
            (SELECT pb.jurisdiction_id FROM public_bodies pb WHERE pb.body_code = meetings.body),
            jurisdiction_id
        ),
        public_body_id = COALESCE(
            (SELECT pb.id FROM public_bodies pb WHERE pb.body_code = meetings.body),
            public_body_id
        )
    """)
    conn.commit()
    print("Backfilled jurisdiction_id and public_body_id")

    # 5. Verify
    print()
    verify(conn)


def main():
    verify_only = "--verify" in sys.argv

    conn = get_conn()

    if verify_only:
        verify(conn)
    else:
        print("=== APPLYING BODY CODE FIXES ===\n")
        apply(conn)

    conn.close()


if __name__ == "__main__":
    main()
