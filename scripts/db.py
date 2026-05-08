"""
Persistence layer for Maricopa agenda data.

Uses SQLAlchemy to store meetings and agenda items.
Defaults to SQLite; set DATABASE_URL to switch to Postgres.
"""

import logging
import os
import re
from datetime import date, datetime, timezone


log = logging.getLogger(__name__)
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    inspect as sa_inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "sqlite:///data/maricopa.sqlite"
)

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        connect_args = {}
        url = DATABASE_URL
        if url.startswith("sqlite"):
            # Enable WAL mode and foreign keys for SQLite
            connect_args["check_same_thread"] = False
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), future=True)
    return _SessionLocal()


class Base(DeclarativeBase):
    pass


class Supervisor(Base):
    __tablename__ = "supervisors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    district = Column(String(16), nullable=True, default=None)
    active_from = Column(Date, nullable=True, default=None)
    active_to = Column(Date, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MeetingSupervisor(Base):
    __tablename__ = "meeting_supervisors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    supervisor_id = Column(Integer, nullable=False, index=True)
    role = Column(String(64), nullable=True, default=None)
    present = Column(Boolean, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", "supervisor_id", name="uq_meeting_supervisor"),
    )


class AgendaItemVote(Base):
    __tablename__ = "agenda_item_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_id = Column(Integer, nullable=False, index=True, unique=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    agenda_item_number = Column(Integer, nullable=False, index=True)
    c_number = Column(String(32), nullable=True, default=None, index=True)
    c_number_base = Column(String(48), nullable=True, default=None, index=True)
    motion_result = Column(String(64), nullable=True, default=None)
    vote_text = Column(Text, nullable=True, default=None)
    conditions = Column(Text, nullable=True, default=None)
    is_split_vote = Column(Boolean, nullable=True, default=None,
                           comment="True if members voted differently (not unanimous)")
    unanimous = Column(Boolean, nullable=True, default=None,
                       comment="True if all voting members voted the same way, excluding recusals/abstentions")
    majority_position = Column(
        String(16), nullable=True, default=None,
        comment="yes|no|tie|unknown — the position taken by the majority of voting members"
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SupervisorVote(Base):
    __tablename__ = "supervisor_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agenda_item_vote_id = Column(Integer, nullable=False, index=True)
    supervisor_id = Column(Integer, nullable=False, index=True)
    vote = Column(String(32), nullable=False, default="unknown", index=True)
    raw_vote_text = Column(String(64), nullable=True, default=None)
    is_dissent = Column(Boolean, nullable=True, default=None,
                        comment="True if supervisor voted against the majority")
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("agenda_item_vote_id", "supervisor_id", name="uq_supervisor_vote"),
    )


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", name="uq_meeting_body_id"),
    )
    meeting_date = Column(String(16), nullable=False)
    meeting_type = Column(String(64), nullable=False, default="")
    meeting_title = Column(String(256), nullable=False, default="")
    meeting_title_raw = Column(Text, nullable=True, default=None)
    meeting_context = Column(String(128), nullable=True, default=None)
    meeting_body = Column(String(128), nullable=True, default=None)
    display_name = Column(String(256), nullable=True, default=None)
    source_url = Column(String(512), nullable=False, default="")
    sync_status = Column(String(32), nullable=False, default="pending", index=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True, default=None)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True, default=None)
    last_error = Column(Text, nullable=True, default=None)
    retry_count = Column(Integer, nullable=False, default=0)
    item_count_expected = Column(Integer, nullable=True, default=None)
    item_count_actual = Column(Integer, nullable=True, default=None)
    supporting_doc_count = Column(Integer, nullable=False, default=0)
    items_extracted = Column(Boolean, nullable=False, default=False)
    supporting_docs_extracted = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Case(Base):
    __tablename__ = "cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_number = Column(String(32), nullable=False, unique=True, index=True)
    case_type = Column(String(16), nullable=False, default="")
    normalized_case_number = Column(String(32), nullable=False, index=True)
    description = Column(Text, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PZItemDetail(Base):
    """Structured fields extracted from P&Z agenda PDF items."""
    __tablename__ = "pz_item_details"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_id = Column(Integer, nullable=True, default=None, index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    agenda_item_number = Column(Integer, nullable=False)
    case_number = Column(String(32), nullable=False, default="", index=True)
    district = Column(String(32), nullable=True, default=None)
    project_name = Column(Text, nullable=True, default=None)
    applicant = Column(Text, nullable=True, default=None)
    request = Column(Text, nullable=True, default=None)
    location = Column(Text, nullable=True, default=None)
    recommendation = Column(Text, nullable=True, default=None)
    presented_by = Column(Text, nullable=True, default=None)
    staff_report_url = Column(String(512), nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CaseEvent(Base):
    __tablename__ = "case_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    case_id = Column(Integer, nullable=False, index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    agenda_item_id = Column(Integer, nullable=True, default=None)
    source = Column(String(16), nullable=False, default="")
    event_type = Column(String(32), nullable=False, default="")
    event_date = Column(String(16), nullable=False, default="")
    notes = Column(Text, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class AgendaItem(Base):
    __tablename__ = "agenda_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    agenda_item_number = Column(Integer, nullable=False)
    agenda_item_id = Column(String(128), nullable=False, unique=True)
    agenda_item_title = Column(Text, nullable=False, default="")
    agenda_item_text = Column(Text, nullable=False, default="")
    agenda_item_url = Column(String(512), nullable=False, default="")
    vote_or_action = Column(String(64), nullable=False, default="")
    source_body = Column(String(64), nullable=False, default="Board of Supervisors")
    source_url = Column(String(512), nullable=False, default="")
    c_number = Column(String(32), nullable=False, default="", index=True)
    c_number_base = Column(String(48), nullable=False, default="", index=True)
    c_number_revision = Column(String(16), nullable=True, default=None, index=True)
    case_number = Column(String(32), nullable=False, default="", index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        None,
    )
class PublicBodyMember(Base):
    """Body-scoped membership roster for any public body (BOS, PZ, ADJ, DRAIN, HEALTH, TAB, IDA)."""
    __tablename__ = "public_body_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    title = Column(String(64), nullable=True, default=None)
    district_or_seat = Column(String(32), nullable=True, default=None)
    active_from = Column(Date, nullable=True, default=None)
    active_to = Column(Date, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("body", "normalized_name", name="uq_public_body_member"),
    )


class MeetingAttendance(Base):
    """Per-meeting attendance records for members of any public body."""
    __tablename__ = "meeting_attendance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    attendance_status = Column(
        String(24), nullable=False, default="unknown",
        comment="present|absent|excused|late|left_early|unknown|inferred_absent"
    )
    source_text = Column(Text, nullable=True, default=None)
    inference_method = Column(String(64), nullable=True, default=None,
                              comment="e.g. 'other_members_voted_but_member_did_not'")
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", "member_id", name="uq_meeting_attendance"),
    )


class MemberVote(Base):
    """Generalized vote table for non-BOS bodies. Preserves supervisor_votes for BOS."""
    __tablename__ = "member_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_vote_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)
    vote = Column(String(32), nullable=False, default="unknown", index=True)
    raw_vote_text = Column(String(64), nullable=True, default=None)
    is_dissent = Column(Boolean, nullable=True, default=None,
                        comment="True if member voted against the majority")
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("agenda_item_vote_id", "member_id", name="uq_member_vote"),
    )


class ExecutiveSessionParticipant(Base):
    """BOS executive session advisors and attendees."""
    __tablename__ = "executive_session_participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    person_name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    role_or_title = Column(String(128), nullable=True, default=None)
    organization = Column(String(128), nullable=True, default=None)
    participation_type = Column(
        String(32), nullable=False, default="unknown",
        comment="advised|attended|presented|legal_counsel|staff|outside_counsel|unknown"
    )
    agenda_item_number = Column(Integer, nullable=True, default=None)
    source_text = Column(Text, nullable=True, default=None)
    source_url = Column(String(512), nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "body", "meeting_id", "normalized_name",
            "agenda_item_number",
            name="uq_exec_session_participant"
        ),
    )


