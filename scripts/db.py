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
        if url.startswith("sqlite"):
            _set_sqlite_pragmas(_engine)
    return _engine


def _set_sqlite_pragmas(engine):
    """Apply performance-oriented PRAGMAs to a SQLite connection."""
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
        cursor.execute("PRAGMA cache_size=-20000;")  # 20 MB cache
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


def set_database_url(url: str):
    """
    Switch the database URL at runtime.

    Used ONLY by test fixtures to point the engine at a temporary database.
    Disposes any existing engine and resets the session factory so that
    the next get_engine() / get_session() call creates fresh connections
    to the new URL.

    .. warning::
       Do NOT call this in production.  Set DATABASE_URL via the
       environment variable before the first import of this module.
    """
    global DATABASE_URL, _engine, _SessionLocal
    if _engine:
        _engine.dispose()
    DATABASE_URL = url
    _engine = None
    _SessionLocal = None


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


class PermitReport(Base):
    """A single weekly permit activity report (one XLSX file)."""

    __tablename__ = "permit_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(String(16), nullable=False, index=True)
    adid = Column(String(16), nullable=False, unique=True, index=True)
    report_title = Column(String(256), nullable=False, default="")
    file_type = Column(String(16), nullable=True, default=None)
    file_name = Column(String(256), nullable=True, default=None)
    source_url = Column(String(512), nullable=False, default="")
    local_path = Column(String(512), nullable=True, default=None)
    content_hash = Column(String(64), nullable=True, default=None)
    downloaded_at = Column(
        DateTime(timezone=True), nullable=True, default=None
    )
    row_count = Column(Integer, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Permit(Base):
    """Individual permit row extracted from a weekly permit report."""

    __tablename__ = "permits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(String(16), nullable=False, index=True)
    report_adid = Column(String(16), nullable=False, index=True)
    source_file = Column(String(256), nullable=True, default=None)

    # Core permit fields (all nullable since column positions vary by report)
    permit_type = Column(Text, nullable=True, default=None)
    work_class = Column(Text, nullable=True, default=None)
    permit_number = Column(String(64), nullable=True, default=None, index=True)
    permit_issue_date = Column(String(32), nullable=True, default=None)
    permit_description = Column(Text, nullable=True, default=None)
    permit_valuation = Column(
        String(32), nullable=True, default=None,
    )
    permit_square_feet = Column(
        String(32), nullable=True, default=None,
    )
    parcel_no = Column(String(32), nullable=True, default=None)
    no_units = Column(String(16), nullable=True, default=None)
    job_address = Column(Text, nullable=True, default=None)
    subdivision = Column(Text, nullable=True, default=None)
    lot = Column(String(32), nullable=True, default=None)
    job_city = Column(String(128), nullable=True, default=None)
    job_state = Column(String(16), nullable=True, default=None)
    job_zip = Column(String(16), nullable=True, default=None)
    owner_name = Column(Text, nullable=True, default=None)
    contractor_name = Column(Text, nullable=True, default=None)
    contractor_phone = Column(String(64), nullable=True, default=None)
    contractor_email = Column(String(256), nullable=True, default=None)

    row_hash = Column(String(64), nullable=False, index=True)
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
        # Permit numbers may repeat across different weekly reports (e.g.
        # fiscal-year reset), so the uniqueness is scoped to each report.
        UniqueConstraint("report_adid", "permit_number", name="uq_permit_per_report"),
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

    # Create additional indexes for common query patterns
    _ensure_index(engine, "meetings", "idx_meetings_date_desc", "meeting_date DESC")
    _ensure_index(engine, "meetings", "idx_meetings_meeting_type", "meeting_type")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_meeting_id", "meeting_id")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_c_number", "c_number")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_c_number_base", "c_number_base")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_agenda_item_number", "agenda_item_number")

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


