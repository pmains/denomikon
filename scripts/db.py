"""
Persistence layer for Maricopa agenda data.

Uses SQLAlchemy to store meetings and agenda items.
Defaults to SQLite; set DATABASE_URL to switch to Postgres.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
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


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    meeting_id = Column(String(32), unique=True, nullable=False, index=True)
    meeting_date = Column(String(16), nullable=False)
    meeting_type = Column(String(64), nullable=False, default="")
    meeting_title = Column(String(256), nullable=False, default="")
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


def init_db():
    """Create all tables if they don't exist, or migrate existing ones."""
    Base.metadata.create_all(bind=get_engine())

    _migrate_table("supporting_documents")

    engine = get_engine()
    _migrate_col(engine, "agenda_items", "c_number", "VARCHAR(32) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_base", "VARCHAR(48) NOT NULL DEFAULT ''")
    _migrate_col(engine, "agenda_items", "c_number_revision", "VARCHAR(16) DEFAULT NULL")

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
