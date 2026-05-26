"""migrations module."""

import logging
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, or_, select, text
from sqlalchemy.orm import Session

from db.models import (Base, Meeting, AgendaItem, SupportingDocument,
    CaseEvent, MeetingSupervisor, AgendaItemVote, PZItemDetail,
    BodyMembership, Person, BodySeat, PublicBody, Jurisdiction, Permit)
from db.core import get_engine, get_session

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

    # One-time backfill: normalize legacy non-ISO meeting dates
    _normalize_existing_meeting_dates(engine)

    # Add item_title column to article_sources if missing
    _migrate_col(engine, "article_sources", "item_title", "VARCHAR(512) NOT NULL DEFAULT ''")

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

    # Composite index for /permits aggregate queries (jurisdiction + category + work_type + date)
    _ensure_index(engine, "permits", "ix_permits_jur_cat_wt_issuedate",
                  "jurisdiction, normalized_category, work_type, permit_issue_date")
    _ensure_index(engine, "permits", "ix_permits_dedup_parts",
                  "permit_number, row_hash, permit_square_feet")

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

        # Backfill meetings — use actual jurisdiction_id from the public body
        conn.execute(
            text("""
                UPDATE meetings
                SET jurisdiction_id = COALESCE(
                        (SELECT pb.jurisdiction_id FROM public_bodies pb
                         WHERE pb.body_code = meetings.body LIMIT 1),
                        1
                    ),
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
                SET jurisdiction_id = (
                        SELECT COALESCE(m.jurisdiction_id, 1) FROM meetings m
                        WHERE m.meeting_id = agenda_items.meeting_id
                          AND m.body = agenda_items.body
                        LIMIT 1
                    ),
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

        # ── City of Mesa (jurisdiction_id=5) ──
        mesa = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "mesa")
        ).scalar_one_or_none()
        if mesa is None:
            mesa = Jurisdiction(name="City of Mesa", slug="mesa", state="AZ")
            session.add(mesa)
            session.flush()

        mesa_bodies = [
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa City Council",
                slug="mesa-city-council",
                body_code="mesa-cc",
                body_type="Council",
                website_url="https://www.mesaaz.gov/Government/City-Council-Meetings",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Planning & Zoning Board",
                slug="mesa-planning-zoning",
                body_code="mesa-pz",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Design Review Board",
                slug="mesa-design-review-board",
                body_code="mesa-drb",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Board of Adjustment",
                slug="mesa-board-of-adjustment",
                body_code="mesa-boa",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Historic Preservation Board",
                slug="mesa-historic-preservation-board",
                body_code="mesa-hpb",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Cadence Community Facilities District Board",
                slug="mesa-cadence-cfd",
                body_code="mesa-cadence",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Eastmark Community Facilities District No. 1 Board",
                slug="mesa-eastmark-cfd-1",
                body_code="mesa-eastmark1",
                body_type="Board",
            ),
            PublicBody(
                jurisdiction_id=mesa.id,
                name="Mesa Eastmark Community Facilities District No. 2 Board",
                slug="mesa-eastmark-cfd-2",
                body_code="mesa-eastmark2",
                body_type="Board",
            ),
        ]
        for pb in mesa_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == mesa.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── Town of Gilbert (jurisdiction_id=6) ──
        gilbert = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "gilbert")
        ).scalar_one_or_none()
        if gilbert is None:
            gilbert = Jurisdiction(name="Town of Gilbert", slug="gilbert", state="AZ")
            session.add(gilbert)
            session.flush()

        gilbert_bodies = [
            PublicBody(
                jurisdiction_id=gilbert.id,
                name="Gilbert Town Council",
                slug="gilbert-town-council",
                body_code="gilbert-tc",
                body_type="Council",
                website_url="https://www.gilbertaz.gov/departments/town-hall/mayor-town-council",
            ),
        ]
        for pb in gilbert_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == gilbert.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── City of Scottsdale (jurisdiction_id=7) ──
        scottsdale = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == "scottsdale")
        ).scalar_one_or_none()
        if scottsdale is None:
            scottsdale = Jurisdiction(name="City of Scottsdale", slug="scottsdale", state="AZ")
            session.add(scottsdale)
            session.flush()

        scottsdale_bodies = [
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale City Council",
                slug="scottsdale-city-council",
                body_code="scottsdale-cc",
                body_type="Council",
                website_url="https://www.scottsdaleaz.gov/council/meeting-information",
            ),
        ]
        for pb in scottsdale_bodies:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == scottsdale.id,
                    PublicBody.slug == pb.slug,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(pb)

        # ── Scottsdale boards (housing/construction related) ──
        scottsdale_boards = [
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Planning Commission",
                slug="scottsdale-planning-commission",
                body_code="scottsdale-pc",
                body_type="Commission",
                website_url="https://www.scottsdaleaz.gov/boards/planning-commission",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Board of Adjustment",
                slug="scottsdale-board-of-adjustment",
                body_code="scottsdale-boa",
                body_type="Board",
                website_url="https://www.scottsdaleaz.gov/boards/board-of-adjustment",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Development Review Board",
                slug="scottsdale-development-review-board",
                body_code="scottsdale-drb",
                body_type="Board",
                website_url="https://www.scottsdaleaz.gov/boards/development-review-board",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Historic Preservation Commission",
                slug="scottsdale-historic-preservation-commission",
                body_code="scottsdale-hpc",
                body_type="Commission",
                website_url="https://www.scottsdaleaz.gov/boards/historic-preservation-commission",
            ),
            PublicBody(
                jurisdiction_id=scottsdale.id,
                name="Scottsdale Building Advisory Board of Appeals",
                slug="scottsdale-building-advisory-board-of-appeals",
                body_code="scottsdale-baba",
                body_type="Board",
                website_url="https://www.scottsdaleaz.gov/boards/building-advisory-board-of-appeals",
            ),
        ]
        for pb in scottsdale_boards:
            existing = session.execute(
                select(PublicBody).where(
                    PublicBody.jurisdiction_id == scottsdale.id,
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


def _normalize_existing_meeting_dates(engine=None):
    """Backfill: normalize Chandler and Mesa legacy dates to YYYY-MM-DD.

    Chandler stores "September 9, 2025" (month-name format).
    Mesa stores "9/9/2025" (M/D/YYYY format).
    Fix both to ISO 8601.
    """
    from scraper.io_utils import _normalize_text_date, normalize_meeting_date
    if engine is None:
        engine = get_engine()
    session = Session(engine)
    try:
        # Fix Chandler: month-name dates
        rows = session.execute(
            text("SELECT id, meeting_date FROM meetings WHERE meeting_date LIKE '% %'")
        ).fetchall()
        fixed = 0
        for row_id, raw in rows:
            normalized = _normalize_text_date(raw)
            if normalized:
                session.execute(
                    text("UPDATE meetings SET meeting_date = :norm WHERE id = :rid"),
                    {"norm": normalized, "rid": row_id}
                )
                fixed += 1
        if fixed:
            session.commit()
            log.info("Normalized %d Chandler-style meeting dates", fixed)

        # Fix Mesa: M/D/YYYY dates
        rows2 = session.execute(
            text("SELECT id, meeting_date FROM meetings WHERE meeting_date LIKE '%/%'")
        ).fetchall()
        fixed2 = 0
        for row_id, raw in rows2:
            normalized = normalize_meeting_date(raw)
            if normalized:
                session.execute(
                    text("UPDATE meetings SET meeting_date = :norm WHERE id = :rid"),
                    {"norm": normalized, "rid": row_id}
                )
                fixed2 += 1
        if fixed2:
            session.commit()
            log.info("Normalized %d Mesa-style meeting dates", fixed2)

        return fixed + fixed2
    finally:
        session.close()


