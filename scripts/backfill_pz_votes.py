"""Batch backfill PZ votes from meeting minutes. Run locally."""
import sys, os, datetime, logging, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))

logging.basicConfig(level=logging.WARNING)

from db import set_database_url, get_session, persist_pz_votes
from db.models import Meeting, AgendaItemVote, MemberVote
from scraper.pz_minutes import extract_votes_from_minutes
from sqlalchemy import select, func

DB_PATH = '/Users/pmains/Code/openclaw/maricopa-agendas/data/maricopa.sqlite'
set_database_url(f'sqlite:///{DB_PATH}')

session = get_session()

meetings = session.query(Meeting).filter(
    Meeting.body == 'pz',
).order_by(Meeting.meeting_date).all()

print(f"Total PZ meetings: {len(meetings)}")

skipped_no_minutes = 0
already_had = 0
parsed = 0
total_votes = 0
errors = []

for m in meetings:
    mid = m.meeting_id
    try:
        dt = datetime.date.fromisoformat(m.meeting_date)
    except (ValueError, TypeError):
        print(f"  SKIP {mid}: bad date {m.meeting_date}")
        continue

    url_slug = f"_{dt.month:02d}{dt.day:02d}{dt.year}-{mid}"
    url = f"https://www.maricopa.gov/AgendaCenter/ViewFile/Minutes/{url_slug}"

    existing = session.execute(
        select(func.count()).select_from(MemberVote).join(
            AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id
        ).where(AgendaItemVote.meeting_id == mid)
    ).scalar() or 0
    if existing > 0:
        already_had += 1
        continue

    print(f"  {mid} ({m.meeting_date})", end='', flush=True)
    result = extract_votes_from_minutes(url)
    if not result or not result.get('votes'):
        print(" -> no votes found")
        skipped_no_minutes += 1
        continue

    votes = result['votes']
    absent_names = (result.get('commissioners') or {}).get('absent', [])
    print(f" -> {len(votes)} vote(s)" + (f", {len(absent_names)} absent" if absent_names else ""), end='', flush=True)

    try:
        count = persist_pz_votes(session, mid, votes, absent_names)
        print(f", {count} member-votes")
        parsed += 1
        total_votes += count
    except Exception as e:
        session.rollback()
        print(f" ERROR: {e}")
        errors.append((mid, str(e)))

print()
print(f"Summary:")
print(f"  Parsed: {parsed} meetings, {total_votes} member-votes")
print(f"  Already had votes: {already_had}")
print(f"  No minutes available: {skipped_no_minutes}")
print(f"  Errors: {len(errors)}")
for mid, err in errors[:5]:
    print(f"    {mid}: {err}")

session.close()