def _ensure_index(engine, table: str, index_name: str, column_expr: str):
    """Create an index if it doesn't already exist."""
    inspector = sa_inspect(engine)
    existing = {ix["name"] for ix in inspector.get_indexes(table)}
    if index_name not in existing:
        with engine.connect() as conn:
            conn.execute(
                text(f'CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column_expr})')
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
        with session.no_autoflush:
            existing = session.execute(
                select(Supervisor).where(Supervisor.normalized_name == norm)
            ).scalar_one_or_none()
        if existing:
            existing.name = sup.get("name", existing.name)
            # Only update district when the new value is not None — a
            # subsequent meeting with a truncated or unparseable summary
            # should not erase a previously captured district.
            new_district = sup.get("district")
            if new_district is not None:
                existing.district = new_district
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
    # Use no_autoflush to prevent stale pending objects from a previous
    # failed call from being re-inserted by query-invoked autoflush, which
    # would collide with the fresh inserts below.
    with session.no_autoflush:
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
        with session.no_autoflush:
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
                with session.no_autoflush:
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

    # 6. Infer absences and abstentions from missing votes
    #    After all explicit votes are stored, check each supervisor present at
    #    this meeting against the total number of AIVs.
    #    -  0 votes for this meeting  → supervisor was absent (update MeetingSupervisor.present)
    #    -  >0 but < total AIVs       → supervisor abstained on the missing items
    #
    #    Only infer on items where a vote was actually taken.  Skip:
    #    -  Items with motion_result="withdrawn" (no vote was taken)
    #    -  Items whose vote_text lacks "Ayes:" or "Nay:" (informational
    #       presentations, proclamations, etc. where no roll call occurred)
    if aiv_rows:
        votable_aivs = [
            aiv for aiv in aiv_rows
            if aiv.motion_result != "withdrawn"
            and aiv.vote_text
            and ("Ayes:" in aiv.vote_text or "Nay:" in aiv.vote_text)
        ]
        aiv_ids = [aiv.id for aiv in votable_aivs]
        aiv_count = len(aiv_ids)
        # Load all MeetingSupervisor rows for this meeting
        ms_rows = session.execute(
            select(MeetingSupervisor).where(
                MeetingSupervisor.body == body,
                MeetingSupervisor.meeting_id == meeting_id,
            )
        ).scalars().all()
        for ms in ms_rows:
            sv_count = session.execute(
                select(func.count()).select_from(SupervisorVote).where(
                    SupervisorVote.supervisor_id == ms.supervisor_id,
                    SupervisorVote.agenda_item_vote_id.in_(aiv_ids),
                )
            ).scalar()
            if sv_count == 0:
                # Supervisor was listed as present but never voted — mark absent
                ms.present = False
            elif sv_count < aiv_count:
                # Supervisor voted on some items but not all — abstain on the rest
                existing_aiv_ids = set(
                    row[0] for row in session.execute(
                        select(SupervisorVote.agenda_item_vote_id).where(
                            SupervisorVote.supervisor_id == ms.supervisor_id,
                            SupervisorVote.agenda_item_vote_id.in_(aiv_ids),
                        )
                    ).all()
                )
                for aiv_id in aiv_ids:
                    if aiv_id not in existing_aiv_ids:
                        session.add(SupervisorVote(
                            agenda_item_vote_id=aiv_id,
                            supervisor_id=ms.supervisor_id,
                            vote="abstain",
                            raw_vote_text="inferred abstention — no vote recorded on this item",
                        ))
                        vote_count += 1

    # 7. Commit
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
    from sqlalchemy import join as sa_join
    q = select(SupervisorVote).where(SupervisorVote.is_dissent == True)
    if member_name:
        norm = member_name.lower().strip()
        q = (
            select(SupervisorVote)
            .join(Supervisor, SupervisorVote.supervisor_id == Supervisor.id)
            .where(
                SupervisorVote.is_dissent == True,
                Supervisor.normalized_name.ilike(f"%{norm}%"),
            )
        )
    rows = session.execute(q).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# BOS Member / Supervisor Voting Portal — Query Helpers
# ---------------------------------------------------------------------------
# These use "supervisor" naming for BOS.  The same patterns can be adapted
# for other bodies via the agenda_item_votes.body / meeting_supervisors.body
# filter without schema changes.
# ---------------------------------------------------------------------------


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


def get_supervisor_by_slug_or_name(
    session: Session,
    slug_or_name: str,
) -> Optional[Supervisor]:
    """Look up a supervisor by URL slug or name.

    Slugs use the normalized_name field with hyphens instead of spaces.
    Falls back to partial name matching for flexibility.
    """
    from sqlalchemy import or_

    # Try slug: hyphen → space, match normalized_name exactly
    maybe_norm = slug_or_name.replace("-", " ").strip()
    sup = session.execute(
        select(Supervisor).where(Supervisor.normalized_name == maybe_norm)
    ).scalar_one_or_none()
    if sup:
        return sup

    # Try name match with noise filtering (shortest = most canonical)
    matches = session.execute(
        select(Supervisor)
        .where(
            or_(
                Supervisor.name.ilike(f"%{maybe_norm}%"),
                Supervisor.normalized_name.ilike(f"%{maybe_norm}%"),
            )
        )
        .order_by(func.length(Supervisor.name))
    ).scalars().all()
    for candidate in matches:
        if len(candidate.name) < 40 and not re.search(r"\d", candidate.name):
            return candidate

    return None


def get_bos_supervisors(session: Session) -> list[Supervisor]:
    """Get BOS supervisors with meaningful attendance (≥5 meeting appearances).

    Uses meeting_supervisors (body='bos') to identify actual supervisors
    versus noise rows from vote-parsing artifacts.
    """
    rows = session.execute(
        select(Supervisor, func.count(MeetingSupervisor.id).label("attendance"))
        .join(
            MeetingSupervisor,
            MeetingSupervisor.supervisor_id == Supervisor.id,
        )
        .where(MeetingSupervisor.body == "bos")
        .group_by(Supervisor.id)
        .having(func.count(MeetingSupervisor.id) >= 5)
        .order_by(Supervisor.name)
    ).all()
    # Also filter out obvious noise (long names, digits)
    result = []
    for sup, _att in rows:
        if len(sup.name) < 40 and not re.search(r"\d", sup.name):
            result.append(sup)
    return result


def get_supervisor_vote_stats(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> dict:
    """Get aggregated voting statistics for a supervisor.

    Returns a dict with:
        total_votes, yes, no, abstain, absences,
        split_votes_attended, with_majority, against_majority,
        attendance_rate, attendance_present, attendance_absent
    """
    from collections import Counter

    # --- 1. Raw vote counts (filtered by body scope on AgendaItemVote) ---
    rows = session.execute(
        select(SupervisorVote.vote, AgendaItemVote.id.label("aiv_id"))
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    total_votes = len(rows)
    norm_counts: Counter = Counter()
    aiv_ids: set[int] = set()
    for row in rows:
        norm_counts[_normalize_vote_value(row.vote)] += 1
        aiv_ids.add(row.aiv_id)

    yes_count = norm_counts.get("yes", 0)
    no_count = norm_counts.get("no", 0)
    abstain_count = norm_counts.get("abstain", 0)

    # --- 2. Vote-level attributes (split, majority) from other members ---
    # Fetch all votes on the same AIVs from ALL supervisors
    aiv_id_list = list(aiv_ids)
    if aiv_id_list:
        all_votes = session.execute(
            select(
                SupervisorVote.agenda_item_vote_id,
                SupervisorVote.supervisor_id,
                SupervisorVote.vote,
            )
            .where(SupervisorVote.agenda_item_vote_id.in_(aiv_id_list))
        ).all()
    else:
        all_votes = []

    # Build per-AIV data
    aiv_votes: dict[int, dict[str, list[int]]] = {}
    for av in all_votes:
        aiv_votes.setdefault(av.agenda_item_vote_id, {"yes": [], "no": []})
        nv = _normalize_vote_value(av.vote)
        if nv == "yes":
            aiv_votes[av.agenda_item_vote_id]["yes"].append(av.supervisor_id)
        elif nv == "no":
            aiv_votes[av.agenda_item_vote_id]["no"].append(av.supervisor_id)

    split_count = 0
    with_maj = 0
    against_maj = 0

    for aiv_id, vd in aiv_votes.items():
        yes_sup = len(vd["yes"])
        no_sup = len(vd["no"])
        is_split = yes_sup > 0 and no_sup > 0
        if not is_split:
            continue
        split_count += 1
        # Determine majority
        if yes_sup > no_sup:
            majority = "yes"
        elif no_sup > yes_sup:
            majority = "no"
        else:
            majority = "tie"
        # Check where this supervisor voted
        # Re-find the supervisor's vote for this AIV
        sup_nv = None
        for av in all_votes:
            if av.agenda_item_vote_id == aiv_id and av.supervisor_id == sup_id:
                sup_nv = _normalize_vote_value(av.vote)
                break
        if sup_nv is None:
            continue
        if majority == "tie":
            continue
        if sup_nv == majority:
            with_maj += 1
        elif sup_nv in ("yes", "no"):
            against_maj += 1

    # --- 3. Attendance ---
    present = session.execute(
        select(func.count())
        .select_from(MeetingSupervisor)
        .where(
            MeetingSupervisor.supervisor_id == sup_id,
            MeetingSupervisor.body == body,
            MeetingSupervisor.present == True,
        )
    ).scalar() or 0

    absent = session.execute(
        select(func.count())
        .select_from(MeetingSupervisor)
        .where(
            MeetingSupervisor.supervisor_id == sup_id,
            MeetingSupervisor.body == body,
            MeetingSupervisor.present == False,
        )
    ).scalar() or 0

    total_meetings = present + absent
    attendance_rate = round(present / total_meetings, 4) if total_meetings > 0 else None

    return {
        "total_votes": total_votes,
        "yes": yes_count,
        "no": no_count,
        "abstain": abstain_count,
        "absences": absent,
        "attendance_present": present,
        "attendance_absent": absent,
        "attendance_rate": attendance_rate,
        "split_votes_attended": split_count,
        "with_majority": with_maj,
        "against_majority": against_maj,
        "dissent_rate": round(against_maj / split_count, 4) if split_count > 0 else None,
    }


def get_supervisor_split_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get all split votes involving this supervisor.

    Returns list of dicts with:
        meeting_id, meeting_date, meeting_type, agenda_item_number,
        agenda_item_title, c_number, supervisor_vote,
        motion_result, majority_position, with_or_against_majority
    """
    # 1. Get all AIV IDs this supervisor voted on (body-scoped)
    my_aiv_rows = session.execute(
        select(SupervisorVote.agenda_item_vote_id, SupervisorVote.vote)
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    if not my_aiv_rows:
        return []

    sup_vote_map = {av.agenda_item_vote_id: _normalize_vote_value(av.vote) for av in my_aiv_rows}
    aiv_ids = list(sup_vote_map.keys())

    # 2. Get all votes on these AIVs (all supervisors)
    all_votes = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    # Group by aiv
    aiv_group: dict[int, list[str]] = {}
    for r in all_votes:
        aiv_group.setdefault(r.agenda_item_vote_id, []).append(
            _normalize_vote_value(r.vote)
        )

    # Identify split AIVs: both yes and no votes present
    split_aiv_ids: set[int] = set()
    aiv_majority: dict[int, Optional[str]] = {}
    for aiv_id, votes in aiv_group.items():
        vote_set = set(votes)
        if "yes" in vote_set and "no" in vote_set:
            split_aiv_ids.add(aiv_id)
            yes_cnt = sum(1 for v in votes if v == "yes")
            no_cnt = sum(1 for v in votes if v == "no")
            if yes_cnt > no_cnt:
                aiv_majority[aiv_id] = "yes"
            elif no_cnt > yes_cnt:
                aiv_majority[aiv_id] = "no"
            else:
                aiv_majority[aiv_id] = "tie"

    if not split_aiv_ids:
        return []

    # 3. Fetch AIV details + meeting + agenda item info for split AIVs (raw SQL to avoid ORM ambiguity)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in split_aiv_ids)
    rows = session.execute(
        sa_text(f"""
            SELECT
                aiv.meeting_id,
                aiv.agenda_item_number,
                aiv.c_number,
                aiv.motion_result,
                ai.agenda_item_title,
                m.meeting_date,
                m.meeting_type
            FROM agenda_item_votes aiv
            LEFT JOIN agenda_items ai
                ON ai.meeting_id = aiv.meeting_id
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in rows:
        aiv_key = None
        # Need to find the AIV id — re-query to get id from meeting_id+item_number
        aiv = session.execute(
            select(AgendaItemVote.id)
            .where(
                AgendaItemVote.body == body,
                AgendaItemVote.meeting_id == r.meeting_id,
                AgendaItemVote.agenda_item_number == r.agenda_item_number,
            )
        ).scalar_one_or_none()
        if aiv is None:
            continue
        aiv_id = aiv
        sup_nv = sup_vote_map.get(aiv_id, "unknown")
        maj = aiv_majority.get(aiv_id)
        with_maj = None
        if maj and sup_nv in ("yes", "no") and maj not in ("tie", "unknown"):
            with_maj = "with_majority" if sup_nv == maj else "against_majority"

        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "majority_position": maj,
            "with_or_against_majority": with_maj,
        })

    return results


def get_supervisor_dissents(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get split votes where this supervisor voted against the majority.

    Returns the same dict format as get_supervisor_split_votes, but only
    items where with_or_against_majority == 'against_majority'.
    """
    all_split = get_supervisor_split_votes(session, sup_id, body)
    return [s for s in all_split if s.get("with_or_against_majority") == "against_majority"]


def get_supervisor_abstentions(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get votes where this supervisor abstained."""
    from sqlalchemy import text as sa_text

    # Use raw SQL to avoid ORM join-ambiguity issues with the 4-table chain
    sql = sa_text("""
        SELECT
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            m.meeting_date,
            m.meeting_type
        FROM supervisor_votes sv
        JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_id = aiv.meeting_id
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
        WHERE sv.supervisor_id = :sup_id
          AND aiv.body = :body
          AND sv.vote IN ('abstain', 'abstained')
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    rows = session.execute(sql, {"sup_id": sup_id, "body": body}).all()

    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "motion_result": r.motion_result or "",
        }
        for r in rows
    ]


def get_supervisor_absences(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get meetings where this supervisor was marked absent.

    Returns list of dicts with meeting_id, meeting_date, meeting_type, title.
    """
    rows = session.execute(
        select(
            Meeting.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            Meeting.display_name,
            Meeting.meeting_title,
        )
        .join(
            MeetingSupervisor,
            (MeetingSupervisor.meeting_id == Meeting.meeting_id)
            & (MeetingSupervisor.body == Meeting.body),
        )
        .where(
            MeetingSupervisor.supervisor_id == sup_id,
            MeetingSupervisor.present == False,
            Meeting.body == body,
        )
        .order_by(Meeting.meeting_date)
    ).all()

    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "title": r.display_name or r.meeting_title or r.meeting_id,
        }
        for r in rows
    ]


def get_supervisor_full_voting_record(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get chronological list of all recorded votes for this supervisor.

    Returns list of dicts with:
        meeting_id, meeting_date, meeting_type, agenda_item_number,
        agenda_item_title, c_number, vote (normalized), motion_result,
        is_split_vote, majority_position, with_or_against_majority
    """
    from sqlalchemy import text as sa_text

    # Use raw SQL to avoid ORM join-ambiguity issues with the 4-table chain
    sql = sa_text("""
        SELECT
            sv.vote,
            sv.raw_vote_text,
            sv.agenda_item_vote_id,
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            m.meeting_date,
            m.meeting_type
        FROM supervisor_votes sv
        JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_id = aiv.meeting_id
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
        WHERE sv.supervisor_id = :sup_id
          AND aiv.body = :body
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    sup_votes = session.execute(sql, {"sup_id": sup_id, "body": body}).all()

    if not sup_votes:
        return []

    # Gather all AIV IDs for split/majority detection
    aiv_ids = [r.agenda_item_vote_id for r in sup_votes]

    # Get all votes on these AIVs (for split/majority detection)
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    aiv_group: dict[int, list[str]] = {}
    for r in all_v:
        aiv_group.setdefault(r.agenda_item_vote_id, []).append(
            _normalize_vote_value(r.vote)
        )

    is_split: dict[int, bool] = {}
    majority: dict[int, Optional[str]] = {}
    for aiv_id, votes in aiv_group.items():
        vote_set = set(votes)
        has_yes = "yes" in vote_set
        has_no = "no" in vote_set
        is_split[aiv_id] = has_yes and has_no
        if has_yes and has_no:
            yes_cnt = sum(1 for v in votes if v == "yes")
            no_cnt = sum(1 for v in votes if v == "no")
            if yes_cnt > no_cnt:
                majority[aiv_id] = "yes"
            elif no_cnt > yes_cnt:
                majority[aiv_id] = "no"
            else:
                majority[aiv_id] = "tie"
        else:
            majority[aiv_id] = None

    results = []
    for r in sup_votes:
        aiv_id = r.agenda_item_vote_id
        sup_nv = _normalize_vote_value(r.vote)
        maj = majority.get(aiv_id)
        split_flag = is_split.get(aiv_id, False)
        with_maj = None
        if maj and split_flag and sup_nv in ("yes", "no") and maj not in ("tie", "unknown"):
            with_maj = "with_majority" if sup_nv == maj else "against_majority"

        is_inferred = bool(r.raw_vote_text and r.raw_vote_text.startswith("inferred"))
        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "vote": sup_nv,
            "is_inferred": is_inferred,
            "motion_result": r.motion_result or "",
            "is_split_vote": split_flag,
            "majority_position": maj,
            "with_or_against_majority": with_maj,
        })

    return results


