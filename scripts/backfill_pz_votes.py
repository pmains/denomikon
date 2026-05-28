"""Batch backfill PZ votes from meeting minutes. Run locally."""
import sys, os, datetime, logging, urllib.request

# Set up path to find project modules
project_root = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

logging.basicConfig(level=logging.WARNING)

from db import set_database_url, get_session, persist_pz_votes
from db.models import Meeting, AgendaItemVote, MemberVote
from scraper.pz_minutes import extract_votes_from_minutes
from sqlalchemy import select, func

db_url = os.environ.get("DATABASE_URL", f"sqlite:///{project_root}/data/maricopa.sqlite")
set_database_url(db_url)

session = get_session()

meetings = session.query(Meeting).filter(
    Meeting.body == 'pz',
).order_by(Meeting.meeting_date).all()
