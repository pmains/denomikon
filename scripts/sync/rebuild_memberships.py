#!/usr/bin/env python3
"""
Clean up spurious body_memberships and rebuild the entity graph.

The initial migration (scripts/db/migrations.py:_migrate_membership_model)
created BodyMembership records for bodies that had no real member data,
using Chandler City Council members as filler. This polluted the entity
graph with false MEMBER_OF relationships (e.g., Chandler councilors
incorrectly listed as members of Scottsdale and Tucson bodies).

This script:
  1. Deletes body_memberships that have zero meeting attendance evidence
     (no meeting_members records linking the person to that body).
  2. Clears the graph_builder watermark so it re-processes the clean data.
  3. Re-runs graph_builder to create accurate MEMBER_OF edges with proper
     entity mention provenance.

Usage:
    PYTHONPATH=scripts python3 scripts/sync/rebuild_memberships.py
    PYTHONPATH=scripts python3 scripts/sync/rebuild_memberships.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

from sqlalchemy import text
from sqlalchemy.engine import Engine

sys.path.insert(0, "scripts")
from db import get_engine

log = logging.getLogger("rebuild_memberships")


def count_memberships_without_evidence(engine: Engine) -> int:
    """Count body_memberships with no meeting attendance evidence.

    A membership is considered suspect if the person has never attended a
    meeting of that body (no matching record in meeting_members).
    """
    with engine.connect() as connection:
        return connection.execute(text("""
            SELECT COUNT(*)::int
            FROM body_memberships bm
            JOIN public_bodies pb ON pb.id = bm.public_body_id
            WHERE NOT EXISTS (
                SELECT 1 FROM meeting_members mm
                WHERE mm.member_id = bm.person_id
                  AND mm.body = pb.body_code
            )
        """)).scalar()


def delete_suspect_memberships(engine: Engine, dry_run: bool = False) -> dict[str, int]:
    """Delete body_memberships lacking meeting evidence and clean dependent data.

    Returns a dict of counts: bad_memberships, edges_deleted, mentions_deleted.
    """
    stats: dict[str, int] = {
        "bad_memberships": 0,
        "edges_deleted": 0,
        "mentions_deleted": 0,
    }

    # Find membership IDs with no meeting attendance evidence
    with engine.connect() as connection:
        suspect_rows = connection.execute(text("""
            SELECT bm.id, bm.person_id, pb.body_code, pb.name as body_name,
                   p.name as person_name
            FROM body_memberships bm
            JOIN public_bodies pb ON pb.id = bm.public_body_id
            JOIN persons p ON p.id = bm.person_id
            WHERE NOT EXISTS (
                SELECT 1 FROM meeting_members mm
                WHERE mm.member_id = bm.person_id
                  AND mm.body = pb.body_code
            )
            ORDER BY bm.id
        """)).fetchall()

    stats["bad_memberships"] = len(suspect_rows)
    if not suspect_rows:
        log.info("No suspect memberships found.")
        return stats

    log.info("Found %d suspect body_memberships:", len(suspect_rows))
    for row in suspect_rows[:10]:
        log.info("  #%d %s → %s (%s)", row[0], row[4], row[3], row[2])
    if len(suspect_rows) > 10:
        log.info("  ... and %d more", len(suspect_rows) - 10)

    suspect_ids = [int(r[0]) for r in suspect_rows]

    if dry_run:
        return stats

    # Delete entity_mentions that reference these memberships
    with engine.begin() as connection:
        delete_result = connection.execute(
            text(
                "DELETE FROM entity_mentions "
                "WHERE source_type = 'body_membership' "
                "AND source_id = ANY(:membership_ids)"
            ),
            {"membership_ids": suspect_ids},
        )
        stats["mentions_deleted"] = delete_result.rowcount
        log.info("Deleted %d entity_mentions", delete_result.rowcount)

    # Delete entity_relationships that reference these memberships
    with engine.begin() as connection:
        delete_result = connection.execute(
            text(
                "DELETE FROM entity_relationships "
                "WHERE provenance_type = 'body_membership' "
                "AND provenance_id = ANY(:membership_ids)"
            ),
            {"membership_ids": suspect_ids},
        )
        stats["edges_deleted"] = delete_result.rowcount
        log.info("Deleted %d entity_relationships", delete_result.rowcount)

    # Delete the body_membership records themselves
    with engine.begin() as connection:
        delete_result = connection.execute(
            text("DELETE FROM body_memberships WHERE id = ANY(:membership_ids)"),
            {"membership_ids": suspect_ids},
        )
        log.info("Deleted %d body_memberships", delete_result.rowcount)

    return stats


def reset_graph_builder_watermark(engine: Engine) -> None:
    """Remove the body_memberships watermark so graph_builder re-processes it."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM _graph_builder_watermark "
                "WHERE source_name = 'body_memberships'"
            )
        )
    log.info("Reset graph_builder watermark for body_memberships")


