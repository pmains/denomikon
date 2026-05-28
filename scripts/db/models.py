"""SQLAlchemy ORM model classes for Maricopa governance data."""

import logging
from datetime import datetime, timezone

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
)
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


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
    minutes_url = Column(String(512), nullable=True, default=None)
    votes_extracted = Column(Boolean, nullable=False, default=False)
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

    __table_args__ = (
        UniqueConstraint("body", "meeting_id", name="uq_meeting_body_id"),
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
    item_type = Column(String(16), nullable=False, default="", index=True)
    section_level = Column(Integer, nullable=True, default=None)
    sort_order = Column(Integer, nullable=True, default=None, index=True)
    agenda_category = Column(String(32), nullable=False, default="", index=True)
    jurisdiction_id = Column(Integer, nullable=True, default=None, index=True)
    public_body_id = Column(Integer, nullable=True, default=None, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PublicBodyMember(Base):
    """DEPRECATED — do not use. Use body_memberships + persons instead.

    This table has 0 rows in production and is never written to by active
    code paths.  It is kept only for migration compatibility.  All new
    member data goes through ``body_memberships`` and ``persons``.
    """
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
    scraped_at = Column(DateTime(timezone=True), nullable=True, default=None)
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
    downloaded_at = Column(DateTime(timezone=True), nullable=True, default=None)
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
    """A named seat or district within a public body."""
    __tablename__ = "body_seats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_body_id = Column(Integer, nullable=False, index=True)
    seat_name = Column(String(128), nullable=True, default=None)
    district_number = Column(String(16), nullable=True, default=None)
    seat_type = Column(String(32), nullable=True, default=None, comment="elected|appointed|ex-officio")
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
    Membership validity for a given meeting date ``md``::
        term_start <= md AND (term_end IS NULL OR term_end >= md)
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
    permit_type = Column(Text, nullable=True, default=None)
    work_class = Column(Text, nullable=True, default=None)
    permit_number = Column(String(64), nullable=True, default=None, index=True)
    permit_issue_date = Column(String(32), nullable=True, default=None)
    permit_description = Column(Text, nullable=True, default=None)
    permit_valuation = Column(String(32), nullable=True, default=None)
    permit_square_feet = Column(String(32), nullable=True, default=None)
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
        UniqueConstraint("report_adid", "row_hash", name="uq_permit_per_report"),
        UniqueConstraint("source_system", "source_record_id", name="uq_permit_source"),
        Index("ix_permits_issue_date", "permit_issue_date"),
        Index("ix_permits_issue_date_category", "permit_issue_date", "normalized_category"),
        Index("ix_permits_issue_date_jurisdiction", "permit_issue_date", "jurisdiction"),
        Index("ix_permits_native_type", "native_type"),
        Index("ix_permits_valuation", "permit_valuation"),
        Index("ix_permits_square_feet", "permit_square_feet"),
        Index("ix_permits_jur_cat_wt_issuedate", "jurisdiction", "normalized_category", "work_type", "permit_issue_date"),
        Index("ix_permits_dedup_parts", "permit_number", "row_hash", "permit_square_feet"),
    )
