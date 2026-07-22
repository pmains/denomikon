#!/usr/bin/env python3
"""Backfill case numbers — batched."""
from datetime import datetime, timezone
from sqlalchemy import text
from db.core import get_engine
from case_utils import extract_all_case_numbers

engine = get_engine()
now = datetime.now(timezone.utc)

with engine.connect() as c:
    body_map = dict(c.execute(text("SELECT id, body FROM meetings")).fetchall())
    rows = c.execute(text("""
        SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text,
               ai.meeting_db_id, ai.body
        FROM agenda_items ai
        WHERE ai.case_number IS NULL OR ai.case_number = ''
        ORDER BY ai.id
    """)).fetchall()

print(f"Scanning {len(rows)} items...", flush=True)

updates = {}
for idx, (item_id, title, txt, mdb_id, body_code) in enumerate(rows):
    meeting_body = body_map.get(mdb_id, "")
    text_to_scan = f"{title or ''} {txt or ''}"
    cases = extract_all_case_numbers(text_to_scan, body=meeting_body)
    if cases:
        updates[item_id] = cases[0]
    if idx % 20000 == 0 and idx:
        print(f"  scanned {idx}, {len(updates)} found...", flush=True)

print(f"Found {len(updates)} items with case numbers", flush=True)

# Batch update agenda_items
BATCH = 500
batch, updated_total = [], 0
for item_id, case in updates.items():
    batch.append({"case": case, "item_id": item_id})
    if len(batch) >= BATCH:
        with engine.begin() as conn:
            r = conn.execute(
                text("UPDATE agenda_items SET case_number = :case WHERE id = :item_id AND (case_number IS NULL OR case_number = '')"),
                batch,
            )
            updated_total += r.rowcount
        batch = []
if batch:
    with engine.begin() as conn:
        r = conn.execute(
            text("UPDATE agenda_items SET case_number = :case WHERE id = :item_id AND (case_number IS NULL OR case_number = '')"),
            batch,
        )
        updated_total += r.rowcount
print(f"Updated {updated_total} agenda_items", flush=True)

# Deduplicate
unique_cases = {}
for item_id, case in updates.items():
    if case not in unique_cases:
        prefix = case.split("-")[0] if "-" in case else case[:2]
        unique_cases[case] = prefix
print(f"Unique cases: {len(unique_cases)}", flush=True)

# Insert into cases table
case_list = [{"case": c, "ctype": p, "now": now} for c, p in unique_cases.items()]
if case_list:
    for i in range(0, len(case_list), BATCH):
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO cases (case_number, case_type, created_at, updated_at)
                    VALUES (:case, :ctype, :now, :now)
                    ON CONFLICT (case_number) DO UPDATE SET updated_at = :now
                """),
                case_list[i : i + BATCH],
            )
print(f"Synced {len(case_list)} cases", flush=True)

# Get case IDs
case_ids = {}
with engine.connect() as c:
    for case in unique_cases:
        r = c.execute(text("SELECT id FROM cases WHERE case_number = :c"), {"c": case}).scalar()
        if r:
            case_ids[case] = r

# Create case_events
events = []
for item_id, case in updates.items():
    cid = case_ids.get(case)
    if cid:
        events.append({"cid": cid, "aid": item_id, "now": now})

ev_cnt = 0
for i in range(0, len(events), BATCH):
    with engine.begin() as conn:
        r = conn.execute(
            text("""
                INSERT INTO case_events (case_id, agenda_item_id, source, event_type, event_date, created_at, updated_at)
                VALUES (:cid, :aid, 'backfill', 'agenda_item', :now::date, :now, :now)
                ON CONFLICT DO NOTHING
            """),
            events[i : i + BATCH],
        )
        ev_cnt += r.rowcount

print(f"Created {ev_cnt} case_events", flush=True)
print("Done!", flush=True)