def run_graph_builder(engine: Engine, dry_run: bool = False) -> dict[str, object]:
    """Execute graph_builder.py for the body_memberships source only.

    Runs as a subprocess so it has a clean SQLAlchemy session and
    watermark state. Returns a dict with ok, elapsed, and returncode keys.
    """
    import os
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "entities", "graph_builder.py"
    )
    command = [sys.executable, "-u", script_path, "--source", "body_memberships"]
    if dry_run:
        command.append("--dry-run")

    log.info("Running graph_builder for body_memberships...")
    start_time = time.time()
    subprocess_result = subprocess.run(
        command, capture_output=True, text=True, cwd=os.path.dirname(script_path),
    )
    elapsed_seconds = time.time() - start_time

    for line in subprocess_result.stdout.strip().split("\n"):
        if line:
            log.info("  %s", line)
    if subprocess_result.stderr.strip():
        for line in subprocess_result.stderr.strip().split("\n"):
            if line:
                log.warning("  %s", line)

    completed_ok = subprocess_result.returncode == 0
    return {
        "ok": completed_ok,
        "elapsed": elapsed_seconds,
        "returncode": subprocess_result.returncode,
    }


def verify_state(engine: Engine) -> None:
    """Log post-cleanup counts for body_memberships, edges, and mentions."""
    with engine.connect() as connection:
        membership_count = connection.execute(
            text("SELECT COUNT(*) FROM body_memberships")
        ).scalar()
        edge_count = connection.execute(
            text("""
                SELECT COUNT(*) FROM entity_relationships
                WHERE provenance_type = 'body_membership'
            """)
        ).scalar()
        mention_count = connection.execute(
            text("""
                SELECT COUNT(*) FROM entity_mentions
                WHERE source_type = 'body_membership'
            """)
        ).scalar()
        log.info(
            "body_memberships: %d | MEMBER_OF edges: %d | mentions: %d",
            membership_count, edge_count, mention_count,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean suspect body_memberships and rebuild entity graph"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report only, no writes",
    )
    parsed_args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    engine = get_engine()

    # Step 1: Count suspect memberships
    suspect_count = count_memberships_without_evidence(engine)
    if suspect_count == 0:
        log.info("No suspect memberships found. Nothing to clean.")
        return

    log.info("=== Step 1: Deleting %d suspect body_memberships ===", suspect_count)
    cleanup_stats = delete_suspect_memberships(engine, dry_run=parsed_args.dry_run)
    if parsed_args.dry_run:
        log.info(
            "DRY RUN: would delete %d memberships, %d edges, %d mentions",
            cleanup_stats["bad_memberships"],
            cleanup_stats["edges_deleted"],
            cleanup_stats["mentions_deleted"],
        )
        return

    # Step 2: Reset watermark so graph_builder re-processes
    log.info("\n=== Step 2: Resetting graph_builder watermark ===")
    reset_graph_builder_watermark(engine)

    # Step 3: Re-run graph_builder on clean data
    log.info("\n=== Step 3: Re-running graph_builder ===")
    builder_result = run_graph_builder(engine)
    if builder_result["ok"]:
        log.info("graph_builder complete (%.1fs)", builder_result["elapsed"])
    else:
        log.error(
            "graph_builder failed (exit %d)", builder_result["returncode"]
        )

    # Step 4: Verify final state
    log.info("\n=== Step 4: Verify ===")
    verify_state(engine)


if __name__ == "__main__":
    main()
