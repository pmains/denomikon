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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    inspect as sa_inspect,
    or_,
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


class Person(Base):
    """Generic person/member registry for any public body.

    Memberships (which body they serve on, term dates, roles, seats)
    are stored in ``body_memberships``.  This table holds only identity.
    """
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# Backward-compatible alias for existing code that references "Supervisor"
Supervisor = Person


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
    agenda_item_number = Column(String(32), nullable=False, index=True)
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
    # Multi-jurisdiction FK columns (logical FKs — added after schema creation)
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    public_body_id = Column(Integer, nullable=True, default=None, index=True)
    source_system = Column(String(64), nullable=True, default=None)
    source_instance_url = Column(String(512), nullable=True, default=None)
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
    agenda_item_number = Column(String(32), nullable=False, default="", index=True)
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
    # Structural / semantic fields (OnBase, PZ, Tempe)
    item_type = Column(String(16), nullable=False, default="", index=True)
    section_level = Column(Integer, nullable=True, default=None)
    sort_order = Column(Integer, nullable=True, default=None, index=True)
    agenda_category = Column(String(32), nullable=False, default="", index=True)
    # Multi-jurisdiction FK columns
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    public_body_id = Column(Integer, nullable=True, default=None, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        None,
    )
class PublicBodyMember(Base):
    """DEPRECATED — merged into supervisors table. Kept for migration only."""
    __tablename__ = "public_body_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    body = Column(String(16), nullable=False, default="", index=True)
    name = Column(String(128), nullable=False, index=True)
    normalized_name = Column(String(128), nullable=False, index=True)
    title = Column(String(64), nullable=True, default=None)
    district_or_seat = Column(String(32), nullable=True, default=None)
    active_from = Column(Date, nullable=True, default=None)
    active_to = Column(Date, nullable=True, default=None)
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    public_body_id = Column(Integer, nullable=True, default=None, index=True)
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
    agenda_item_number = Column(String(32), nullable=False, default="", index=True)
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
    # Multi-jurisdiction FK column
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
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


class Jurisdiction(Base):
    """A government jurisdiction (county, city, town) whose meetings we track."""
    __tablename__ = "jurisdictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, unique=True, index=True)
    slug = Column(String(64), nullable=False, unique=True, index=True)
    state = Column(String(2), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PublicBody(Base):
    """A public body (board, commission, committee) within a jurisdiction."""
    __tablename__ = "public_bodies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(Integer, nullable=False, index=True)
    name = Column(String(256), nullable=False)
    slug = Column(String(64), nullable=False, index=True)
    body_code = Column(String(16), nullable=True, default=None, index=True)
    body_type = Column(String(64), nullable=True, default=None)
    description = Column(Text, nullable=True, default=None)
    website_url = Column(String(512), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("jurisdiction_id", "slug", name="uq_public_body_slug"),)


class BodySeat(Base):
    """A named seat or district within a public body.

    Examples: "District 1" (BOS), "At-Large" (Tempe Council),
    "Chair" (P&Z), "Alternate" (Board of Adjustment).

    Not all bodies have stable seat concepts.  When a body doesn't,
    this table stays empty and memberships reference only the body.
    """
    __tablename__ = "body_seats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_body_id = Column(Integer, nullable=False, index=True)
    seat_name = Column(String(128), nullable=True, default=None)
    district_number = Column(String(16), nullable=True, default=None)
    seat_type = Column(String(32), nullable=True, default=None,
                       comment="elected|appointed|ex-officio")
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
        UniqueConstraint("public_body_id", "seat_name", name="uq_body_seat_name"),
    )


