#!/usr/bin/env python3
"""Migrate entity_mentions from pz_item_detail source_type to agenda_item.

Legacy mentions (Phase 1 seed data) pointed to pz_item_details rows via
source_type='pz_item_detail'. pz_item_details has a direct agenda_item_id
foreign key, so we can re-point these mentions to the actual agenda_items.
"""

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_here))

from db.core import get_engine
from sqlalchemy import text


def main():
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE entity_mentions em
                SET source_type = 'agenda_item',
                    source_id = pz.agenda_item_id
                FROM pz_item_details pz
                WHERE em.source_type = 'pz_item_detail'
                  AND pz.id = em.source_id
                  AND pz.agenda_item_id IS NOT NULL
            """)
        )
        print(f"Migrated {result.rowcount} entity_mentions from pz_item_detail → agenda_item")

    # Verify
    with engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT COUNT(*) FROM entity_mentions WHERE source_type = 'pz_item_detail'")
        ).scalar()
        print(f"Remaining pz_item_detail mentions: {remaining}")


if __name__ == "__main__":
    main()
