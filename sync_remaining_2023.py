#!/usr/bin/env python3
"""Sync remaining Phoenix 2023 meetings one at a time with timeouts."""
import sys
import os
import subprocess
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
logging.basicConfig(level=logging.ERROR)

from scraper.phoenix_rss import search_meetings_via_html
from db import get_session, Meeting
from sqlalchemy import select

CWD = os.path.dirname(os.path.abspath(__file__))

def sync_one(mid, mdate, mtype, guid, timeout=45):
    code = f'''import sys; sys.path.insert(0, '{CWD}/scripts')
import logging; logging.basicConfig(level=logging.ERROR)
from db import get_session, init_db, replace_meeting_data_safe
from scraper.phoenix_rss import fetch_meeting_items_via_rss
init_db()
session = get_session()
try:
    items, supp_docs = fetch_meeting_items_via_rss("{mid}", "{guid}", "phoenix-cc", leg_limit=0)
    meeting_dict = {{"meeting_id": "{mid}", "meeting_date": "{mdate}", "meeting_type": "{mtype}", "meeting_title": "{mtype}", "source_url": ""}}
    replace_meeting_data_safe(session, "phoenix-cc", "{mid}", meeting_dict, items, supporting_doc_dicts=supp_docs)
    print(f'OK: {{len(items)}} items')
except Exception as e:
    session.rollback()
    print(f'FAIL: {{e}}')
session.close()
'''
    proc = subprocess.Popen(
        ['python3', '-u', '-c', code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=CWD
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        out = stdout.decode().strip()
        # Filter out the [config] line
        lines = [l for l in out.split('\n') if not l.startswith('[config]')]
        return '\n'.join(lines).strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return 'TIMEOUT'
    except:
        proc.kill()
        proc.wait()
        return 'ERROR'

# Get meetings from Legistar
print("Fetching meeting list from Legistar...")
all_m = search_meetings_via_html(2023)
lookup = {m['meeting_id']: m for m in all_m}

# Find which aren't in DB
session = get_session()
remaining = [(mid, m['meeting_date'], m.get('meeting_type',''), m.get('meeting_guid',''))
             for mid, m in lookup.items()
             if not session.execute(select(Meeting).where(
                 Meeting.body == 'phoenix-cc', Meeting.meeting_id == mid
             )).scalar_one_or_none()]
session.close()

print(f"{len(remaining)} meetings to sync")
ok = fail = timeout = 0

for mid, mdate, mtype, guid in remaining:
    result = sync_one(mid, mdate, mtype, guid)
    if 'OK:' in result:
        ok += 1
        ok_part = result.split('OK:')[1].strip() if 'OK:' in result else ''
        print(f"OK  {mid} {mdate} {ok_part}")
    elif result == 'TIMEOUT':
        # Retry with longer timeout
        result2 = sync_one(mid, mdate, mtype, guid, timeout=90)
        if 'OK:' in result2:
            ok += 1
            ok_part = result2.split('OK:')[1].strip() if 'OK:' in result2 else ''
            print(f"OK  {mid} {mdate} {ok_part} (retry)")
        else:
            timeout += 1
            print(f"TO  {mid} {mdate} (timeout x2)")
    else:
        fail += 1
        print(f"FAIL {mid} {mdate}: {result[:60]}")

print(f"\nDone: {ok} OK, {fail} fail, {timeout} timeout")