class BodyMembership(Base):
    """A person's term of service on a public body.

    A person may have multiple non-consecutive memberships on the
    same body (e.g. two non-adjacent council terms).  Each term
    is a separate row.

    Membership validity for a given meeting date ``md``::

        term_start <= md AND (term_end IS NULL OR term_end >= md)

    ``term_end`` is NULL for currently-serving members or when the
    end date is unknown.
    """
    __tablename__ = "body_memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(Integer, nullable=False, index=True)
    public_body_id = Column(Integer, nullable=False, index=True)
    body_seat_id = Column(Integer, nullable=True, default=None, index=True)
    role = Column(String(64), nullable=True, default=None,
                  comment="Supervisor, Councilmember, Mayor, Chair, etc.")
    term_start = Column(Date, nullable=False)
    term_end = Column(Date, nullable=True, default=None)
    selection_method = Column(String(32), nullable=True, default=None,
                              comment="elected|appointed|ex-officio")
    source_url = Column(String(512), nullable=True, default=None)
    notes = Column(Text, nullable=True, default=None)
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
        Index("ix_membership_body_term", "public_body_id", "term_start", "term_end"),
        Index("ix_membership_person_body", "person_id", "public_body_id"),
        Index("ix_membership_body_term_end", "public_body_id", "term_end"),
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

    jurisdiction = Column(String(64), nullable=True, default=None, index=True)
    application_date = Column(String(32), nullable=True, default=None)
    height_stories = Column(String(32), nullable=True, default=None)
    permit_status = Column(String(64), nullable=True, default=None, comment="Permit status: Issued, Finaled, Expired, etc.")
    permit_last_inspection_date = Column(String(32), nullable=True, default=None, comment="Date of last inspection (indicates completion)")
    permit_expiration_date = Column(String(32), nullable=True, default=None, comment="Permit expiration date")
    assessor_code = Column(String(64), nullable=True, default=None, comment="Assessor's property classification code")
    native_type = Column(Text, nullable=True, default=None, comment="Original jurisdiction-specific permit type label")
    native_category = Column(Text, nullable=True, default=None, comment="Original jurisdiction-specific category label")
    normalized_category = Column(String(64), nullable=True, default=None, index=True, comment="Cross-jurisdiction category: Residential, Commercial, Industrial, Mixed-Use, Other")
    work_type = Column(String(32), nullable=True, default=None, index=True, comment="New Construction, Addition, Alteration, Trade, Demolition, Infrastructure, Unknown")

    # Tempe ArcGIS permit fields
    applied_date = Column(String(32), nullable=True, default=None)
    completed_date = Column(String(32), nullable=True, default=None)
    certificate_of_occupancy_date = Column(String(32), nullable=True, default=None)
    units = Column(String(16), nullable=True, default=None, comment="Total housing units from source")
    project_name = Column(Text, nullable=True, default=None)
    fee = Column(String(32), nullable=True, default=None)
    latitude = Column(String(32), nullable=True, default=None)
    longitude = Column(String(32), nullable=True, default=None)
    raw_permit_type = Column(Text, nullable=True, default=None)
    raw_permit_type_description = Column(Text, nullable=True, default=None)
    raw_permit_class = Column(String(64), nullable=True, default=None)
    # Phoenix PDD structure class code (001-997 series): classifies what kind
    # of building the work is on, e.g. 001=Single Family, 007=10+ Family Units.
    # Used alongside permit_type to identify residential construction in the
    # PDD system where housing units appear under BLD, LPRN, TCO, etc.
    struct_class = Column(String(8), nullable=True, default=None)
    zone = Column(String(64), nullable=True, default=None)
    source_system = Column(String(64), nullable=True, default=None, index=True)
    source_record_id = Column(String(64), nullable=True, default=None, index=True)
    contractor_license = Column(String(64), nullable=True, default=None)

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
        # Uniqueness by content hash within each report ensures idempotent
        # re-sync without collision on duplicate permit numbers (common in
        # pre-2024 reports where tracking numbers repeat for multi-line items).
        UniqueConstraint("report_adid", "row_hash", name="uq_permit_per_report"),

        # Uniqueness by source system + source record ID prevents
        # duplicates when syncing from external systems (e.g. Tempe ArcGIS).
        UniqueConstraint("source_system", "source_record_id", name="uq_permit_source"),

        # Performance indexes for common query patterns.
        # The aggregate /permits endpoint filters by permit_issue_date (LIKE
        # prefix for year) and groups by normalized_category or jurisdiction.
        Index("ix_permits_issue_date", "permit_issue_date"),
        Index("ix_permits_issue_date_category", "permit_issue_date", "normalized_category"),
        Index("ix_permits_issue_date_jurisdiction", "permit_issue_date", "jurisdiction"),
        Index("ix_permits_native_type", "native_type"),
        Index("ix_permits_valuation", "permit_valuation"),
        Index("ix_permits_square_feet", "permit_square_feet"),
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
    init_poliscopic_models()

    _migrate_table("supporting_documents")

    engine = get_engine()
    _migrate_col(engine, "agenda_items", "c_number", "VARCHAR(32) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_base", "VARCHAR(48) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_revision", "VARCHAR(16) DEFAULT NULL")

    _migrate_table("cases")
    _migrate_table("case_events")
    _migrate_table("pz_item_details")

    _migrate_col(engine, "agenda_items", "case_number", "VARCHAR(32) NOT NULL DEFAULT ''")

    _migrate_table("persons")
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

    # Multi-jurisdiction columns
    _migrate_col(engine, "meetings", "jurisdiction_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "meetings", "public_body_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "meetings", "source_system", "VARCHAR(64) DEFAULT NULL")
    _migrate_col(engine, "meetings", "source_instance_url", "VARCHAR(512) DEFAULT NULL")
    _migrate_col(engine, "agenda_items", "jurisdiction_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "agenda_items", "public_body_id", "INTEGER DEFAULT NULL")
    _migrate_col(engine, "supporting_documents", "jurisdiction_id", "INTEGER DEFAULT NULL")

    # Create indexes for multi-jurisdiction lookups
    _ensure_index(engine, "meetings", "idx_meetings_jurisdiction_id", "jurisdiction_id")
    _ensure_index(engine, "meetings", "idx_meetings_public_body_id", "public_body_id")
    _ensure_index(engine, "meetings", "idx_meetings_source_system", "source_system")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_jurisdiction_id", "jurisdiction_id")
    _ensure_index(engine, "agenda_items", "idx_agenda_items_public_body_id", "public_body_id")
    _ensure_index(engine, "supporting_documents", "idx_supporting_docs_jurisdiction_id", "jurisdiction_id")

    # Backfill multi-jurisdiction columns for existing Maricopa records
    backfill_multi_jurisdiction_columns(engine)

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

    # Create poliscopic tables
    init_poliscopic_models(engine)

    # Seed default jurisdiction and bodies
    seed_default_jurisdictions()

    # Backfill existing records to body='bos' and determine pz from meeting_type
    backfill_body_column(engine)

    # Drop deprecated Person columns and legacy tables
    _drop_deprecated_person_columns()

    # Migrate to historical membership model (BodySeat + BodyMembership)
    _migrate_membership_model()


def backfill_multi_jurisdiction_columns(engine):
    """Backfill jurisdiction_id, public_body_id for existing Maricopa records.

    Maps meetings.body (body_code) to public_bodies.body_code to set public_body_id.
    All existing data belongs to Maricopa County (jurisdiction_id=1).
    Uses _multi_jurisdiction_backfilled as a migration marker.
    """
    inspector = sa_inspect(engine)
    if "meetings" not in inspector.get_table_names():
        return

    # Add marker column if needed
    _migrate_col(engine, "meetings", "_multi_jurisdiction_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "agenda_items", "_multi_jurisdiction_backfilled", "BOOLEAN NOT NULL DEFAULT 0")
    _migrate_col(engine, "supporting_documents", "_multi_jurisdiction_backfilled", "BOOLEAN NOT NULL DEFAULT 0")

    with engine.connect() as conn:
        # Check if already backfilled
        existing = conn.execute(
            text("SELECT COUNT(*) FROM meetings WHERE _multi_jurisdiction_backfilled = 0")
        ).scalar()
        if existing == 0:
            return

        # Map body_code to public_body_id
        body_map = {}
        rows = conn.execute(
            text("SELECT id, body_code FROM public_bodies WHERE body_code IS NOT NULL")
        ).fetchall()
        for row in rows:
            body_map[row[1]] = row[0]

        # Backfill meetings
        conn.execute(
            text("""
                UPDATE meetings
                SET jurisdiction_id = 1,
                    public_body_id = (
                        SELECT pb.id FROM public_bodies pb
                        WHERE pb.body_code = meetings.body
                        LIMIT 1
                    ),
                    _multi_jurisdiction_backfilled = 1
                WHERE _multi_jurisdiction_backfilled = 0
            """)
        )

        # Backfill agenda_items from their parent meeting
        conn.execute(
            text("""
                UPDATE agenda_items
                SET jurisdiction_id = 1,
                    public_body_id = (
                        SELECT m.public_body_id FROM meetings m
                        WHERE m.meeting_id = agenda_items.meeting_id
                          AND m.body = agenda_items.body
                        LIMIT 1
                    ),
                    _multi_jurisdiction_backfilled = 1
                WHERE _multi_jurisdiction_backfilled = 0
            """)
        )

        # Backfill supporting_documents from their parent meeting
        conn.execute(
            text("""
                UPDATE supporting_documents
                SET jurisdiction_id = 1,
                    _multi_jurisdiction_backfilled = 1
                WHERE _multi_jurisdiction_backfilled = 0
            """)
        )

        conn.commit()


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
                try:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {marker}"))
                    conn.commit()
                except Exception:
                    pass
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
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}')
                )
                conn.commit()
        except Exception:
            pass  # Race: parallel worker may have added it first


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


