#!/usr/bin/env python3
"""
event_extractor — Three-stage event extraction pipeline.

Orchestrates:
  Step 1: event_extract.py   — Pattern extraction from Meeting Result docs
  Step 2: event_normalize.py — Normalize extractions to canonical events
  Step 3: event_link.py      — Link events to entity graph

Each step is idempotent and watermark-tracked. Unprocessed records from
previous runs are picked up automatically.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_extractor.py
    PYTHONPATH=scripts python3 scripts/entities/event_extractor.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_extractor.py --step extract
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("event_extractor")

STEPS = {
    "extract": {
        "script": "scripts/entities/event_extract.py",
        "description": "Pattern extraction from Meeting Result docs",
    },
    "normalize": {
        "script": "scripts/entities/event_normalize.py",
        "description": "Normalize extractions to canonical events",
    },
    "link": {
        "script": "scripts/entities/event_link.py",
        "description": "Link events to entity graph",
    },
}


def run_step(step_name: str, extra_args: list[str] | None = None) -> dict:
    """Run one step via subprocess. Returns elapsed time and return code."""
    step = STEPS[step_name]
    script = os.path.join(
        os.path.dirname(__file__), "..", "..", step["script"]
    )
    cmd = [sys.executable, "-u", script]
    if extra_args:
        cmd.extend(extra_args)

    log.info("Starting step '%s': %s", step_name, step["description"])
    start = time.time()

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(script))

    elapsed = time.time() - start
    for line in result.stdout.strip().split("\n"):
        if line:
            log.info("  [%s] %s", step_name, line)
    if result.stderr.strip():
        for line in result.stderr.strip().split("\n"):
            if line:
                log.warning("  [%s] %s", step_name, line)

    ok = result.returncode == 0
    status = "OK" if ok else f"FAILED (exit {result.returncode})"
    log.info("Step '%s': %s — %.1fs", step_name, status, elapsed)

    return {"step": step_name, "ok": ok, "elapsed": elapsed, "returncode": result.returncode}


def count_pending(engine) -> dict:
    """Count pending work for each step."""
    counts = {}

    # Each query in its own connection to avoid transaction poisoning
    # Step 1: supporting_docs not yet extracted
    try:
        with engine.connect() as c:
            wm = c.execute(text("SELECT COALESCE(MAX(last_doc_id), 0) FROM _event_extract_watermark")).scalar()
            r = c.execute(text("""
                SELECT COUNT(*) FROM supporting_documents
                WHERE id > :wm
                  AND document_type = 'Meeting Result'
                  AND text_content IS NOT NULL AND text_content != ''
            """), {"wm": wm}).scalar()
            counts["extract"] = r
    except Exception:
        counts["extract"] = "?"  # Table may not exist yet, watermarked not started

    # Step 2: extractions without meeting_event_id
    with engine.connect() as c:
        r = c.execute(text("""
            SELECT COUNT(*) FROM meeting_event_extractions
            WHERE meeting_event_id IS NULL
        """)).scalar()
        counts["normalize"] = r

    # Step 3: events without participants
    with engine.connect() as c:
        r = c.execute(text("""
            SELECT COUNT(*) FROM meeting_events e
            WHERE NOT EXISTS (
                SELECT 1 FROM event_participants ep
                WHERE ep.meeting_event_id = e.id
            )
        """)).scalar()
        counts["link"] = r

    return counts


def run_event_pipeline(
    engine,
    steps: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
    limit: int | None = None,
    **kwargs,
) -> dict:
    """Run event extraction pipeline. Returns structured result dict.

    Runs sub-steps sequentially: extract → normalize → link.
    Each sub-step is still called via subprocess (the sub-step scripts have
    their own complex logic and model artifacts). This function provides
    the orchestration layer as a library call instead of shelling out.
    """
    pending = count_pending(engine)
    step_results = []
    total_elapsed = 0.0

    if steps is None:
        steps = list(STEPS.keys())

    extra = []
    if limit:
        extra = ["--limit", str(limit)]

    for step_name in steps:
        result = run_step(step_name, extra_args=extra)
        step_results.append(result)
        total_elapsed += result.get("elapsed", 0)
        if not result.get("ok"):
            log.error("Pipeline failed at step '%s'", step_name)
            return {
                "success": False,
                "duration_s": round(total_elapsed, 1),
                "steps": step_results,
                "failed_at": step_name,
                "dry_run": dry_run,
            }

    return {
        "success": True,
        "duration_s": round(total_elapsed, 1),
        "steps": step_results,
        "pending": pending,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(description="Event extraction pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pending work without running")
    parser.add_argument("--step", choices=list(STEPS.keys()),
                        help="Run only this step")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit per step (passes through)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.dry_run else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    engine = get_engine()

    # Show pending work
    pending = count_pending(engine)
    log.info("Pending work:")
    for step_name in STEPS:
        log.info("  %s: %s pending", step_name, pending[step_name])

    if args.dry_run:
        log.info("Dry run — no steps executed")
        print(json.dumps({"phase": "event_pipeline", "success": True, "dry_run": True, "pending": pending, "steps": []}))
        return

    # Determine which steps to run
    steps = [args.step] if args.step else None

    result = run_event_pipeline(engine, steps=steps, dry_run=args.dry_run, limit=args.limit)

    if result["success"]:
        log.info("Pipeline complete — all steps OK, %.1fs total", result["duration_s"])
    else:
        log.error("Pipeline failed at step '%s'", result["failed_at"])

    print(json.dumps({"phase": "event_pipeline", **result}))


if __name__ == "__main__":
    main()
