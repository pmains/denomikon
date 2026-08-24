#!/usr/bin/env python3
"""Quick data inventory for analytics planning."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from db import get_engine
from sqlalchemy import text

engine = get_engine()

with engine.connect() as conn:
    # 1. Agenda item text volume
    r = conn.execute(text("""
        SELECT
            COUNT(*) as total_items,
            COUNT(agenda_item_text) as with_text,
            SUM(CASE WHEN LENGTH(agenda_item_text) > 100 THEN 1 ELSE 0 END) as meaningful_text,
            SUM(CASE WHEN LENGTH(agenda_item_text) > 1000 THEN 1 ELSE 0 END) as long_text,
            SUM(LENGTH(agenda_item_text)) as total_chars
        FROM agenda_items
    """)).fetchone()
    print("=== AGENDA ITEM TEXT ===")
    print(f"  Total items:            {r[0]:>8,}")
    print(f"  With text (non-null):   {r[1]:>8,}")
    print(f"  >100 chars (meaningful):{r[2]:>8,}")
    print(f"  >1k chars (long):       {r[3]:>8,}")
    print(f"  Total text volume:      {r[4]:>12,} chars  ({r[4]/1e6:.1f}M)")

    # 2. Bodies with meaningful text
    rows = conn.execute(text("""
        SELECT
            body,
            COUNT(*) as items,
            SUM(CASE WHEN LENGTH(agenda_item_text) > 100 THEN 1 ELSE 0 END) as meaningful,
            CAST(SUM(CASE WHEN LENGTH(agenda_item_text) > 100 THEN 1 ELSE 0 END) AS FLOAT) / NULLIF(COUNT(*), 0) * 100 as pct
        FROM agenda_items
        WHERE agenda_item_text != ''
        GROUP BY body
        HAVING COUNT(*) > 50
        ORDER BY meaningful DESC
        LIMIT 20
    """)).fetchall()
    print("\n=== BODIES WITH MOST MEANINGFUL TEXT ===")
    for r in rows:
        print(f"  {r[0]:30s} {r[1]:>6,} items  {r[2]:>6,} meaningful  {r[3]:>5.1f}%")

    # 3. Split votes by body
    rows = conn.execute(text("""
        SELECT body, COUNT(*) as splits
        FROM agenda_item_votes
        WHERE is_split_vote = TRUE
        GROUP BY body
        ORDER BY splits DESC
        LIMIT 15
    """)).fetchall()
    print("\n=== SPLIT VOTES BY BODY ===")
    for r in rows:
        print(f"  {r[0]:30s} {r[1]:>6,}")

    # 4. Lifecycle signals: held, continued, pulled items
    for signal, label in [("held", "HELD"), ("continued", "CONTINUED"), ("pulled", "PULLED"), ("tabl", "TABLED")]:
        rows = conn.execute(text(f"""
            SELECT body, COUNT(*) as cnt
            FROM agenda_items
            WHERE LOWER(agenda_item_text) LIKE '%{signal}%'
            GROUP BY body
            HAVING COUNT(*) > 5
            ORDER BY cnt DESC
            LIMIT 8
        """)).fetchall()
        if rows:
            print(f"\n=== '{label}' MENTIONS IN AGENDA ITEM TEXT ===")
            for r in rows:
                print(f"  {r[0]:30s} {r[1]:>6,}")

    # 5. Supporting document text
    r = conn.execute(text("""
        SELECT
            COUNT(*) as total_docs,
            SUM(CASE WHEN LENGTH(text_content) > 100 THEN 1 ELSE 0 END) as with_text,
            SUM(CASE WHEN LENGTH(text_content) > 1000 THEN 1 ELSE 0 END) as long_text,
            SUM(LENGTH(text_content)) as total_chars
        FROM supporting_documents
    """)).fetchone()
    print(f"\n=== SUPPORTING DOCUMENTS TEXT ===")
    print(f"  Total docs:            {r[0]:>8,}")
    print(f"  With text:             {r[1]:>8,}")
    print(f"  >1k chars:             {r[2]:>8,}")
    print(f"  Total text volume:     {r[3]:>12,} chars  ({r[3]/1e6:.1f}M)")

    # 6. Documents by body
    rows = conn.execute(text("""
        SELECT body, COUNT(*) as docs,
               SUM(CASE WHEN LENGTH(text_content) > 100 THEN 1 ELSE 0 END) as with_text
        FROM supporting_documents
        GROUP BY body
        HAVING COUNT(*) > 10
        ORDER BY docs DESC
        LIMIT 15
    """)).fetchall()
    print(f"\n=== SUPPORTING DOCUMENTS BY BODY ===")
    for r in rows:
        print(f"  {r[0]:30s} {r[1]:>6,} docs  {r[2]:>6,} with text")

    # 7. Meetings with zero items extracted
    r = conn.execute(text("""
        SELECT body, COUNT(*) as zero_item_meetings
        FROM meetings m
        WHERE m.item_count_actual = 0
            AND m.sync_status = 'complete'
        GROUP BY body
        HAVING COUNT(*) > 10
        ORDER BY zero_item_meetings DESC
        LIMIT 15
    """)).fetchall()
    print(f"\n=== MEETINGS WITH 0 ITEMS EXTRACTED (complete) ===")
    for r in rows:
        print(f"  {r[0]:30s} {r[1]:>6,}")