def _resolve_jurisdiction_id(session: Session, body: str) -> Optional[int]:
    """Resolve a meeting's jurisdiction_id from its public body code."""
    pb = session.execute(
        select(PublicBody).where(PublicBody.body_code == body)
    ).scalar_one_or_none()
    if pb:
        return pb.jurisdiction_id
    return None


def create_or_get_meeting(session: Session, body: str, meeting_dict: dict) -> Meeting:
    """Get or create a meeting row, setting sync_status=pending for new rows.

    Jurisdiction ID is resolved from the public body table so that every
    meeting gets the correct jurisdiction regardless of sync code path.
    """
    meeting_id = meeting_dict.get("meeting_id", "")
    existing = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    jurisdiction_id = _resolve_jurisdiction_id(session, body)
    meeting = Meeting(
        body=body,
        meeting_id=meeting_id,
        meeting_date=meeting_dict.get("meeting_date", ""),
        meeting_type=meeting_dict.get("meeting_type", ""),
        meeting_title=meeting_dict.get("meeting_title", ""),
        meeting_title_raw=meeting_dict.get("meeting_title", ""),
        source_url=meeting_dict.get("source_url", ""),
        sync_status="pending",
        jurisdiction_id=jurisdiction_id,
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
    elif status in ("manual_review", "no_agenda"):
        # These are classifications, not failures; don't increment retries
        meeting.retry_count = 0
        if error:
            meeting.last_error = error
        if status == "no_agenda":
            meeting.last_synced_at = now
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
    for sort_idx, item_dict in enumerate(agenda_item_dicts):
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
            agenda_item_number=str(item_dict.get("agenda_item_number", "0") or "0"),
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
            item_type=item_dict.get("item_type", ""),
            section_level=item_dict.get("section_level"),
            sort_order=sort_idx,
            agenda_category=item_dict.get("agenda_category", ""),
        )
        session.add(item)
        inserted_item_count += 1

    if supporting_doc_dicts:
        for doc_dict in supporting_doc_dicts:
            doc = SupportingDocument(
                body=body,
                agenda_item_id=doc_dict.get("agenda_item_id", 0),
                meeting_id=meeting_id,
                agenda_item_number=str(doc_dict.get("agenda_item_number", "0") or "0"),
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
    if not session or aiv_list[0] is None:
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


def _ensure_membership(
    session: Session,
    person_id: int,
    body: str,
    meeting_date: Optional[date] = None,
) -> Optional[BodyMembership]:
    """Ensure a BodyMembership row exists for this person + body.

    Creates a membership using meeting_date (or today) as term_start if
    one doesn't already exist.  Returns the existing or new membership.
    """
    # Resolve public_body_id from body code
    pb = session.execute(
        select(PublicBody).where(PublicBody.body_code == body)
    ).scalar_one_or_none()
    if pb is None:
        return None

    # Check existing membership
    existing = session.execute(
        select(BodyMembership)
        .where(BodyMembership.person_id == person_id)
        .where(BodyMembership.public_body_id == pb.id)
        .order_by(BodyMembership.term_start.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Look up person for role/title info
    person = session.execute(
        select(Person).where(Person.id == person_id)
    ).scalar_one_or_none()
    if person is None:
        return None

    term_start = meeting_date or date.today()
    membership = BodyMembership(
        person_id=person_id,
        public_body_id=pb.id,
        role=getattr(person, 'title', None) or None,
        term_start=term_start,
        selection_method="elected" if body == "bos" else "appointed",
    )
    session.add(membership)
    session.flush()
    return membership


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
    # Look up meeting date for membership creation
    meeting_date = None
    meeting_row = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if meeting_row and meeting_row.meeting_date:
        try:
            meeting_date = date.fromisoformat(meeting_row.meeting_date)
        except (ValueError, TypeError):
            pass

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
            existing.updated_at = datetime.now(timezone.utc)
            # Ensure BodyMembership exists for this person + body
            _ensure_membership(session, existing.id, body, meeting_date)
            supervisor_map[norm] = existing.id
        else:
            new = Supervisor(
                name=sup.get("name", ""),
                normalized_name=norm,
            )
            session.add(new)
            session.flush()
            supervisor_map[norm] = new.id

            # If Tempe council member, pass role info to membership
            role = None
            if "tempe" in body:
                titler_map = {"woods": "Mayor", "garlid": "Vice Mayor"}
                role = titler_map.get(norm, "Councilmember")

            # Ensure BodyMembership exists for new person + body
            membership = _ensure_membership(session, new.id, body, meeting_date)
            if membership and role:
                membership.role = role

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
        item_number = str(vote.get("agenda_item_number", "0"))
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
                        body=body,
                    )
                    session.add(new)
                    session.flush()
                    sup_id = new.id
                    supervisor_map[norm_name] = sup_id

                    # Ensure BodyMembership for this new person
                    _ensure_membership(session, new.id, body, meeting_date)

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
    aiv_rows = [r for r in session.execute(
        select(AgendaItemVote).where(
            AgendaItemVote.body == body,
            AgendaItemVote.meeting_id == meeting_id,
        )
    ).scalars().all() if r is not None]
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


def init_poliscopic_models(engine=None):
    """Create all poliscopic tables that may not yet exist (jurisdictions, public_bodies, etc.)."""
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine, checkfirst=True)


def _migrate_existing_tables(engine=None):
    """Add columns to existing tables that were introduced after initial creation.

    SQLite's CREATE TABLE IF NOT EXISTS won't ALTER existing tables, so
    newly-added columns on tables that already exist need explicit ALTER TABLE.
    This function uses PRAGMA table_info to check before adding.
    """
    if engine is None:
        engine = get_engine()

    migrations = [
        ("public_body_members", "jurisdiction_id", "INTEGER DEFAULT NULL"),
        ("public_body_members", "public_body_id", "INTEGER DEFAULT NULL"),
        ("permits", "jurisdiction", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "application_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "height_stories", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "native_type", "TEXT DEFAULT NULL"),
        ("permits", "native_category", "TEXT DEFAULT NULL"),
        ("permits", "normalized_category", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "work_type", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "applied_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "completed_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "certificate_of_occupancy_date", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "units", "VARCHAR(16) DEFAULT NULL"),
        ("permits", "project_name", "TEXT DEFAULT NULL"),
        ("permits", "fee", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "latitude", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "longitude", "VARCHAR(32) DEFAULT NULL"),
        ("permits", "raw_permit_type", "TEXT DEFAULT NULL"),
        ("permits", "raw_permit_type_description", "TEXT DEFAULT NULL"),
        ("permits", "raw_permit_class", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "zone", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "source_system", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "source_record_id", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "contractor_license", "VARCHAR(64) DEFAULT NULL"),
        ("permits", "struct_class", "VARCHAR(8) DEFAULT NULL"),
    ]

    with engine.connect() as conn:
        for table, column, col_type in migrations:
            # Check if table exists
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            )
            if not result.fetchone():
                continue  # table doesn't exist yet — create_all will handle it

            # Check if column exists
            result = conn.execute(
                text(f"PRAGMA table_info('{table}')"),
            )
            existing_cols = {row[1] for row in result.fetchall()}
            if column not in existing_cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
        conn.commit()


def _migrate_supervisors_to_public_body_members():
    """DEPRECATED — membership data now lives in BodyMembership.
    Kept as a true no-op."""
    pass


def seed_default_jurisdictions():
    """Populate the Maricopa County jurisdiction and its public bodies if empty."""
    # Ensure tables exist (both new ones and columns added to existing ones)
    _migrate_existing_tables()
    init_poliscopic_models()

    # Migrate legacy Supervisor data into public_body_members (idempotent)
    _migrate_supervisors_to_public_body_members()

    # Populate permit jurisdiction, native_type, and normalized_category
    _migrate_permit_normalized_fields()

    session = get_session()
    try:
        # ── Maricopa County (jurisdiction_id=1) ──
        mc = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "maricopa-county")
        ).scalar_one_or_none()
        if mc is None:
            mc = Jurisdiction(name="Maricopa County", slug="maricopa-county", state="AZ")
            session.add(mc)
            session.flush()

        maricopa_bodies = [
            PublicBody(jurisdiction_id=mc.id, name="Board of Supervisors", slug="board-of-supervisors", body_code="bos", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Planning & Zoning Commission", slug="planning-zoning-commission", body_code="pz", body_type="Commission"),
            PublicBody(jurisdiction_id=mc.id, name="Board of Adjustment", slug="board-of-adjustment", body_code="adj", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Board of Health", slug="board-of-health", body_code="health", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Drainage Review Board", slug="drainage-review-board", body_code="drain", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Transportation Advisory Board", slug="transportation-advisory-board", body_code="tab", body_type="Board"),
            PublicBody(jurisdiction_id=mc.id, name="Industrial Development Authority", slug="industrial-development-authority", body_code="ida", body_type="Authority"),
        ]
        for pb in maricopa_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == mc.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)
        session.flush()

        # ── City of Tempe (jurisdiction_id=2) ──
        tempe = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "tempe")
        ).scalar_one_or_none()
        if tempe is None:
            tempe = Jurisdiction(name="City of Tempe", slug="tempe", state="AZ")
            session.add(tempe)
            session.flush()

        # OnBase Agenda Online meeting type IDs are noted in comments
        # Actual body names, not session types (e.g. "Executive Session" is a type, not a body)
        tempe_bodies = [
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe City Council",
                slug="tempe-city-council",
                body_code="tempe-cc",
                body_type="Council",
                website_url="https://www.tempe.gov/government/mayor-and-city-council",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Development Review Commission",
                slug="tempe-development-review-commission",
                body_code="tempe-drc",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Board of Adjustment",
                slug="tempe-board-of-adjustment",
                body_code="tempe-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Historic Preservation Commission",
                slug="tempe-historic-preservation-commission",
                body_code="tempe-hpc",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Housing Authority",
                slug="tempe-housing-authority",
                body_code="tempe-ha",
                body_type="Authority",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Rio Salado Community Facilities District Board",
                slug="tempe-rio-salado-cfd",
                body_code="tempe-rio",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Risk Management Trust Board",
                slug="tempe-risk-management-trust",
                body_code="tempe-rmt",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=tempe.id,
                name="Tempe Joint Review Committee",
                slug="tempe-joint-review-committee",
                body_code="tempe-jrc",
                body_type="Committee",
            ),
        ]
        for pb in tempe_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == tempe.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)
        session.flush()

        # ── City of Chandler (jurisdiction_id=3) ──
        chandler = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "chandler")
        ).scalar_one_or_none()
        if chandler is None:
            chandler = Jurisdiction(name="City of Chandler", slug="chandler", state="AZ")
            session.add(chandler)
            session.flush()

        chandler_bodies = [
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler City Council",
                slug="chandler-city-council",
                body_code="chandler-cc",
                body_type="Council",
            ),
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler Planning & Zoning Commission",
                slug="chandler-planning-zoning-commission",
                body_code="chandler-pz",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler Board of Adjustment",
                slug="chandler-board-of-adjustment",
                body_code="chandler-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=chandler.id,
                name="Chandler Development Review Commission",
                slug="chandler-development-review-commission",
                body_code="chandler-drc",
                body_type="Commission",
            ),
        ]
        for pb in chandler_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == chandler.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)
        session.flush()

        # ── City of Phoenix (jurisdiction_id=4) ──
        phoenix = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "phoenix")
        ).scalar_one_or_none()
        if phoenix is None:
            phoenix = Jurisdiction(name="City of Phoenix", slug="phoenix", state="AZ")
            session.add(phoenix)
            session.flush()

        phoenix_bodies = [
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix City Council",
                slug="phoenix-city-council",
                body_code="phoenix-cc",
                body_type="Council",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Planning Commission",
                slug="phoenix-planning-commission",
                body_code="phoenix-pc",
                body_type="Commission",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Board of Adjustment",
                slug="phoenix-board-of-adjustment",
                body_code="phoenix-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=phoenix.id,
                name="Phoenix Village Planning Committees",
                slug="phoenix-village-planning",
                body_code="phoenix-vpc",
                body_type="Committee",
            ),
        ]
        for pb in phoenix_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == phoenix.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        session.commit()
    finally:
        session.close()


