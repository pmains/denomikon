"""
Persistence layer for Maricopa agenda data.

Uses SQLAlchemy to store meetings and agenda items.
Defaults to SQLite; set DATABASE_URL to switch to Postgres.
"""

import os
import re
from datetime import date, datetime, timezone
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
        UniqueConstraint("meeting_id", "supervisor_id", name="uq_meeting_supervisor"),
    )


class AgendaItemVote(Base):
    __tablename__ = "agenda_item_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agenda_item_id = Column(Integer, nullable=False, index=True, unique=True)
    meeting_id = Column(String(32), nullable=False, index=True)
    agenda_item_number = Column(Integer, nullable=False, index=True)
    c_number = Column(String(32), nullable=True, default=None, index=True)
    c_number_base = Column(String(48), nullable=True, default=None, index=True)
    motion_result = Column(String(64), nullable=True, default=None)
    vote_text = Column(Text, nullable=True, default=None)
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
    meeting_id = Column(String(32), unique=True, nullable=False, index=True)
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


class AgendaItem(Base):
    __tablename__ = "agenda_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
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
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        None,
    )
class SupportingDocument(Base):
    __tablename__ = "supporting_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
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
    # Fallback: first word of raw_type
    parts = (raw_type or "").strip().split()
    if parts:
        return parts[0].capitalize()
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

    # Skip if it's just a known meeting type word
    lower = t.lower()
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

    _migrate_table("supervisors")
    _migrate_table("meeting_supervisors")
    _migrate_table("agenda_item_votes")
    _migrate_table("supervisor_votes")

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


def create_or_get_meeting(session: Session, meeting_dict: dict) -> Meeting:
    """Get or create a meeting row, setting sync_status=pending for new rows."""
    meeting_id = meeting_dict.get("meeting_id", "")
    existing = session.execute(
        select(Meeting).where(Meeting.meeting_id == meeting_id)
    ).scalar_one_or_none()
    if existing:
        return existing
    meeting = Meeting(
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
        select(Meeting).where(Meeting.meeting_id == meeting_id)
    ).scalar_one_or_none()
    if not meeting:
        raise ValueError(f"Meeting {meeting_id} not found")

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
    start_date_iso: str,
    end_date_iso: str,
) -> list[Meeting]:
    """Get all meetings with meeting_date in the given ISO date range (inclusive)."""
    q = (
        select(Meeting)
        .where(Meeting.meeting_date >= start_date_iso)
        .where(Meeting.meeting_date <= end_date_iso)
        .order_by(Meeting.meeting_date, Meeting.meeting_id)
    )
    return list(session.execute(q).scalars().all())


def get_meetings_by_status(
    session: Session,
    statuses: Optional[list[str]] = None,
    *,
    force: bool = False,
    meeting_ids: Optional[list[str]] = None,
) -> list[Meeting]:
    """Get meetings filtered by sync_status and/or meeting_ids.

    If force is True, ignore status filter and return all matching meeting_ids.
    """
    q = select(Meeting).order_by(Meeting.meeting_date, Meeting.meeting_id)
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


def get_failed_meetings(session: Session) -> list[Meeting]:
    """Get meetings with failed or partial status (excludes manual_review)."""
    return get_meetings_by_status(session, ["failed", "partial"], force=False)


