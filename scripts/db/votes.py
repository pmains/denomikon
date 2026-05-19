"""votes module."""

import logging
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, select, text, case
from sqlalchemy.orm import Session

from db.models import (AgendaItem, AgendaItemVote, SupervisorVote,
    Supervisor, Meeting, BodyMembership, Person)
from db.core import get_session


# Controversy detection constants
_CONTROVERSY_KEYWORDS = [
    "controvers", "contentious", "split", "divided", "content",
    "objection", "opposition", "debate", "disagree", "dispute",
    "close vote", "tie", "deadlock", "impasse",
    "overrule", "overruled", "appeal",
    "recusal", "abstain",
    "public hearing", "public comment",
    "zoning", "rezon", "variance", "special use",
    "conditional use", "text amendment", "comprehensive plan",
    "general plan", "land use", "development agreement",
]

import re
_DOLLAR_PATTERN = re.compile(r'\$[0-9,]+')

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
        select(SupervisorVote.vote)
        .where(SupervisorVote.agenda_item_vote_id == aiv_id)
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
        select(SupervisorVote.vote)
        .where(SupervisorVote.agenda_item_vote_id == aiv_id)
    ).scalars().all()
    norm = [_normalize_vote_value(v) for v in votes]
    yes = sum(1 for v in norm if v == "yes")
    no = sum(1 for v in norm if v == "no")
    abstain = sum(1 for v in norm if v == "abstain")
    return {"yes": yes, "no": no, "abstain": abstain, "total": len(votes)}

def detect_controversy_flags(
    item_title: str = "",
    item_text: str = "",
    is_split_vote: bool = False,
    motion_result: str = "",
    has_abstention: bool = False,
) -> list[str]:
    """Detect controversy flags for an agenda item.

    Returns a list of reason strings like ["split", "keyword: zoning"]
    """
    flags: list[str] = []
    combined = f"{item_title} {item_text}".lower()

    if is_split_vote:
        flags.append("split")

    if has_abstention:
        flags.append("abstention")

    mr = motion_result.lower().strip()
    if mr in ("continued", "denied", "deny"):
        flags.append(f"motion_{mr}")

    # Check keywords
    for kw in _CONTROVERSY_KEYWORDS:
        if kw in combined:
            flags.append(f"keyword: {kw}")

    # Check for dollar amounts
    if _DOLLAR_PATTERN.search(combined):
        flags.append("dollar-amount")

    return flags