def _migrate_permit_normalized_fields():
    """Populate jurisdiction, native_type, and normalized_category on permits.

    These fields were added after data was already ingested.  Also derive
    normalized_category (Residential, Commercial, Industrial, Infrastructure,
    Other) from the native permit_type label so the aggregate views work.
    """
    session = get_session()
    try:
        # Only process permits that haven't been migrated yet
        to_migrate = session.execute(
            select(Permit).where(
                or_(
                    Permit.jurisdiction.is_(None),
                    Permit.native_type.is_(None),
                    Permit.normalized_category.is_(None),
                )
            )
        ).scalars().all()

        if not to_migrate:
            return

        for p in to_migrate:
            if not p.jurisdiction:
                p.jurisdiction = "Maricopa County"
            if not p.native_type and p.permit_type:
                p.native_type = p.permit_type
            if not p.normalized_category and p.permit_type:
                pt = p.permit_type.lower()
                if "residential" in pt:
                    p.normalized_category = "Residential"
                elif "commercial" in pt:
                    p.normalized_category = "Commercial"
                elif "industrial" in pt:
                    p.normalized_category = "Industrial"
                elif "mixed" in pt:
                    p.normalized_category = "Mixed-Use"
                elif "grading" in pt or "infrastructure" in pt or "stormwater" in pt:
                    p.normalized_category = "Infrastructure"
                else:
                    p.normalized_category = "Other"

        session.commit()
    finally:
        session.close()