def upsert_meeting(session: Session, meeting: Meeting) -> Meeting:
    """Insert or update a meeting by meeting_id."""
    existing = session.execute(
        select(Meeting).where(Meeting.meeting_id == meeting.meeting_id)
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
    meeting_id: str,
    agenda_item_dicts: list[dict],
    supporting_doc_dicts: Optional[list[dict]] = None,
) -> int:
    """Transactionally persist a meeting's agenda items and supporting docs.

    WARNING: This replaces ALL existing agenda_items and supporting_documents
    for the given meeting_id. Callers should only invoke this after
    successfully extracting data into memory and validating.

    Steps:
    1. Delete existing agenda_items for this meeting_id.
    2. Delete existing supporting_documents for this meeting_id.
    3. Insert new agenda_item rows and supporting doc rows.
    4. Verify the inserted count matches expected.
    5. Commit only if validation passes; rollback on failure.

    Returns the number of agenda items persisted.
    Raises ValueError if the count doesn't match.
    """
    expected_count = len(agenda_item_dicts)
    inserted_item_count = 0
    inserted_doc_count = 0

    # Delete existing rows for this meeting
    session.execute(
        AgendaItem.__table__.delete().where(
            AgendaItem.meeting_id == meeting_id
        )
    )
    session.execute(
        SupportingDocument.__table__.delete().where(
            SupportingDocument.meeting_id == meeting_id
        )
    )

    for item_dict in agenda_item_dicts:
        item = AgendaItem(
            meeting_id=meeting_id,
            agenda_item_number=int(item_dict.get("agenda_item_number", 0)),
            agenda_item_id=item_dict.get("agenda_item_id", ""),
            agenda_item_title=item_dict.get("agenda_item_title", ""),
            agenda_item_text=item_dict.get("agenda_item_text", ""),
            agenda_item_url=item_dict.get("agenda_item_url", ""),
            vote_or_action=item_dict.get("vote_or_action", ""),
            source_body=item_dict.get("source_body", "Board of Supervisors"),
            source_url=item_dict.get("source_url", ""),
            c_number=item_dict.get("c_number", ""),
            c_number_base=item_dict.get("c_number_base", ""),
            c_number_revision=item_dict.get("c_number_revision", None),
        )
        session.add(item)
        inserted_item_count += 1

    if inserted_item_count != expected_count:
        raise ValueError(
            f"Inserted {inserted_item_count} items but expected {expected_count}"
        )

    if supporting_doc_dicts:
        for doc_dict in supporting_doc_dicts:
            doc = SupportingDocument(
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

    session.commit()
    return inserted_item_count


def replace_meeting_data_safe(
    session: Session,
    meeting_id: str,
    meeting_dict: dict,
    agenda_item_dicts: list[dict],
    supporting_doc_dicts: Optional[list[dict]] = None,
) -> int:
    """Safely replace meeting data within a transaction.

    This creates the meeting row if needed, replaces items/docs,
    and updates sync status to 'complete' on success.
    Returns the number of agenda items persisted.

    On failure, rolls back and raises.
    """
    try:
        # Ensure meeting row exists (creates if new)
        meeting = create_or_get_meeting(session, meeting_dict)

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
            meeting_id,
            agenda_item_dicts,
            supporting_doc_dicts,
        )

        doc_count = len(supporting_doc_dicts) if supporting_doc_dicts else 0

        update_sync_status(
            session,
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


def persist_votes(
    session: Session,
    meeting_id: str,
    supervisors: list[dict],
    votes: list[dict],
) -> int:
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

    # 2. Delete existing records for this meeting
    session.execute(
        MeetingSupervisor.__table__.delete().where(
            MeetingSupervisor.meeting_id == meeting_id
        )
    )
    existing_aiv_rows = session.execute(
        select(AgendaItemVote).where(AgendaItemVote.meeting_id == meeting_id)
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
            AgendaItemVote.meeting_id == meeting_id
        )
    )
    session.flush()
    # Clear identity map to avoid stale identity warnings when re-inserting
    session.expire_all()

    vote_count = 0

    # 3. Insert meeting_supervisor records
    for sup in supervisors:
        norm = sup.get("normalized_name", sup.get("name", "").lower().strip())
        sup_id = supervisor_map.get(norm)
        if sup_id is None:
            continue
        ms = MeetingSupervisor(
            meeting_id=meeting_id,
            supervisor_id=sup_id,
            role=sup.get("role"),
            present=sup.get("present", True),
        )
        session.add(ms)

    # 4. Insert vote records
    for vote in votes:
        item_number = int(vote.get("agenda_item_number", 0))
        aiv = AgendaItemVote(
            agenda_item_id=vote.get("agenda_item_id", 0),
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

    # 5. Commit
    session.commit()
    return vote_count