def get_supervisor_slug(sup: Supervisor) -> str:
    """Get URL slug for a supervisor."""
    return sup.normalized_name.replace(" ", "-")


# ---------------------------------------------------------------------------
# Phase 2 — Voting Analytics Helpers
# ---------------------------------------------------------------------------


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


def get_supervisor_majority_alignment_stats(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> dict:
    """Get detailed majority alignment stats for a supervisor.

    Extends get_supervisor_vote_stats with additional analytics fields.
    Returns breakdown of unanimous vs split-vote behavior.
    """
    from collections import Counter

    # Get raw vote data
    rows = session.execute(
        select(SupervisorVote.vote, AgendaItemVote.id.label("aiv_id"))
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    total_votes = len(rows)
    if total_votes == 0:
        return {
            "total_votes": 0, "unanimous_votes": 0,
            "split_votes_attended": 0, "with_majority": 0,
            "against_majority": 0, "abstain_on_split": 0,
            "majority_alignment_rate": None, "dissent_rate": None,
        }

    aiv_ids = {r.aiv_id for r in rows}
    aiv_id_list = list(aiv_ids)

    # Get all votes on all relevant AIVs (for split/majority detection)
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.supervisor_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_id_list))
    ).all()

    # Build per-AIV data
    aiv_data: dict[int, dict] = {}
    for av in all_v:
        aiv_data.setdefault(av.agenda_item_vote_id, {
            "votes": [],  # all normalized
            "sup_votes": {},  # sup_id -> norm_vote
        })
        nv = _normalize_vote_value(av.vote)
        aiv_data[av.agenda_item_vote_id]["votes"].append(nv)
        aiv_data[av.agenda_item_vote_id]["sup_votes"][av.supervisor_id] = nv

    # Build sup vote map: aiv_id -> norm_vote for this specific supervisor
    sup_own_votes = {}
    for r in rows:
        sup_own_votes[r.aiv_id] = _normalize_vote_value(r.vote)

    unanimous = 0
    split_attended = 0
    with_maj = 0
    against_maj = 0
    abstain_on_split = 0

    for aiv_id in aiv_ids:
        dd = aiv_data.get(aiv_id, {})
        vlist = dd.get("votes", [])
        yes_cnt = sum(1 for v in vlist if v == "yes")
        no_cnt = sum(1 for v in vlist if v == "no")
        is_split = yes_cnt > 0 and no_cnt > 0

        if not is_split:
            unanimous += 1
            continue

        split_attended += 1
        sup_nv = sup_own_votes.get(aiv_id, "unknown")

        if sup_nv == "abstain":
            abstain_on_split += 1
            continue
        if sup_nv not in ("yes", "no"):
            continue

        if yes_cnt > no_cnt:
            maj = "yes"
        elif no_cnt > yes_cnt:
            maj = "no"
        else:
            maj = "tie"

        if maj == "tie":
            continue
        if sup_nv == maj:
            with_maj += 1
        else:
            against_maj += 1

    majority_alignment_rate = (
        round(with_maj / split_attended, 4) if split_attended > 0 else None
    )
    dissent_rate = (
        round(against_maj / split_attended, 4) if split_attended > 0 else None
    )

    return {
        "total_votes": total_votes,
        "unanimous_votes": unanimous,
        "split_votes_attended": split_attended,
        "with_majority": with_maj,
        "against_majority": against_maj,
        "abstain_on_split": abstain_on_split,
        "majority_alignment_rate": majority_alignment_rate,
        "dissent_rate": dissent_rate,
    }