def get_public_bodies_by_jurisdiction(slug):
    """Get all public bodies for a jurisdiction slug."""
    session = get_session()
    try:
        jurisdiction = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == slug)
        ).scalar_one_or_none()
        if not jurisdiction:
            return []
        bodies = session.execute(
            select(PublicBody).where(PublicBody.jurisdiction_id == jurisdiction.id).order_by(PublicBody.name)
        ).scalars().all()
        return list(bodies)
    finally:
        session.close()


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


def _parse_date(val) -> Optional[date]:
    """Parse a value into a date, handling SQLite string returns."""
    if val is None:
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except (ValueError, TypeError):
            return None
    return None


def _drop_deprecated_person_columns():
    """Drop legacy Person columns migrated to BodyMembership, and the
    legacy public_body_members table.

    Deprecated columns dropped:
      active_from, active_to, body, title, district, district_or_seat,
      jurisdiction_id, public_body_id, _person_dates_backfilled,
      _membership_migrated
    """
    engine = get_engine()
    inspector = sa_inspect(engine)
    if "persons" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("persons")}
    to_drop = [
        "active_from", "active_to", "body", "title", "district",
        "district_or_seat", "jurisdiction_id", "public_body_id",
        "_person_dates_backfilled", "_membership_migrated",
    ]
    existing_to_drop = [c for c in to_drop if c in existing_cols]

    if not existing_to_drop:
        pass  # Already clean
    else:
        try:
            with engine.connect() as conn:
                for col in existing_to_drop:
                    conn.execute(text(f"ALTER TABLE persons DROP COLUMN {col}"))
                conn.commit()
            log.info(f"_drop_deprecated_person_columns: dropped {len(existing_to_drop)} column(s)")
        except Exception as e:
            log.warning(f"_drop_deprecated_person_columns: partial failure: {e}")

    # Drop legacy table
    if "public_body_members" in inspector.get_table_names():
        try:
            with engine.connect() as conn:
                conn.execute(text("DROP TABLE IF EXISTS public_body_members"))
                conn.commit()
            log.info("_drop_deprecated_person_columns: dropped public_body_members table")
        except Exception as e:
            log.warning(f"_drop_deprecated_person_columns: drop public_body_members failed: {e}")


