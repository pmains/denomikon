"""votes module."""

import logging
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, select, text, case
from sqlalchemy.orm import Session

from db.models import (AgendaItem, AgendaItemVote, MemberVote,
    Supervisor, Meeting, BodyMembership, Person)
from db.core import get_session


def _normalize_vote_value(vote: str) -> str:
    """Normalize a vote value to canonical form."""
    v = (vote or "").lower().strip()
    if v in ("yes", "aye"):
        return "yes"
    if v in ("no", "nay"):
        return "no"
    if v in ("abstain", "abstained"):
        return "abstain"
    if v == "absent":
        return "absent"
    if v == "recused":
        return "recused"
    return v

def _make_supervisor_slug(sup: Supervisor) -> str:
    """Derive a URL-safe slug from a supervisor record."""
    return sup.normalized_name.replace(" ", "-")

def infer_majority_position(session, aiv_id: int) -> Optional[str]:
    """Infer the majority position for an agenda item vote from individual votes.

    Returns 'yes', 'no', 'tie', or None if cannot determine.
    Only considers substantive votes (yes/no).
    """
    votes = session.execute(
        select(MemberVote.vote)
        .where(MemberVote.agenda_item_vote_id == aiv_id)
    ).scalars().all()
    norm = [_normalize_vote_value(v) for v in votes]
    yes_cnt = sum(1 for v in norm if v == "yes")
    no_cnt = sum(1 for v in norm if v == "no")
    if yes_cnt == 0 and no_cnt == 0:
        return None
    if yes_cnt > no_cnt:
        return "yes"
    if no_cnt > yes_cnt:
        return "no"
    return "tie"

def compute_vote_tally(session, aiv_id: int) -> dict:
    """Compute vote tally for a single agenda item vote.

    Returns dict with yes, no, abstain counts and total.
    """
    votes = session.execute(
        select(MemberVote.vote)
        .where(MemberVote.agenda_item_vote_id == aiv_id)
    ).scalars().all()
    norm = [_normalize_vote_value(v) for v in votes]
    yes = sum(1 for v in norm if v == "yes")
    no = sum(1 for v in norm if v == "no")
    abstain = sum(1 for v in norm if v == "abstain")
    return {"yes": yes, "no": no, "abstain": abstain, "total": len(votes)}