def get_supervisor_voting_alignment(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Compare voting patterns between this supervisor and all others.

    For each other BOS supervisor, compute:
    - total comparable votes (both voted yes or no)
    - same votes
    - different votes
    - overall alignment percentage
    - split-vote alignment percentage

    Excludes abstentions and absences from pairwise alignment.
    """
    # Get all BOS supervisor IDs
    sup_ids = [
        r[0]
        for r in session.execute(
            select(Supervisor.id)
            .join(
                MeetingSupervisor,
                MeetingSupervisor.supervisor_id == Supervisor.id,
            )
            .where(
                MeetingSupervisor.body == body,
            )
            .group_by(Supervisor.id)
            .having(func.count(MeetingSupervisor.id) >= 5)
            .order_by(Supervisor.name)
        ).all()
    ]

    if sup_id not in sup_ids:
        return []

    other_ids = [sid for sid in sup_ids if sid != sup_id]
    if not other_ids:
        return []

    # Get all votes for this supervisor (body-scoped)
    sup_aiv_votes: dict[int, str] = {}
    for r in session.execute(
        select(SupervisorVote.agenda_item_vote_id, SupervisorVote.vote)
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            sup_aiv_votes[r.agenda_item_vote_id] = nv

    if not sup_aiv_votes:
        return []

    aiv_ids = list(sup_aiv_votes.keys())

    # Get all other supervisors' votes on the same AIVs
    # other_votes[other_sup_id][aiv_id] = norm_vote
    other_votes: dict[int, dict[int, str]] = {oid: {} for oid in other_ids}
    for r in session.execute(
        select(
            SupervisorVote.supervisor_id,
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(
            SupervisorVote.supervisor_id.in_(other_ids),
            SupervisorVote.agenda_item_vote_id.in_(aiv_ids),
        )
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            other_votes[r.supervisor_id][r.agenda_item_vote_id] = nv

    # Also determine which AIVs are split votes
    # For each AIV, collect all normalized votes to detect split
    all_votes_on_aiv: dict[int, list[str]] = {}
    for r in session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            all_votes_on_aiv.setdefault(r.agenda_item_vote_id, []).append(nv)

    split_aiv_ids: set[int] = set()
    for aiv_id, vl in all_votes_on_aiv.items():
        if "yes" in vl and "no" in vl:
            split_aiv_ids.add(aiv_id)

    # Get other supervisor names
    sup_names: dict[int, str] = {
        r.id: r.name
        for r in session.execute(select(Supervisor)).scalars().all()
    }

    results = []
    for oid in other_ids:
        other_name = sup_names.get(oid, f"Supervisor #{oid}")
        other_o = get_supervisor_by_slug_or_name(session, other_name)
        slug = get_supervisor_slug(other_o) if other_o else ""

        # Compare votes
        comparable_aivs = set(sup_aiv_votes.keys()) & set(other_votes[oid].keys())
        if not comparable_aivs:
            continue

        same = 0
        diff = 0
        split_same = 0
        split_diff = 0

        for aiv_id in comparable_aivs:
            sv = sup_aiv_votes[aiv_id]
            ov = other_votes[oid][aiv_id]
            if sv == ov:
                same += 1
                if aiv_id in split_aiv_ids:
                    split_same += 1
            else:
                diff += 1
                if aiv_id in split_aiv_ids:
                    split_diff += 1

        total = same + diff
        split_total = split_same + split_diff
        overall_pct = round(same / total * 100, 1) if total else None
        split_pct = round(split_same / split_total * 100, 1) if split_total else None

        results.append({
            "other_supervisor_id": oid,
            "other_name": other_name,
            "slug": slug,
            "total_comparable_votes": total,
            "same_votes": same,
            "different_votes": diff,
            "overall_alignment_pct": overall_pct,
            "split_vote_comparable": split_total,
            "split_vote_same": split_same,
            "split_vote_alignment_pct": split_pct,
        })

    return results


# ---------------------------------------------------------------------------
# Controversy Detection
# ---------------------------------------------------------------------------

_CONTROVERSY_KEYWORDS = {
    "protest", "opposition", "appeal", "litigation", "lawsuit",
    "zoning", "rezoning", "variance", "special use",
    "settlement", "claim", "procurement", "contract",
    "sole source", "emergency", "tax", "election",
}

_DOLLAR_PATTERN = re.compile(r"\$[\d,]+(?:,\d{3})*(?:\.\d{2})?")


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


def get_supervisor_swing_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Identify swing votes for a BOS supervisor.

    Swing vote = split vote where motion passed/failed by one vote
    and this supervisor voted with the prevailing side.
    For BOS (5 members), one-vote margin = 3-2 or 2-3.

    Returns list of dicts with meeting/agenda/vote detail.
    """
    # Get this supervisor's votes on split AIVs
    sup_votes = session.execute(
        select(
            SupervisorVote.vote,
            SupervisorVote.agenda_item_vote_id,
        )
        .join(
            AgendaItemVote,
            AgendaItemVote.id == SupervisorVote.agenda_item_vote_id,
        )
        .where(
            SupervisorVote.supervisor_id == sup_id,
            AgendaItemVote.body == body,
        )
    ).all()

    if not sup_votes:
        return []

    aiv_ids = [r.agenda_item_vote_id for r in sup_votes]

    # Get all votes on these AIVs
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.supervisor_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    # Build tally per AIV and find this sup's vote
    from collections import Counter
    aiv_tallies: dict[int, Counter] = {}
    sup_aiv_nv: dict[int, str] = {}
    for r in all_v:
        aiv_tallies.setdefault(r.agenda_item_vote_id, Counter())
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            aiv_tallies[r.agenda_item_vote_id][nv] += 1
        if r.supervisor_id == sup_id:
            sup_aiv_nv[r.agenda_item_vote_id] = nv

    # Identify swing AIVs: split vote, margin=1, sup with prevailing side
    swing_aiv_ids: set[int] = set()
    for aiv_id, tally in aiv_tallies.items():
        yes = tally.get("yes", 0)
        no = tally.get("no", 0)
        is_split = yes > 0 and no > 0
        if not is_split:
            continue
        margin = abs(yes - no)
        if margin != 1:
            continue
        # Now check: was this supervisor's vote with the prevailing side?
        sup_nv = sup_aiv_nv.get(aiv_id)
        if sup_nv is None:
            continue
        if sup_nv not in ("yes", "no"):
            continue
        prev_side = "yes" if yes > no else "no"
        if sup_nv == prev_side:
            swing_aiv_ids.add(aiv_id)

    if not swing_aiv_ids:
        return []

    # Fetch meeting/agenda details
    ids_list = list(swing_aiv_ids)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in ids_list)
    rows = session.execute(
        sa_text(f"""
            SELECT
                aiv.id AS aiv_id,
                aiv.meeting_id,
                aiv.agenda_item_number,
                aiv.c_number,
                aiv.motion_result,
                ai.agenda_item_title,
                m.meeting_date,
                m.meeting_type
            FROM agenda_item_votes aiv
            LEFT JOIN agenda_items ai
                ON ai.meeting_id = aiv.meeting_id
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in rows:
        tally = aiv_tallies.get(r.aiv_id, Counter())
        sup_nv = sup_aiv_nv.get(r.aiv_id, "unknown")
        prev_side = "yes" if tally.get("yes", 0) > tally.get("no", 0) else "no"
        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "vote_tally": f"{tally.get('yes',0)}-{tally.get('no',0)}",
            "prevailing_side": prev_side,
        })

    return results


def get_supervisor_controversial_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get controversial votes involving this supervisor.

    Flags items as controversial using detect_controversy_flags heuristics.
    Returns list of dicts with meeting/agenda detail and reason flags.
    """
    from sqlalchemy import text as sa_text
    from collections import defaultdict

    # Get this supervisor's votes with item text
    sql = sa_text("""
        SELECT
            sv.vote,
            sv.agenda_item_vote_id AS aiv_id,
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            COALESCE(ai.agenda_item_text, '') AS agenda_item_text,
            m.meeting_date,
            m.meeting_type
        FROM supervisor_votes sv
        JOIN agenda_item_votes aiv ON aiv.id = sv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_id = aiv.meeting_id
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.meeting_id = aiv.meeting_id
        WHERE sv.supervisor_id = :sup_id
          AND aiv.body = :body
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    rows = session.execute(sql, {"sup_id": sup_id, "body": body}).all()

    if not rows:
        return []

    # Gather all AIV IDs to detect split votes and abstentions
    aiv_ids = [r.aiv_id for r in rows]

    # Get all votes on these AIVs
    all_v = session.execute(
        select(
            SupervisorVote.agenda_item_vote_id,
            SupervisorVote.vote,
        )
        .where(SupervisorVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    aiv_has_abstention: set[int] = set()
    aiv_is_split: set[int] = set()
    aiv_votes: dict[int, list] = defaultdict(list)
    for r in all_v:
        aiv_votes[r.agenda_item_vote_id].append(r.vote)

    for aiv_id, vlist in aiv_votes.items():
        norm = [_normalize_vote_value(v) for v in vlist]
        if "abstain" in norm:
            aiv_has_abstention.add(aiv_id)
        yes = sum(1 for v in norm if v == "yes")
        no = sum(1 for v in norm if v == "no")
        if yes > 0 and no > 0:
            aiv_is_split.add(aiv_id)

    results = []
    for r in rows:
        aiv_id = r.aiv_id
        is_split = aiv_id in aiv_is_split
        has_abst = aiv_id in aiv_has_abstention

        flags = detect_controversy_flags(
            item_title=r.agenda_item_title or "",
            item_text=r.agenda_item_text or "",
            is_split_vote=is_split,
            motion_result=r.motion_result or "",
            has_abstention=has_abst,
        )

        if not flags:
            continue

        sv = r.vote or ""
        sup_nv = _normalize_vote_value(sv) if sv else "unknown"

        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "controversy_flags": flags,
        })

    return results