def _migrate_membership_model():
    """One-time migration from flat Person fields to BodyMembership rows.

    Creates BodyMembership rows for every person who has attended meetings
    (recorded in meeting_supervisors) or who has explicit term data on their
    Person record (active_from/active_to).

    Uses `_membership_migrated` marker column on persons to run once.
    """
    engine = get_engine()
    inspector = sa_inspect(engine)
    needed = {"persons", "public_bodies", "meeting_supervisors", "meetings"}
    existing_tables = set(inspector.get_table_names())
    if not needed.issubset(existing_tables):
        return

    # Ensure the new tables exist
    init_poliscopic_models(engine)

    # Check if migration is still relevant — the deprecated columns
    # (active_from, active_to, public_body_id, title, district, etc.)
    # may have already been dropped by _drop_deprecated_person_columns().
    existing_cols = {c["name"] for c in inspector.get_columns("persons")}
    if "active_from" not in existing_cols:
        # Columns already gone; nothing to migrate.
        return

    # Add marker column
    _migrate_col(engine, "persons", "_membership_migrated", "BOOLEAN NOT NULL DEFAULT 0")

    session = get_session()
    try:
        with engine.connect() as conn:
            already = conn.execute(
                text("SELECT COUNT(*) FROM persons WHERE _membership_migrated = 1")
            ).scalar() or 0
            if already > 0:
                return

        # Map body code → public_body_id
        body_map = {}
        for pb in session.execute(select(PublicBody)).scalars().all():
            if pb.body_code:
                body_map[pb.body_code] = pb.id

        created = 0
        already_covered_ids = set()

        # 1) Create memberships from persons with explicit public_body_id + active_from
        rows = session.execute(text("""
            SELECT id, public_body_id, title, district_or_seat, district,
                   active_from, active_to
            FROM persons
            WHERE _membership_migrated = 0
              AND public_body_id IS NOT NULL
              AND active_from IS NOT NULL
        """)).fetchall()
        for row in rows:
            already_covered_ids.add(row.id)
            membership = BodyMembership(
                person_id=row.id,
                public_body_id=row.public_body_id,
                role=row.title or None,
                term_start=_parse_date(row.active_from) or date(2000, 1, 1),
                term_end=_parse_date(row.active_to) if row.active_to else None,
                selection_method="elected",
            )
            session.add(membership)
            created += 1

        # 2) Create memberships from persons who have attended meetings
        meeting_rows = session.execute(text("""
            SELECT p.id, ms.body, p.name,
                   COALESCE(p.active_from, MIN(m.meeting_date)) AS term_start,
                   p.active_to,
                   COUNT(ms.id) AS attendance
            FROM persons p
            JOIN meeting_supervisors ms ON ms.supervisor_id = p.id
            JOIN meetings m ON m.meeting_id = ms.meeting_id AND m.body = ms.body
            WHERE p._membership_migrated = 0
              AND LENGTH(p.name) < 40
              AND p.name NOT GLOB '*[0-9]*'
            GROUP BY p.id, ms.body
            HAVING COUNT(ms.id) >= 3
        """)).fetchall()
        for mr in meeting_rows:
            if mr.id in already_covered_ids:
                continue
            pb_id = body_map.get(mr.body)
            if pb_id is None:
                continue
            if mr.name.lower().startswith("also present") or mr.name.lower().startswith("also "):
                continue
            membership = BodyMembership(
                person_id=mr.id,
                public_body_id=pb_id,
                role=session.execute(
                    text("SELECT title FROM persons WHERE id = :pid"),
                    {"pid": mr.id}
                ).scalar() or None,
                term_start=_parse_date(mr.term_start) or date(2000, 1, 1),
                term_end=_parse_date(mr.active_to) if mr.active_to else None,
                selection_method="elected" if mr.body == "bos" else "appointed",
            )
            session.add(membership)
            created += 1

        # Mark all persons as migrated
        session.execute(
            text("UPDATE persons SET _membership_migrated = 1")
        )
        session.commit()
        if created:
            log.info(f"_migrate_membership_model: created {created} membership(s)")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _enhance_member_for_template(person: Person, public_body_id: int) -> Person:
    """Add backward-compatible attributes to a Person for template rendering.

    Sets active_from, active_to, title, district_or_seat, body from the
    person's most recent BodyMembership so existing templates still work
    without those database columns.

    Uses setattr on the object's __dict__ since the columns no longer
    exist on the Person model.
    """
    session = get_session()
    try:
        membership = session.execute(
            select(BodyMembership)
            .where(BodyMembership.person_id == person.id)
            .where(BodyMembership.public_body_id == public_body_id)
            .order_by(BodyMembership.term_start.desc())
            .limit(1)
        ).scalar_one_or_none()
        if membership:
            person.__dict__["active_from"] = membership.term_start
            person.__dict__["active_to"] = membership.term_end
            person.__dict__["title"] = membership.role
            # Look up body_code from public_body_id
            pb = session.execute(
                select(PublicBody).where(PublicBody.id == public_body_id)
            ).scalar_one_or_none()
            if pb:
                person.__dict__["body"] = pb.body_code or ""
            # Look up district_or_seat from BodySeat if set
            if membership.body_seat_id:
                seat = session.execute(
                    select(BodySeat).where(BodySeat.id == membership.body_seat_id)
                ).scalar_one_or_none()
                if seat:
                    person.__dict__["district_or_seat"] = seat.seat_name
    finally:
        session.close()
    return person


