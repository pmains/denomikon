#!/usr/bin/env python3
r"""
Extract case numbers from agenda_items and generate a SQL file for local execution.

The scan reads from the dev DB (over Tailscale) -- this is fast, it's just SELECTs.
The writes are emitted as a .sql file that gets SCP'd to the Windows server
and run via local psql (avoiding Tailscale latency on 20K+ writes).

Usage:
    # 1. Generate the SQL file
    python3 -u scripts/backfill_cases_to_sql.py

    # 2. SCP to Windows
    scp /tmp/backfill_cases.sql windows-tailscale:"C:\Users\Peter\Desktop\backfill_cases.sql"

    # 3. Run on Windows (via ssh or RDP)
    #    psql -h localhost -U postgres -d poliscopic_dev -f C:\Users\Peter\Desktop\backfill_cases.sql
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timezone
from sqlalchemy import text
from db.core import get_engine
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from case_utils import extract_all_case_numbers

OUTPUT_PATH = "/tmp/backfill_cases.sql"
BATCH = 500  # rows per VALUES clause in generated SQL

engine = get_engine()
now = datetime.now(timezone.utc)

print(f"Reading meeting body map...", flush=True)
with engine.connect() as c:
    body_map = dict(c.execute(text("SELECT id, body FROM meetings")).fetchall())

print(f"Scanning agenda_items for missing case numbers...", flush=True)
with engine.connect() as c:
    rows = c.execute(text("""
        SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text,
               ai.meeting_db_id, ai.body
        FROM agenda_items ai
        WHERE ai.case_number IS NULL OR ai.case_number = ''
        ORDER BY ai.id
    """)).fetchall()

print(f"Scanning {len(rows)} items...", flush=True)

# ── Phase 1: Extract case numbers ──
updates: dict[int, str] = {}  # item_id -> case_number
for idx, (item_id, title, txt, mdb_id, body_code) in enumerate(rows):
    meeting_body = body_map.get(mdb_id, "")
    text_to_scan = f"{title or ''} {txt or ''}"
    cases = extract_all_case_numbers(text_to_scan, body=meeting_body)
    if cases:
        updates[item_id] = cases[0]
    if idx > 0 and idx % 20000 == 0:
        print(f"  scanned {idx}, {len(updates)} found...", flush=True)

print(f"Found {len(updates)} items with case numbers", flush=True)

# Deduplicate unique cases
unique_cases: dict[str, str] = {}
for item_id, case in updates.items():
    if case not in unique_cases:
        prefix = case.split("-")[0] if "-" in case else case[:2]
        unique_cases[case] = prefix

print(f"Unique cases: {len(unique_cases)}", flush=True)
print(f"Writing SQL to {OUTPUT_PATH}...", flush=True)

# ── Phase 2: Generate SQL file ──
now_iso = now.strftime("%Y-%m-%d %H:%M:%S%z")

with open(OUTPUT_PATH, "w") as f:
    f.write(f"-- Backfill case numbers — generated {now_iso}\n")
    f.write(f"-- {len(updates)} agenda_items, {len(unique_cases)} unique cases\n")
    f.write("BEGIN;\n\n")

    # ── Step 1: UPDATE agenda_items ──
    f.write("-- Step 1: Update agenda_items with case_number\n")
    f.write("UPDATE agenda_items SET case_number = v.case_number\n")
    f.write("FROM (VALUES\n")

    items_list = list(updates.items())
    for i in range(0, len(items_list), BATCH):
        chunk = items_list[i : i + BATCH]
        for j, (item_id, case) in enumerate(chunk):
            comma = "," if not (i + j == len(items_list) - 1) else ""
            escaped_case = case.replace("'", "''")
            f.write(f"  ({item_id}, '{escaped_case}'){comma}\n")

    f.write(") AS v(id, case_number)\n")
    f.write("WHERE agenda_items.id = v.id\n")
    f.write("  AND (agenda_items.case_number IS NULL OR agenda_items.case_number = '');\n\n")

    # ── Step 2: INSERT unique cases ──
    f.write("-- Step 2: Insert unique cases\n")

    case_list = list(unique_cases.items())
    for i in range(0, len(case_list), BATCH):
        chunk = case_list[i : i + BATCH]
        values_lines = []
        for case, ctype in chunk:
            escaped_case = case.replace("'", "''")
            escaped_ctype = ctype.replace("'", "''")
            values_lines.append(f"  ('{escaped_case}', '{escaped_ctype}', '{now_iso}', '{now_iso}')")

        if i == 0:
            f.write("INSERT INTO cases (case_number, case_type, created_at, updated_at)\n")
            f.write("VALUES\n")
            f.write(",\n".join(values_lines))
            f.write("\nON CONFLICT (case_number) DO UPDATE SET updated_at = EXCLUDED.updated_at;\n")
        else:
            # Subsequent batches need separate statements
            f.write("INSERT INTO cases (case_number, case_type, created_at, updated_at)\n")
            f.write("VALUES\n")
            f.write(",\n".join(values_lines))
            f.write("\nON CONFLICT (case_number) DO UPDATE SET updated_at = EXCLUDED.updated_at;\n")

    f.write("\n")

    # ── Step 3: INSERT case_events ──
    f.write("-- Step 3: Insert case_events\n")

    # Build a temp table approach to join item_id -> case_number -> case.id
    f.write("CREATE TEMP TABLE _backfill_events (agenda_item_id INT, case_number TEXT) ON COMMIT DROP;\n\n")

    events_list = list(updates.items())
    f.write("INSERT INTO _backfill_events (agenda_item_id, case_number) VALUES\n")
    for i in range(0, len(events_list), BATCH):
        chunk = events_list[i : i + BATCH]
        for j, (item_id, case) in enumerate(chunk):
            comma = "," if not (i + j == len(events_list) - 1) else ""
            escaped_case = case.replace("'", "''")
            f.write(f"  ({item_id}, '{escaped_case}'){comma}\n")
    f.write(";\n\n")

    f.write("INSERT INTO case_events (case_id, agenda_item_id, source, event_type, event_date, created_at, updated_at)\n")
    f.write("SELECT c.id, e.agenda_item_id, 'backfill', 'agenda_item',\n")
    f.write(f"       '{now_iso[:10]}'::date, '{now_iso}', '{now_iso}'\n")
    f.write("FROM _backfill_events e\n")
    f.write("JOIN cases c ON c.case_number = e.case_number\n")
    f.write("ON CONFLICT DO NOTHING;\n\n")

    f.write("DROP TABLE IF EXISTS _backfill_events;\n\n")

    f.write("COMMIT;\n")

print(f"SQL file written: {OUTPUT_PATH}")
print(f"Next steps:")
print(f"  1. scp {OUTPUT_PATH} windows-tailscale:\"C:\\\\Users\\\\Peter\\\\Desktop\\\\backfill_cases.sql\"")
print(f"  2. ssh windows-tailscale psql -h localhost -U postgres -d poliscopic_dev -f \"C:\\Users\\Peter\\Desktop\\backfill_cases.sql\"")
print("Done!", flush=True)
