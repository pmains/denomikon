"""
Migration: Add swept_at columns for entity sweep tracking.

Adds swept_at TIMESTAMPTZ to agenda_items and supporting_documents
so the entity sweep can track per-row progress instead of relying
solely on body-level watermarks.

Safe to run multiple times (IF NOT EXISTS).
"""

import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from db.core import get_engine
from sqlalchemy import text

log = logging.getLogger("migrations_v2")


MIGRATIONS = [
    # 1. swept_at on agenda_items
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'agenda_items'
              AND column_name = 'swept_at'
        ) THEN
            ALTER TABLE agenda_items ADD COLUMN swept_at TIMESTAMPTZ;
        END IF;
    END
    $$;
    """,
    # 2. swept_at on supporting_documents
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'supporting_documents'
              AND column_name = 'swept_at'
        ) THEN
            ALTER TABLE supporting_documents ADD COLUMN swept_at TIMESTAMPTZ;
        END IF;
    END
    $$;
    """,
    # 3. Index on swept_at for fast WHERE IS NULL queries
    """
    CREATE INDEX IF NOT EXISTS ix_agenda_items_swept_at
        ON agenda_items (swept_at)
        WHERE swept_at IS NULL;
    """,
    # 4. Index on supporting_documents swept_at
    """
    CREATE INDEX IF NOT EXISTS ix_supporting_documents_swept_at
        ON supporting_documents (swept_at)
        WHERE swept_at IS NULL;
    """,
    # 5. Backfill swept_at for agenda_items that already have entity mentions
    """
    UPDATE agenda_items ai
    SET swept_at = (
        SELECT MIN(em.created_at)
        FROM entity_mentions em
        WHERE em.source_type = 'agenda_item'
          AND em.source_id = ai.id
    )
    WHERE ai.swept_at IS NULL
      AND EXISTS (
        SELECT 1 FROM entity_mentions em
        WHERE em.source_type = 'agenda_item'
          AND em.source_id = ai.id
    );
    """,
]


def run_migrations(verbose: bool = False) -> int:
    """Run all pending migrations. Returns count executed."""
    engine = get_engine()
    executed = 0

    for i, sql in enumerate(MIGRATIONS, 1):
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            if verbose:
                log.info("  ✓ migration %d/%d", i, len(MIGRATIONS))
            executed += 1
        except Exception as e:
            log.warning("  ~ migration %d/%d skipped: %s", i, len(MIGRATIONS), e)

    log.info("Migrations complete: %d/%d executed", executed, len(MIGRATIONS))
    return executed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    verbose = "--verbose" in sys.argv
    run_migrations(verbose=verbose)
