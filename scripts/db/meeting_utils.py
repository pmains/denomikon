"""meeting_utils module."""

import logging
import re
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.models import Meeting, AgendaItem
from db.core import get_engine, get_session

_KNWON_MEETING_TYPES = {"formal", "informal", "special", "executive"}

def normalize_meeting_type(raw_type: str, raw_title: str = "") -> str:
    """Normalize a meeting type string to one of: Formal, Informal, Special, Executive.

    Examples:
        "Formal Meeting" → "Formal"
        "Special" → "Special"
        "Special Executive" → "Executive" (context is extracted separately)
        "Executive (CONTINUED)" → "Executive"
    """
    combined = f"{raw_type} {raw_title}".lower()
    # Check longer/more specific terms first to avoid substring false matches
    # (e.g. "informal" contains "formal")
    for t in ("executive", "informal", "formal", "special"):
        if t in combined:
            return t.capitalize()
    # Fallback: return raw_type as-is for custom types (e.g., "Planning & Zoning")
    raw = (raw_type or "").strip()
    if raw:
        return raw
    return "Unknown"

def extract_meeting_context(raw_title: str, meeting_type: str) -> Optional[str]:
    """Extract meaningful context from raw title.

    Special/Election of Chairman → "Election of Chairman"
    Emergency Meeting → "Emergency"
    Special Executive → None (handled by meeting_type normalization)
    4467 → None
    BOARD OF SUPERVISORS... → None

    Returns None when no meaningful context found.
    """
    t = (raw_title or "").strip()
    if not t:
        return None

    # Skip if entirely numeric (meeting ID as title)
    if re.match(r'^\d+$', t):
        return None

    # Skip if just a body/header
    if "BOARD OF SUPERVISORS" in t.upper():
        return None

    # Compute lower once
    lower = t.lower()

    # Skip P&Z boilerplate: "Planning and Zoning Commission Meeting"
    # adds no meaningful context beyond what meeting_type="Planning & Zoning" already conveys
    if re.search(r'planning\s+and\s+zoning', lower):
        return None

    # Skip venue/connection boilerplate commonly in PZ titles
    if re.search(r'bos\s*auditorium|gotowebinar|webinar', lower):
        return None

    # Skip if it's just a known meeting type word
    if lower in ("formal", "informal", "special", "executive", "formal meeting", "informal meeting", "special meeting", "executive meeting"):
        return None

    # "Emergency Meeting" → "Emergency"
    if re.search(r'\bemergency\s+meeting\b', lower):
        return "Emergency"

    # "Special/Election of Chairman" type patterns
    # Look for content after a slash or after "Special/"
    slash_m = re.search(r'/(.+)$', t)
    if slash_m:
        candidate = slash_m.group(1).strip()
        if candidate and candidate.lower() not in _KNWON_MEETING_TYPES:
            return candidate

    # "Special/Call" → "Call"
    if re.search(r'\bspecial\s*/\s*(.+)', lower):
        context = re.search(r'\bspecial\s*/\s*(.+)', lower, re.I)
        if context:
            return context.group(1).strip()

    # If title is a useful phrase (not just a type), return it
    # Remove known type words
    cleaned = re.sub(r'\b(formal|informal|special|executive|meeting)\b', '', lower, flags=re.I).strip()
    if cleaned and len(cleaned) > 3:
        return t.strip()  # Return original formatting

    return None

def extract_meeting_body(raw_title: str) -> Optional[str]:
    """Extract body name from raw title.

    BOARD OF SUPERVISORS - JUNTA DE SUPERVISORES → "Board of Supervisors"

    Returns None if no body identified.
    """
    t = (raw_title or "").strip()
    if "BOARD OF SUPERVISORS" in t.upper():
        return "Board of Supervisors"
    # Other bodies could be added here
    return None

def build_meeting_display_name(meeting_type: str, meeting_date: str, meeting_context: Optional[str] = None) -> str:
    """Build a canonical display name from structured fields.

    Format:
        If meeting_context: "{Meeting Type} Meeting — {Context} — {Mon D, YYYY}"
        Else: "{Meeting Type} Meeting — {Mon D, YYYY}"
    """
    mtype = (meeting_type or "Meeting").strip()
    if not mtype.lower().endswith("meeting"):
        mtype = f"{mtype} Meeting"

    # Parse date
    try:
        parts = meeting_date.split("-")
        dt = date(int(parts[0]), int(parts[1]), int(parts[2]))
        date_str = dt.strftime("%b %-d, %Y")  # "Mar 20, 2026"
    except (IndexError, ValueError):
        date_str = meeting_date

    if meeting_context:
        return f"{mtype} — {meeting_context} — {date_str}"
    return f"{mtype} — {date_str}"

def backfill_meeting_normalization(session, force: bool = False):
    """Iterate over all meetings and apply normalization to new fields.

    If force=True, updates all meetings even if display_name is already set.
    """
    q = select(Meeting)
    if not force:
        q = q.where(Meeting.display_name.is_(None))
    meetings = list(session.execute(q).scalars().all())

    count = 0
    for m in meetings:
        raw_type = m.meeting_type or ""
        raw_title = m.meeting_title_raw or m.meeting_title or ""

        m.meeting_type = normalize_meeting_type(raw_type, raw_title)
        m.meeting_context = extract_meeting_context(raw_title, raw_type)
        m.meeting_body = extract_meeting_body(raw_title)
        m.display_name = build_meeting_display_name(
            m.meeting_type,
            m.meeting_date,
            m.meeting_context,
        )
        count += 1

    if count:
        session.commit()
    return count

def is_canceled_meeting(meeting_dict_or_title) -> bool:
    """Check whether a meeting title/type indicates it was canceled.

    Accepts either a string (title) or a dict with 'meeting_title' and/or
    'meeting_type' keys.  Returns True if the title contains CANCELED,
    CANCELLED, or CANCEL (case-insensitive).
    """
    import re
    if isinstance(meeting_dict_or_title, dict):
        title = meeting_dict_or_title.get("meeting_title", "") or ""
        mtype = meeting_dict_or_title.get("meeting_type", "") or ""
        text = title + " " + mtype
    else:
        text = str(meeting_dict_or_title)
    return bool(re.search(r"\bCANCEL(?:LED|LED|ED)?\b", text, re.IGNORECASE))

def mark_meeting_canceled(session, body: str, meeting_id: str) -> None:
    """Mark a meeting as canceled (no_agenda) in the database."""
    from sqlalchemy import update as sa_update
    session.execute(
        sa_update(Meeting)
        .where(Meeting.body == body, Meeting.meeting_id == meeting_id)
        .values(
            sync_status="no_agenda",
            last_error="Meeting was canceled",
            last_attempted_at=None,
            retry_count=0,
        )
    )
    session.commit()