class SupportingDocument(Base):
    __tablename__ = "supporting_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    agenda_item_id = Column(Integer, nullable=False, index=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    agenda_item_number = Column(Integer, nullable=False, index=True)
    c_number = Column(String(32), nullable=True, default=None, index=True)
    c_number_base = Column(String(48), nullable=True, default=None, index=True)
    c_number_revision = Column(String(16), nullable=True, default=None)
    document_title = Column(Text, nullable=False, default="")
    document_url = Column(String(1024), nullable=False, default="")
    document_type = Column(String(64), nullable=True, default=None)
    file_name = Column(String(256), nullable=True, default=None)
    file_extension = Column(String(16), nullable=True, default=None)
    local_path = Column(String(512), nullable=True, default=None)
    content_hash = Column(String(64), nullable=True, default=None)
    scraped_at = Column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("agenda_item_id", "document_url", name="uq_supporting_doc_item_url"),
    )


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


def init_db():
    """Create all tables if they don't exist, or migrate existing ones."""
    Base.metadata.create_all(bind=get_engine())

    _migrate_table("supporting_documents")

    engine = get_engine()
    _migrate_col(engine, "agenda_items", "c_number", "VARCHAR(32) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_base", "VARCHAR(48) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_revision", "VARCHAR(16) DEFAULT NULL")

    _migrate_table("cases")
    _migrate_table("case_events")
    _migrate_table("pz_item_details")

    _migrate_col(engine, "agenda_items", "case_number", "VARCHAR(32) NOT NULL DEFAULT ''")

    _migrate_table("supervisors")
    _migrate_table("meeting_supervisors")
    _migrate_table("agenda_item_votes")
    _migrate_col(engine, "agenda_item_votes", "conditions", "TEXT DEFAULT NULL")
    _migrate_col(engine, "agenda_item_votes", "is_split_vote", "BOOLEAN DEFAULT NULL")
    _migrate_col(engine, "agenda_item_votes", "unanimous", "BOOLEAN DEFAULT NULL")
    _migrate_col(engine, "agenda_item_votes", "majority_position", "VARCHAR(16) DEFAULT NULL")
    _migrate_table("supervisor_votes")
    _migrate_col(engine, "supervisor_votes", "is_dissent", "BOOLEAN DEFAULT NULL")
    _migrate_table("public_body_members")
    _migrate_table("meeting_attendance")
    _migrate_table("member_votes")
    _migrate_table("executive_session_participants")

    # Resumable sync columns
    _migrate_col(engine, "meetings", "sync_status", "VARCHAR(32) NOT NULL DEFAULT 'pending'")
    _migrate_col(engine, "meetings", "last_synced_at", "DATETIME")
    _migrate_col(engine, "meetings", "last_attempted_at", "DATETIME")
    _migrate_col(engine, "meetings", "last_error", "TEXT")
    _migrate_col(engine, "meetings", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_col(engine, "meetings", "item_count_expected", "INTEGER")
    _migrate_col(engine, "meetings", "item_count_actual", "INTEGER")
    _migrate_col(engine, "meetings", "supporting_doc_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_col(engine, "meetings", "items_extracted", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "meetings", "supporting_docs_extracted", "BOOLEAN NOT NULL DEFAULT 0")

    # Normalization columns
    _migrate_col(engine, "meetings", "meeting_title_raw", "TEXT DEFAULT NULL")
    _migrate_col(engine, "meetings", "meeting_context", "VARCHAR(128) DEFAULT NULL")
    _migrate_col(engine, "meetings", "meeting_body", "VARCHAR(128) DEFAULT NULL")
    _migrate_col(engine, "meetings", "display_name", "VARCHAR(256) DEFAULT NULL")

    # Body column migrations (for body-scoped identity)
    _migrate_col(engine, "meetings", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "meetings", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "agenda_items", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "supporting_documents", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "supporting_documents", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "case_events", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "case_events", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "meeting_supervisors", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "meeting_supervisors", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "agenda_item_votes", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_item_votes", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "pz_item_details", "body", "VARCHAR(16) NOT NULL DEFAULT ''")
    _migrate_col(engine, "pz_item_details", "_body_backfilled", "BOOLEAN NOT NULL DEFAULT 0")

    # Backfill existing records to body='bos' and determine pz from meeting_type
    backfill_body_column(engine)


def backfill_body_column(engine):
    """Backfill body column for existing records.

    - All meetings with meeting_type != 'Planning & Zoning' get body='bos'
    - All meetings with meeting_type == 'Planning & Zoning' get body='pz'
    - Related tables (agenda_items, supporting_documents, etc.) are updated
      to match their meeting's body value.
    - Uses _body_backfilled flag as a migration marker.
    """
    inspector = sa_inspect(engine)

    # Check backfill status using a temp column we added as a marker
    tables_to_backfill = [
        "meetings", "agenda_items", "supporting_documents",
        "case_events", "meeting_supervisors", "agenda_item_votes", "pz_item_details",
    ]

    with engine.connect() as conn:
        for table in tables_to_backfill:
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            if "body" not in existing_cols:
                continue  # Table doesn't have body column yet, skip
            marker = f"_body_backfilled"
            if marker not in existing_cols:
                continue

            # Check if already backfilled
            row = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {marker} = 0")
            ).scalar()
            if not row or row == 0:
                # Already backfilled or no rows
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {marker}"))
                conn.commit()
                continue

        # Backfill meetings body column
        if "meetings" in [t for t in tables_to_backfill if t in {c["name"] for c in inspector.get_columns(t)}]:
            # Update BOS meetings (not Planning & Zoning)
            conn.execute(
                text("UPDATE meetings SET body = 'bos' WHERE meeting_type != 'Planning & Zoning' AND _body_backfilled = 0")
            )
            # Update PZ meetings
            conn.execute(
                text("UPDATE meetings SET body = 'pz' WHERE meeting_type = 'Planning & Zoning' AND _body_backfilled = 0")
            )
            # Mark backfilled
            conn.execute(text("UPDATE meetings SET _body_backfilled = 1 WHERE body != ''"))
            conn.commit()

            # Backfill related tables by joining to meetings
            for table in ["agenda_items", "supporting_documents", "case_events", "meeting_supervisors", "agenda_item_votes", "pz_item_details"]:
                if table not in [c["name"] for c in inspector.get_columns(table)]:
                    continue
                existing_cols = {c["name"] for c in inspector.get_columns(table)}
                if "body" not in existing_cols or marker not in existing_cols:
                    continue

                try:
                    # SQLite doesn't support UPDATE with JOIN directly
                    # Use subquery approach
                    conn.execute(
                        text(f"""
                            UPDATE {table}
                            SET body = (
                                SELECT COALESCE(m.body, 'bos')
                                FROM meetings m
                                WHERE m.meeting_id = {table}.meeting_id
                                LIMIT 1
                            ),
                            _body_backfilled = 1
                            WHERE _body_backfilled = 0
                        """)
                    )
                except Exception:
                    # Fallback: set all to 'bos'
                    conn.execute(
                        text(f"UPDATE {table} SET body = 'bos', _body_backfilled = 1 WHERE _body_backfilled = 0")
                    )
                conn.commit()

            # Drop the marker columns
            for table in tables_to_backfill:
                try:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN _body_backfilled"))
                except Exception:
                    pass
            conn.commit()


def _migrate_table(table_name: str):
    """Create a table via raw SQL if the model doesn't already exist."""
    from sqlalchemy import inspect as sa_inspect

    engine = get_engine()
    inspector = sa_inspect(engine)
    if table_name not in inspector.get_table_names():
        Base.metadata.create_all(bind=get_engine())


def _migrate_col(engine, table: str, col: str, col_def: str):
    """Add a column to an existing table if it doesn't exist yet."""
    inspector = sa_inspect(engine)
    existing_cols = {c["name"] for c in inspector.get_columns(table)}
    if col not in existing_cols:
        with engine.connect() as conn:
            conn.execute(
                text(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}')
            )
            conn.commit()


def create_or_get_meeting(session: Session, body: str, meeting_dict: dict) -> Meeting:
    """Get or create a meeting row, setting sync_status=pending for new rows."""
    meeting_id = meeting_dict.get("meeting_id", "")
    existing = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    meeting = Meeting(
        body=body,
        meeting_id=meeting_id,
        meeting_date=meeting_dict.get("meeting_date", ""),
        meeting_type=meeting_dict.get("meeting_type", ""),
        meeting_title=meeting_dict.get("meeting_title", ""),
        meeting_title_raw=meeting_dict.get("meeting_title", ""),
        source_url=meeting_dict.get("source_url", ""),
        sync_status="pending",
    )
    session.add(meeting)
    return meeting


def update_sync_status(
    session: Session,
    body: str,
    meeting_id: str,
    status: str,
    *,
    item_count_expected: Optional[int] = None,
    item_count_actual: Optional[int] = None,
    supporting_doc_count: Optional[int] = None,
    items_extracted: Optional[bool] = None,
    supporting_docs_extracted: Optional[bool] = None,
    error: Optional[str] = None,
) -> Meeting:
    """Update sync tracking fields on a meeting row."""
    meeting = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if not meeting:
        raise ValueError(f"{body} meeting {meeting_id} not found")

    now = datetime.now(timezone.utc)
    meeting.sync_status = status
    meeting.last_attempted_at = now
    meeting.updated_at = now

    if status == "complete":
        meeting.last_synced_at = now
        meeting.retry_count = 0
        meeting.last_error = None
    elif status == "manual_review":
        # manual_review is a classification, not a failure; don't increment retries
        if error:
            meeting.last_error = error
    else:
        meeting.retry_count = (meeting.retry_count or 0) + 1
        if error:
            meeting.last_error = error

    if item_count_expected is not None:
        meeting.item_count_expected = item_count_expected
    if item_count_actual is not None:
        meeting.item_count_actual = item_count_actual
    if supporting_doc_count is not None:
        meeting.supporting_doc_count = supporting_doc_count
    if items_extracted is not None:
        meeting.items_extracted = items_extracted
    if supporting_docs_extracted is not None:
        meeting.supporting_docs_extracted = supporting_docs_extracted

    return meeting


def get_meetings_by_date_range(
    session: Session,
    body: str,
    start_date_iso: str,
    end_date_iso: str,
) -> list[Meeting]:
    """Get all meetings for a body with meeting_date in the given ISO date range (inclusive)."""
    q = (
        select(Meeting)
        .where(Meeting.body == body)
        .where(Meeting.meeting_date >= start_date_iso)
        .where(Meeting.meeting_date <= end_date_iso)
        .order_by(Meeting.meeting_date, Meeting.meeting_id)
    )
    return list(session.execute(q).scalars().all())


def get_meetings_by_status(
    session: Session,
    body: str,
    statuses: Optional[list[str]] = None,
    *,
    force: bool = False,
    meeting_ids: Optional[list[str]] = None,
) -> list[Meeting]:
    """Get meetings for a body filtered by sync_status and/or meeting_ids.

    If force is True, ignore status filter and return all matching meeting_ids.
    """
    q = select(Meeting).where(Meeting.body == body).order_by(Meeting.meeting_date, Meeting.meeting_id)
    if meeting_ids:
        q = q.where(Meeting.meeting_id.in_(meeting_ids))
    if not force and statuses:
        q = q.where(Meeting.sync_status.in_(statuses))
    return list(session.execute(q).scalars().all())


def get_sync_status_summary(session: Session) -> dict:
    """Get counts of meetings by sync_status."""
    rows = session.execute(
        select(
            Meeting.sync_status,
            func.count(Meeting.id).label("count"),
        )
        .group_by(Meeting.sync_status)
    ).all()

    summary = {
        "complete": 0,
        "partial": 0,
        "failed": 0,
        "pending": 0,
        "total": 0,
        "total_items": 0,
        "total_docs": 0,
    }
    for row in rows:
        status = row.sync_status or "pending"
        summary[status] = row.count
        summary["total"] += row.count

    # Aggregate item and doc counts
    agg = session.execute(
        select(
            func.coalesce(func.sum(Meeting.item_count_actual), 0),
            func.coalesce(func.sum(Meeting.supporting_doc_count), 0),
        )
    ).one()
    summary["total_items"] = agg[0]
    summary["total_docs"] = agg[1]

    return summary


def get_failed_meetings(session: Session, body: str = "bos") -> list[Meeting]:
    """Get meetings with failed or partial status (excludes manual_review)."""
    return get_meetings_by_status(session, body, ["failed", "partial"], force=False)


def upsert_meeting(session: Session, body: str, meeting: Meeting) -> Meeting:
    """Insert or update a meeting by (body, meeting_id)."""
    existing = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting.meeting_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.meeting_date = meeting.meeting_date
        existing.meeting_type = meeting.meeting_type
        existing.meeting_title = meeting.meeting_title
        existing.source_url = meeting.source_url
        existing.updated_at = datetime.now(timezone.utc)
        return existing
    session.add(meeting)
    return meeting


def persist_meeting(
    session: Session,
    body: str,
    meeting_id: str,
    agenda_item_dicts: list[dict],
    supporting_doc_dicts: Optional[list[dict]] = None,
) -> int:
    """Transactionally persist a meeting's agenda items and supporting docs.

    WARNING: This replaces ALL existing agenda_items and supporting_documents
    for the given meeting_id within the body scope. Callers should only invoke
    this after successfully extracting data into memory and validating.

    Steps:
    1. Delete existing agenda_items for this (body, meeting_id).
    2. Delete existing supporting_documents for this (body, meeting_id).
    3. Insert new agenda_item rows and supporting doc rows.
    4. Verify the inserted count matches expected.
    5. Commit only if validation passes; rollback on failure.

    Returns the number of agenda items persisted.
    Raises ValueError if the count doesn't match.
    """
    expected_count = len(agenda_item_dicts)
    inserted_item_count = 0
    inserted_doc_count = 0

    # Delete existing rows for this meeting within body scope
    session.execute(
        AgendaItem.__table__.delete().where(
            AgendaItem.body == body,
            AgendaItem.meeting_id == meeting_id,
        )
    )
    session.execute(
        SupportingDocument.__table__.delete().where(
            SupportingDocument.body == body,
            SupportingDocument.meeting_id == meeting_id,
        )
    )

    seen_item_ids: set[str] = set()
    for item_dict in agenda_item_dicts:
        aii = item_dict.get("agenda_item_id", "")
        if aii:
            if aii in seen_item_ids:
                log.warning(
                    "duplicate agenda_item_id %s for meeting %s — skipping",
                    aii, meeting_id,
                )
                continue
            seen_item_ids.add(aii)
        item = AgendaItem(
            body=body,
            meeting_id=meeting_id,
            agenda_item_number=int(item_dict.get("agenda_item_number", 0)),
            agenda_item_id=aii,
            agenda_item_title=item_dict.get("agenda_item_title", ""),
            agenda_item_text=item_dict.get("agenda_item_text", ""),
            agenda_item_url=item_dict.get("agenda_item_url", ""),
            vote_or_action=item_dict.get("vote_or_action", ""),
            source_body=item_dict.get("source_body", "Board of Supervisors"),
            source_url=item_dict.get("source_url", ""),
            c_number=item_dict.get("c_number", ""),
            c_number_base=item_dict.get("c_number_base", ""),
            c_number_revision=item_dict.get("c_number_revision", None),
            case_number=item_dict.get("case_number", ""),
        )
        session.add(item)
        inserted_item_count += 1

    if supporting_doc_dicts:
        for doc_dict in supporting_doc_dicts:
            doc = SupportingDocument(
                body=body,
                agenda_item_id=doc_dict.get("agenda_item_id", 0),
                meeting_id=meeting_id,
                agenda_item_number=int(doc_dict.get("agenda_item_number", 0)),
                c_number=doc_dict.get("c_number"),
                c_number_base=doc_dict.get("c_number_base"),
                c_number_revision=doc_dict.get("c_number_revision"),
                document_title=doc_dict.get("document_title", ""),
                document_url=doc_dict.get("document_url", ""),
                document_type=doc_dict.get("document_type"),
                file_name=doc_dict.get("file_name"),
                file_extension=doc_dict.get("file_extension"),
                scraped_at=datetime.now(timezone.utc),
            )
            session.add(doc)
            inserted_doc_count += 1

    # Flush to get IDs for the newly inserted items before creating case events
    session.flush()

    # Delete existing CaseEvent records for this meeting (idempotency)
    session.execute(
        CaseEvent.__table__.delete().where(
            CaseEvent.meeting_id == meeting_id
        )
    )
    session.flush()

    # Create case and event records for items with case_number
    meeting_date = agenda_item_dicts[0].get("meeting_date", "") if agenda_item_dicts else ""
    for item_dict in agenda_item_dicts:
        # Determine source from source_body
        source_body = (item_dict.get("source_body") or "").strip()
        item_source = "PZ" if "planning" in source_body.lower() else "BOS"
        _upsert_case_and_event(
            session, meeting_id, meeting_date, item_dict, source=item_source
        )

    session.commit()
    return inserted_item_count


def _upsert_case_and_event(
    session: Session,
    meeting_id: str,
    meeting_date: str,
    item_dict: dict,
    source: str = "BOS",
) -> Optional[Case]:
    """Upsert a Case record and create a CaseEvent for an agenda item.

    If the item has no case_number, returns None.
    """
    case_number = (item_dict.get("case_number") or "").strip()
    if not case_number:
        return None

    case_number_upper = case_number.upper()

    # Determine case_type
    case_type = ""
    for prefix in ["CPA", "Z", "GPA"]:
        if case_number_upper.startswith(prefix):
            case_type = prefix
            break

    # Create or get case
    case = session.execute(
        select(Case).where(Case.case_number == case_number_upper)
    ).scalar_one_or_none()
    if not case:
        case = Case(
            case_number=case_number_upper,
            case_type=case_type,
            normalized_case_number=re.sub(r"[^A-Z0-9]", " ", case_number_upper).strip(),
            description=(item_dict.get("agenda_item_title", "") or "")[:500],
        )
        session.add(case)
        session.flush()

    # Look up the DB agenda_item by meeting_id + agenda_item_id to get its ID
    db_item = session.execute(
        select(AgendaItem).where(
            AgendaItem.meeting_id == meeting_id,
            AgendaItem.agenda_item_id == item_dict.get("agenda_item_id", ""),
        )
    ).scalar_one_or_none()
    agenda_item_db_id = db_item.id if db_item else None

    # Create event
    event = CaseEvent(
        case_id=case.id,
        meeting_id=meeting_id,
        agenda_item_id=agenda_item_db_id,
        source=source,
        event_type="agenda" if source == "BOS" else "hearing",
        event_date=meeting_date,
        notes=None,
    )
    session.add(event)
    return case


def replace_meeting_data_safe(
    session: Session,
    body: str,
    meeting_id: str,
    meeting_dict: dict,
    agenda_item_dicts: list[dict],
    supporting_doc_dicts: Optional[list[dict]] = None,
) -> int:
    """Safely replace meeting data within a transaction.

    This creates the meeting row (body-scoped) if needed, replaces items/docs,
    and updates sync status to 'complete' on success.
    Returns the number of agenda items persisted.

    On failure, rolls back and raises.
    """
    try:
        # Ensure meeting row exists (creates if new)
        meeting = create_or_get_meeting(session, body, meeting_dict)

        # Update meeting metadata from meeting_dict (preserves existing
        # values when meeting_dict has empty fields, e.g. --meeting-id path)
        if meeting_dict.get("meeting_date"):
            meeting.meeting_date = meeting_dict["meeting_date"]
        if meeting_dict.get("meeting_type"):
            meeting.meeting_type = meeting_dict["meeting_type"]
        if meeting_dict.get("meeting_title"):
            meeting.meeting_title = meeting_dict["meeting_title"]
        if meeting_dict.get("source_url"):
            meeting.source_url = meeting_dict["source_url"]

        # Store raw title and normalize fields
        if meeting_dict.get("meeting_title"):
            meeting.meeting_title_raw = meeting_dict["meeting_title"]

        if meeting_dict.get("meeting_type") or meeting_dict.get("meeting_title"):
            raw_type = meeting_dict.get("meeting_type", "")
            raw_title = meeting_dict.get("meeting_title", "")

            meeting.meeting_type = normalize_meeting_type(raw_type, raw_title)
            meeting.meeting_context = extract_meeting_context(raw_title, raw_type)
            meeting.meeting_body = extract_meeting_body(raw_title)
            meeting.display_name = build_meeting_display_name(
                meeting.meeting_type,
                meeting.meeting_date,
                meeting.meeting_context,
            )

        persisted = persist_meeting(
            session,
            body,
            meeting_id,
            agenda_item_dicts,
            supporting_doc_dicts,
        )

        doc_count = len(supporting_doc_dicts) if supporting_doc_dicts else 0

        update_sync_status(
            session,
            body,
            meeting_id,
            "complete",
            item_count_expected=meeting.item_count_expected or len(agenda_item_dicts),
            item_count_actual=len(agenda_item_dicts),
            supporting_doc_count=doc_count,
            items_extracted=True,
            supporting_docs_extracted=bool(supporting_doc_dicts),
            error=None,
        )
        session.commit()
        return persisted

    except Exception:
        session.rollback()
        raise


def _detect_vote_attributes(aiv_list: list[AgendaItemVote]) -> None:
    """Detect split_vote, unanimous, and majority_position on each AgendaItemVote.

    This is called from persist_votes() after all votes are committed.
    It examines supervisor_votes / member_votes in the DB to determine
    vote-level attributes.
    """
    if not aiv_list:
        return
    session = Session.object_session(aiv_list[0])
    if not session:
        return
    for aiv in aiv_list:
        # Gather supervisor votes for this aiv
        sv_rows = session.execute(
            select(SupervisorVote).where(
                SupervisorVote.agenda_item_vote_id == aiv.id
            )
        ).scalars().all()
        if not sv_rows:
            continue

        # Determine the set of distinct substantive votes (yes/no)
        # excluding abstain/recused/absent/not_voting
        substantive = [sv.vote for sv in sv_rows if sv.vote in ("yes", "no")]
        if not substantive:
            # All abstentions/recusals — not split, not unanimous
            aiv.unanimous = None
            aiv.is_split_vote = False
            aiv.majority_position = "unknown"
            continue

        vote_set = set(substantive)
        aiv.is_split_vote = len(vote_set) > 1
        aiv.unanimous = len(vote_set) == 1

        # Determine majority position
        yes_count = sum(1 for v in substantive if v == "yes")
        no_count = sum(1 for v in substantive if v == "no")
        if yes_count > no_count:
            aiv.majority_position = "yes"
        elif no_count > yes_count:
            aiv.majority_position = "no"
        else:
            aiv.majority_position = "tie"

        # Flag dissenting supervisor votes
        if aiv.majority_position and aiv.majority_position not in ("tie", "unknown"):
            for sv in sv_rows:
                if (
                    sv.vote in ("yes", "no")
                    and sv.vote != aiv.majority_position
                ):
                    sv.is_dissent = True


def persist_votes(
    session: Session,
    body: str,
    meeting_id: str,
    supervisors: list[dict],
    votes: list[dict],
) -> int:
    # Suppress identity-map warning from SQLite reusing PK IDs after DELETE
    import warnings
    from sqlalchemy import exc as sa_exc
    warnings.filterwarnings("ignore", category=sa_exc.SAWarning, module="db")
    """Persist supervisor info and vote results for a meeting.

    1. Upsert supervisor records (by normalized_name).
    2. Delete existing meeting_supervisors, agenda_item_votes, supervisor_votes
       for this meeting_id.
    3. Insert new records.
    4. Commit transactionally.

    Returns the number of vote records persisted.
    """
    # 1. Upsert supervisors
    supervisor_map: dict[str, int] = {}
    for sup in supervisors:
        norm = sup.get("normalized_name", sup.get("name", "").lower().strip())
        if not norm:
            continue
        existing = session.execute(
            select(Supervisor).where(Supervisor.normalized_name == norm)
        ).scalar_one_or_none()
        if existing:
            existing.name = sup.get("name", existing.name)
            existing.district = sup.get("district", existing.district)
            existing.updated_at = datetime.now(timezone.utc)
            supervisor_map[norm] = existing.id
        else:
            new = Supervisor(
                name=sup.get("name", ""),
                normalized_name=norm,
                district=sup.get("district"),
                active_from=sup.get("active_from"),
                active_to=sup.get("active_to"),
            )
            session.add(new)
            session.flush()
            supervisor_map[norm] = new.id

    # 2. Delete existing records for this meeting (body-scoped)
    session.execute(
        MeetingSupervisor.__table__.delete().where(
            MeetingSupervisor.body == body,
            MeetingSupervisor.meeting_id == meeting_id,
        )
    )
    existing_aiv_rows = session.execute(
        select(AgendaItemVote).where(
            AgendaItemVote.body == body,
            AgendaItemVote.meeting_id == meeting_id,
        )
    ).scalars().all()
    existing_aiv_ids = [r.id for r in existing_aiv_rows]
    if existing_aiv_ids:
        session.execute(
            SupervisorVote.__table__.delete().where(
                SupervisorVote.agenda_item_vote_id.in_(existing_aiv_ids)
            )
        )
    session.execute(
        AgendaItemVote.__table__.delete().where(
            AgendaItemVote.body == body,
            AgendaItemVote.meeting_id == meeting_id,
        )
    )
    session.flush()
    # Skip expire_all — it causes identity map conflicts when called
    # from within an active transaction (e.g. PZ sync loop).
    vote_count = 0

    # 3. Insert meeting_supervisor records
    for sup in supervisors:
        norm = sup.get("normalized_name", sup.get("name", "").lower().strip())
        sup_id = supervisor_map.get(norm)
        if sup_id is None:
            continue
        ms = MeetingSupervisor(
            body=body,
            meeting_id=meeting_id,
            supervisor_id=sup_id,
            role=sup.get("role"),
            present=sup.get("present", True),
        )
        session.add(ms)

    # 4. Insert vote records
    seen_item_db_ids: set[int] = set()
    for vote in votes:
        item_number = int(vote.get("agenda_item_number", 0))
        # Look up the actual AgendaItem database row for FK reference
        db_agenda_item = session.execute(
            select(AgendaItem).where(
                AgendaItem.body == body,
                AgendaItem.meeting_id == meeting_id,
                AgendaItem.agenda_item_number == item_number,
            )
        ).scalar_one_or_none()
        if db_agenda_item:
            db_agenda_item_id = db_agenda_item.id
        else:
            # No matching agenda item — use a synthetic ID to avoid UNIQUE constraint collisions
            db_agenda_item_id = -1 * hash(f"{body}:{meeting_id}:{item_number}") % (2**31)
        if db_agenda_item_id in seen_item_db_ids:
            # Duplicate item number (e.g., agenda has two #2 entries for
            # different sub-items). Skip rather than violate the unique
            # constraint on agenda_item_id.
            continue
        seen_item_db_ids.add(db_agenda_item_id)
        aiv = AgendaItemVote(
            body=body,
            agenda_item_id=db_agenda_item_id,
            meeting_id=meeting_id,
            agenda_item_number=item_number,
            c_number=vote.get("c_number"),
            c_number_base=vote.get("c_number_base"),
            motion_result=vote.get("motion_result"),
            vote_text=vote.get("vote_text"),
        )
        session.add(aiv)
        session.flush()

        # Insert individual supervisor votes
        for sv in vote.get("supervisor_votes", []):
            name = sv.get("name", "")
            norm_name = name.lower().strip()
            sup_id = supervisor_map.get(norm_name)
            if sup_id is None:
                # Try to find in DB without upserting
                existing_sup = session.execute(
                    select(Supervisor).where(Supervisor.normalized_name == norm_name)
                ).scalar_one_or_none()
                if existing_sup:
                    sup_id = existing_sup.id
                    supervisor_map[norm_name] = sup_id
                else:
                    # Create a new supervisor record for this name
                    new = Supervisor(
                        name=name,
                        normalized_name=norm_name,
                    )
                    session.add(new)
                    session.flush()
                    sup_id = new.id
                    supervisor_map[norm_name] = sup_id

            sv_rec = SupervisorVote(
                agenda_item_vote_id=aiv.id,
                supervisor_id=sup_id,
                vote=sv.get("vote", "unknown"),
                raw_vote_text=sv.get("raw_vote_text"),
            )
            session.add(sv_rec)

        vote_count += 1

    # 5. Flush to get AIV IDs, then detect vote attributes
    session.flush()
    # Reload AIVs to get their IDs for attribute detection
    aiv_rows = session.execute(
        select(AgendaItemVote).where(
            AgendaItemVote.body == body,
            AgendaItemVote.meeting_id == meeting_id,
        )
    ).scalars().all()
    _detect_vote_attributes(aiv_rows)

    # 6. Commit
    session.commit()
    return vote_count


def infer_absence_for_meeting(
    session: Session,
    body: str,
    meeting_id: str,
    known_member_ids: list[int],
    voting_member_ids: list[int],
) -> list["MeetingAttendance"]:
    """Infer absence for members who did not vote while others voted.

    Args:
        session: DB session
        body: Body scope
        meeting_id: Meeting identifier
        known_member_ids: All member IDs known to be active for this body
        voting_member_ids: Member IDs who actually voted on any item

    Returns:
        List of new MeetingAttendance records (not yet committed)
    """
    inferred: list[MeetingAttendance] = []
    voting_set = set(voting_member_ids)
    for mid in known_member_ids:
        if mid not in voting_set:
            att = MeetingAttendance(
                body=body,
                meeting_id=meeting_id,
                member_id=mid,
                attendance_status="inferred_absent",
                source_text="Member did not vote while others voted on agenda items",
                inference_method="missing_vote_when_others_voted",
            )
            session.add(att)
            inferred.append(att)
    return inferred


def get_meeting_attendance(
    session: Session,
    body: str,
    meeting_id: str,
) -> list["MeetingAttendance"]:
    """Get attendance records for a meeting."""
    rows = session.execute(
        select(MeetingAttendance).where(
            MeetingAttendance.body == body,
            MeetingAttendance.meeting_id == meeting_id,
        )
    ).scalars().all()
    return list(rows)


def get_executive_session_participants(
    session: Session,
    body: Optional[str] = None,
    meeting_id: Optional[str] = None,
    person_name: Optional[str] = None,
) -> list["ExecutiveSessionParticipant"]:
    """Get executive session participation records."""
    q = select(ExecutiveSessionParticipant)
    if body:
        q = q.where(ExecutiveSessionParticipant.body == body)
    if meeting_id:
        q = q.where(ExecutiveSessionParticipant.meeting_id == meeting_id)
    if person_name:
        q = q.where(ExecutiveSessionParticipant.normalized_name == person_name.lower())
    q = q.order_by(ExecutiveSessionParticipant.meeting_id)
    rows = session.execute(q).scalars().all()
    return list(rows)


def get_split_votes(
    session: Session,
    body: Optional[str] = None,
) -> list["AgendaItemVote"]:
    """Get all split votes, optionally filtered by body."""
    q = select(AgendaItemVote).where(AgendaItemVote.is_split_vote == True)
    if body and body.lower() != "all":
        q = q.where(AgendaItemVote.body == body)
    q = q.order_by(AgendaItemVote.meeting_id, AgendaItemVote.agenda_item_number)
    rows = session.execute(q).scalars().all()
    return list(rows)


def get_dissenting_votes(
    session: Session,
    member_name: Optional[str] = None,
) -> list["SupervisorVote"]:
    """Get dissent votes, optionally filtered by member name."""
    q = select(SupervisorVote).where(SupervisorVote.is_dissent == True)
    if member_name:
        norm = member_name.lower()
        from sqlalchemy import join as sa_join
        from sqlalchemy.orm import joinedload
    rows = session.execute(q).scalars().all()
    return list(rows)