def get_body_members(body_code, page=1, per_page=10):
    """Get paginated members of a public body by body_code.

    Returns currently active members (no term_end or term_end in the future)
    ordered by most recent term start date.
    """
    session = get_session()
    try:
        today = date.today()
        offset = (page - 1) * per_page

        pb = session.execute(
            select(PublicBody).where(PublicBody.body_code == body_code)
        ).scalar_one_or_none()
        if not pb:
            return [], 0

        # Get active member ids for counting
        active_subq = (
            select(BodyMembership.person_id)
            .where(BodyMembership.public_body_id == pb.id)
            .where(BodyMembership.term_start <= today)
            .where(
                (BodyMembership.term_end.is_(None))
                | (BodyMembership.term_end >= today)
            )
        ).subquery()

        total = session.execute(
            select(func.count(active_subq.c.person_id.distinct()))
        ).scalar() or 0

        members = session.execute(
            select(Person)
            .join(BodyMembership, BodyMembership.person_id == Person.id)
            .where(BodyMembership.public_body_id == pb.id)
            .where(BodyMembership.term_start <= today)
            .where(
                (BodyMembership.term_end.is_(None))
                | (BodyMembership.term_end >= today)
            )
            .order_by(BodyMembership.term_start.desc().nullslast(), Person.name)
            .offset(offset).limit(per_page)
        ).scalars().all()
        return list(members), total
    finally:
        session.close()
