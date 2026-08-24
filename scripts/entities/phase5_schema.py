"""
Phase 5 schema — Meeting Events.

Tables: meeting_event_types, meeting_events, meeting_event_extractions, event_participants

Run via:
    PYTHONPATH=scripts python3 scripts/entities/phase5_schema.py
"""

import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("phase5_schema")

SCHEMA_SQL = """
-- 1. meeting_event_types — hierarchical taxonomy
CREATE TABLE IF NOT EXISTS meeting_event_types (
    id              SERIAL PRIMARY KEY,
    slug            VARCHAR(64) NOT NULL UNIQUE,
    parent_slug     VARCHAR(64) REFERENCES meeting_event_types(slug),
    event_type      VARCHAR(64) NOT NULL,
    display_name    VARCHAR(128) NOT NULL,
    description     TEXT
);

-- 2. meeting_events — canonical event facts (no extraction metadata)
CREATE TABLE IF NOT EXISTS meeting_events (
    id                  SERIAL PRIMARY KEY,
    meeting_id          VARCHAR(64) NOT NULL,
    supporting_doc_id   INTEGER REFERENCES supporting_documents(id),
    agenda_item_id      INTEGER REFERENCES agenda_items(id),
    event_type_id       INTEGER NOT NULL REFERENCES meeting_event_types(id),
    outcome             VARCHAR(64) NOT NULL,
    action_verb         VARCHAR(256) NOT NULL,
    text_offset_start   INTEGER,
    text_offset_end     INTEGER,
    case_number         VARCHAR(32),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_meeting_events_meeting ON meeting_events(meeting_id);
CREATE INDEX IF NOT EXISTS ix_meeting_events_type ON meeting_events(event_type_id);
CREATE INDEX IF NOT EXISTS ix_meeting_events_case ON meeting_events(case_number);

-- 3. meeting_event_extractions — provenance (never mutated)
CREATE TABLE IF NOT EXISTS meeting_event_extractions (
    id                  SERIAL PRIMARY KEY,
    meeting_event_id    INTEGER REFERENCES meeting_events(id) ON DELETE SET NULL,
    extractor           VARCHAR(16) NOT NULL DEFAULT 'pattern',
    extractor_version   VARCHAR(32),
    raw_text            TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_meeting_event_extractions_event
    ON meeting_event_extractions(meeting_event_id);
CREATE INDEX IF NOT EXISTS ix_meeting_event_extractions_extractor
    ON meeting_event_extractions(extractor);

-- 4. event_participants — knowledge graph edges
CREATE TABLE IF NOT EXISTS event_participants (
    meeting_event_id    INTEGER NOT NULL REFERENCES meeting_events(id) ON DELETE CASCADE,
    entity_id           INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role_in_event       VARCHAR(64) NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (meeting_event_id, entity_id, role_in_event)
);

CREATE INDEX IF NOT EXISTS ix_event_participants_entity
    ON event_participants(entity_id);
"""

TAXONOMY_SQL = """
INSERT INTO meeting_event_types (slug, parent_slug, event_type, display_name, description)
VALUES
    -- Root categories
    ('decision',        NULL,   'decision',     'Decision',     'Any decision made by a body'),
    ('legislation',     NULL,   'legislation',  'Legislation',  'Legislative actions'),
    ('administration',  NULL,   'administration','Administration','Administrative actions'),
    ('procedure',       NULL,   'procedure',    'Procedure',    'Procedural actions'),

    -- Decision sub-types
    ('decision.approval',       'decision', 'approval',     'Approval',         'Item was approved'),
    ('decision.denial',         'decision', 'denial',       'Denial',           'Item was denied'),
    ('decision.continuation',   'decision', 'continuation', 'Continuation',     'Item was continued/tabled'),

    -- Legislation sub-types
    ('legislation.adoption',    'legislation', 'adoption',   'Adoption',         'Ordinance or resolution adopted'),
    ('legislation.introduction','legislation', 'introduction','Introduction',    'Ordinance introduced'),
    ('legislation.amendment',   'legislation', 'amendment',  'Amendment',        'Existing legislation amended'),

    -- Administration sub-types
    ('administration.appointment', 'administration', 'appointment', 'Appointment', 'Person appointed to position'),
    ('administration.removal',     'administration', 'removal',     'Removal',     'Person removed from position'),
    ('administration.resignation', 'administration', 'resignation', 'Resignation', 'Resignation accepted'),

    -- Procedure sub-types
    ('procedure.discussion',         'procedure', 'discussion',      'Discussion',         'Item discussed, no action'),
    ('procedure.public_hearing',     'procedure', 'public_hearing',  'Public Hearing',     'Public hearing held'),
    ('procedure.executive_session',  'procedure', 'executive_session','Executive Session',  'Executive session entered'),
    ('procedure.receipt',            'procedure', 'receipt',         'Received',           'Report or communication received')

ON CONFLICT (slug) DO NOTHING;
"""


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = get_engine()

    # Create tables
    for stmt in SCHEMA_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            with engine.begin() as conn:
                conn.execute(text(stmt))
    log.info("Tables created: meeting_event_types, meeting_events, meeting_event_extractions, event_participants")

    # Insert taxonomy
    for stmt in TAXONOMY_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            with engine.begin() as conn:
                conn.execute(text(stmt))
    log.info("Taxonomy inserted: %d event types", 4 + 3 + 3 + 4)  # roots + leaves

    # Verify
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT slug, parent_slug, event_type FROM meeting_event_types ORDER BY slug")).fetchall()
        log.info("Event types in DB:")
        for r in rows:
            indent = "  " if r[1] else ""
            log.info("  %s%s (parent=%s)", indent, r[0], r[1] or "-")

    log.info("Phase 5 schema complete.")


if __name__ == "__main__":
    main()
