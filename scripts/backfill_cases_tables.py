r"""
Generate SQL to populate cases + case_events tables from existing agenda_items.case_number.

The case_numbers are already in agenda_items (20K rows filled by a prior run),
but the `cases` and `case_events` tables are empty. This generates a .sql file
to be run locally on Windows via psql (avoids Tailscale latency on bulk writes).

Usage:
    cd scripts && python3 -u backfill_cases_tables.py
    scp /tmp/backfill_cases_tables.sql windows-tailscale:"C:\Users\Peter\OneDrive\Desktop\backfill_cases_tables.sql"
    # On Windows:
    #   "C:\pgsql\pgsql\bin\psql.exe" -h localhost -U postgres -d poliscopic_dev
    #     -f C:\Users\Peter\OneDrive\Desktop\backfill_cases_tables.sql
"""
from __future__ import annotations

import re
import sys
import os
from datetime import datetime, timezone
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db.core import get_engine

OUTPUT_PATH = "/tmp/backfill_cases_tables.sql"
BATCH = 500

engine = get_engine()
now = datetime.now(timezone.utc)
now_iso = now.strftime("%Y-%m-%d %H:%M:%S%z")

print("Reading agenda_items with case numbers...", flush=True)
with engine.connect() as c:
    rows = c.execute(text("""
        SELECT ai.id, ai.case_number, ai.body, ai.meeting_id, ai.meeting_db_id
        FROM agenda_items ai
        WHERE ai.case_number IS NOT NULL AND ai.case_number != ''
        ORDER BY ai.id
    """)).fetchall()

print(f"Found {len(rows)} items with case numbers", flush=True)

# Deduplicate: case_number -> (prefix, normalized)
unique_cases: dict[str, tuple[str, str]] = {}
for item_id, case, body, meeting_id, meeting_db_id in rows:
    if case not in unique_cases:
        prefix = case.split("-")[0] if "-" in case else case[:2]
        normalized = re.sub(r"[^A-Z0-9]", " ", case.upper()).strip()
        unique_cases[case] = (prefix, normalized)
print(f"Unique cases: {len(unique_cases)}", flush=True)

print(f"Writing SQL to {OUTPUT_PATH}...", flush=True)

with open(OUTPUT_PATH, "w") as f:
    f.write(f"-- Populate cases + case_events from existing agenda_items.case_number\n")
    f.write(f"-- Generated {now_iso}\n")
    f.write(f"-- {len(unique_cases)} unique cases, {len(rows)} event mappings\n")
    f.write("BEGIN;\n\n")

    # ── Step 1: INSERT into cases ──
    f.write("-- Step 1: Insert unique cases\n")

    case_list = list(unique_cases.items())
    for i in range(0, len(case_list), BATCH):
        chunk = case_list[i : i + BATCH]
        values_lines = []
        for case, (ctype, normalized) in chunk:
            escaped_case = case.replace("'", "''")
            escaped_ctype = ctype.replace("'", "''")
            escaped_norm = normalized.replace("'", "''")
            values_lines.append(
                f"  ('{escaped_case}', '{escaped_ctype}', '{escaped_norm}', "
                f"'{now_iso}', '{now_iso}')"
            )

        f.write("INSERT INTO cases "
                "(case_number, case_type, normalized_case_number, created_at, updated_at)\n"
                "VALUES\n")
        f.write(",\n".join(values_lines))
        f.write("\nON CONFLICT (case_number) "
                "DO UPDATE SET updated_at = EXCLUDED.updated_at;\n\n")

    # ── Step 2: INSERT into case_events ──
    f.write("-- Step 2: Insert case_events (via temp table join to get case IDs)\n")
    f.write("CREATE TEMP TABLE _backfill_events (\n")
    f.write("  agenda_item_id INT, case_number TEXT,\n")
    f.write("  body TEXT, meeting_id TEXT, meeting_db_id INT\n")
    f.write(") ON COMMIT DROP;\n\n")

    f.write("INSERT INTO _backfill_events "
            "(agenda_item_id, case_number, body, meeting_id, meeting_db_id) VALUES\n")
    item_data = [(item_id, case, body, mid, mdb_id)
                 for item_id, case, body, mid, mdb_id in rows]
    for i in range(0, len(item_data), BATCH):
        chunk = item_data[i : i + BATCH]
        for j, (item_id, case, body, mid, mdb_id) in enumerate(chunk):
            global_idx = i + j
            comma = "," if global_idx < len(item_data) - 1 else ""
            escaped_case = case.replace("'", "''")
            escaped_body = (body.replace("'", "''") if body else "")[:16]
            escaped_mid = (mid.replace("'", "''") if mid else "")[:32]
            f.write(f"  ({item_id}, '{escaped_case}', '{escaped_body}', "
                    f"'{escaped_mid}', {mdb_id}){comma}\n")
    f.write(";\n\n")

    f.write("INSERT INTO case_events "
            "(body, case_id, meeting_id, meeting_db_id, agenda_item_id, "
            "source, event_type, event_date, created_at, updated_at)\n")
    f.write("SELECT e.body, c.id, e.meeting_id, e.meeting_db_id, e.agenda_item_id,\n")
    f.write("       'backfill', 'agenda_item',\n")
    f.write(f"       '{now_iso[:10]}'::date, '{now_iso}', '{now_iso}'\n")
    f.write("FROM _backfill_events e\n")
    f.write("JOIN cases c ON c.case_number = e.case_number\n")
    f.write("WHERE NOT EXISTS (\n")
    f.write("  SELECT 1 FROM case_events ce\n")
    f.write("  WHERE ce.agenda_item_id = e.agenda_item_id\n")
    f.write("    AND ce.case_id = c.id\n")
    f.write(");\n\n")

    f.write("DROP TABLE IF EXISTS _backfill_events;\n\n")
    f.write("COMMIT;\n")

print(f"SQL file written: {OUTPUT_PATH}")
print(f"File size: {os.path.getsize(OUTPUT_PATH)} bytes")
print()
print("Next steps:")
print(f"  1. scp {OUTPUT_PATH} windows-tailscale:\"C:\\Users\\Peter\\OneDrive\\Desktop\\backfill_cases_tables.sql\"")
print('  2. ssh windows-tailscale "C:\\pgsql\\pgsql\\bin\\psql.exe" -h localhost -U postgres -d poliscopic_dev -f "C:\\Users\\Peter\\OneDrive\\Desktop\\backfill_cases_tables.sql"')
print("Done!", flush=True)
