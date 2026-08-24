#!/usr/bin/env python3
"""
resweep_docs.py — Re-sweep all supporting documents with fixed sweep_docs logic.

Resets swept_at in batches (1000 at a time), then runs sweep_docs on each batch.
Avoids the slow single-UPDATE on 62K rows over Tailscale.

Usage:
  DATABASE_URL=postgresql://... python scripts/entities/resweep_docs.py
  DATABASE_URL=postgresql://... python scripts/entities/resweep_docs.py --dry-run
  DATABASE_URL=postgresql://... python scripts/entities/resweep_docs.py --limit 5000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.entities.sweep_docs import run_sweep_docs

log = logging.getLogger("resweep_docs")
RESET_BATCH = 1000


def reset_swept_batch(engine, min_id: int, max_id: int) -> int:
    """Reset swept_at for a range of doc IDs. Returns count reset."""
    from sqlalchemy import text
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE supporting_documents SET swept_at = NULL
            WHERE id BETWEEN :min_id AND :max_id
              AND text_content IS NOT NULL AND text_content != ''
              AND swept_at IS NOT NULL
        """), {"min_id": min_id, "max_id": max_id})
        return result.rowcount


def main():
    parser = argparse.ArgumentParser(description="Re-sweep all supporting documents")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    parser.add_argument("--limit", type=int, default=None, help="Max docs to reset")
    parser.add_argument("--batch-size", type=int, default=200, help="sweep_docs batch size")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    from sqlalchemy import create_engine, text
    engine = create_engine(database_url)

    # Get ID range
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT MIN(id), MAX(id) FROM supporting_documents
            WHERE text_content IS NOT NULL AND text_content != ''
        """)).fetchone()
        if not row or row[0] is None:
            log.info("No documents to re-sweep")
            return
        min_id, max_id = int(row[0]), int(row[1])
    
    total_reset = 0
    total_sweep_limit = args.limit or (max_id - min_id + 1)

    # Phase 1: Reset swept_at in batches
    batch_start = min_id
    while batch_start <= max_id and total_reset < total_sweep_limit:
        batch_end = min(batch_start + RESET_BATCH - 1, max_id)
        
        if args.dry_run:
            count = 1  # Just to show progress
            log.info("Would reset docs %d–%d", batch_start, batch_end)
        else:
            count = reset_swept_batch(engine, batch_start, batch_end)
            total_reset += count
        
        if count:
            log.info("Reset %d docs in range %d–%d (total reset: %d)",
                     count, batch_start, batch_end, total_reset)
        
        batch_start = batch_end + 1
        
        if total_reset >= total_sweep_limit:
            break

    if args.dry_run:
        log.info("Dry-run complete. Would have reset %d docs across range %d–%d",
                 total_reset or (max_id - min_id + 1), min_id, max_id)
        return

    log.info("Phase 1 complete: reset %d documents. Starting sweep...", total_reset)

    # Phase 2: Run sweep_docs
    stats = run_sweep_docs(
        engine,
        dry_run=args.dry_run,
        verbose=args.verbose,
        force=False,
        limit=None,
        batch_size=args.batch_size,
    )
    log.info("Sweep complete: %s", stats)


if __name__ == "__main__":
    main()
