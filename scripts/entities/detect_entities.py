#!/usr/bin/env python3
"""
detect_entities — Unified entity detection step for the daily pipeline.

Runs all entity pipeline phases in order:

  Phase 1: graph_builder.py     — Structured triples from DB tables
  Phase 2: sweep_docs.py        — Entity extraction from supporting_documents text
  Phase 3: pattern_cascade.py   — Semi-structured header regex on agenda items
  Phase 4: role_classifier.py   — ML role classification (6-signal XGBoost)
  Phase 5: resolver.py          — Entity resolution / dedup
  Phase 6: event_extractor.py   — 3-stage event extraction pipeline

Phases run in dependency order: entity detection (1-4), then dedup (5),
then event extraction + linking (6), which needs clean entities.


Each phase is watermark-tracked and idempotent. Phases that have already
run for today are skipped. Use --force to re-run.

Usage (normal — one step in the pipeline):
    PYTHONPATH=scripts python3 scripts/entities/detect_entities.py

Usage (debugging a specific phase):
    PYTHONPATH=scripts python3 scripts/entities/detect_entities.py --phase pattern_cascade
    PYTHONPATH=scripts python3 scripts/entities/detect_entities.py --phase resolver --verbose
    PYTHONPATH=scripts python3 scripts/entities/detect_entities.py --phase sweep_docs --verbose
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time

from sqlalchemy import text

sys.path.insert(0, "scripts")
from db.core import get_engine

log = logging.getLogger("detect_entities")
WATERMARK_TABLE = "_detect_entities_watermark"

# ── Phase definitions ──────────────────────────────────────────────────────

# Each phase specifies:
#   module_path: dotted Python path to the phase module
#   run_fn_name: name of the library function to call within that module
#   run_fn is lazily resolved by _resolve_phase_fn()

PHASES = [
    {
        "name": "graph_builder",
        "description": "Structured triples from DB tables",
        "module": "scripts.entities.graph_builder",
        "run_fn_name": "run_phase",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "sweep_docs",
        "description": "Entity extraction from supporting_documents text content",
        "module": "scripts.entities.sweep_docs",
        "run_fn_name": "run_sweep_docs",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "pattern_cascade",
        "description": "Semi-structured header regex (Applicant:, Attorney:, etc.)",
        "module": "scripts.entities.pattern_cascade",
        "run_fn_name": "run_pattern_cascade",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "role_classifier",
        "description": "ML role classification — fastText on entity name + context",
        "module": "scripts.entities.role_classifier",
        "run_fn_name": "run_role_classifier",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "resolver",
        "description": "Entity resolution / dedup",
        "module": "scripts.entities.resolver",
        "run_fn_name": "run_resolver",
        "allow_skip": True,
        "critical": False,
    },
    {
        "name": "event_pipeline",
        "description": "3-stage event extraction: extract → normalize → link",
        "module": "scripts.entities.event_extractor",
        "run_fn_name": "run_event_pipeline",
        "allow_skip": True,
        "critical": False,
    },
]


def _get_watermarks(engine, force: bool = False) -> set[str]:
    """Return set of phase names that have already run."""
    if force:
        return set()
    with engine.connect() as conn:
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                    phase VARCHAR(32) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    duration_s NUMERIC(8,1) DEFAULT 0,
                    entities_created INTEGER DEFAULT 0,
                    edges_created INTEGER DEFAULT 0
                )
            """)
        )
        # Also ensure resolver has its watermark table
        conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS _resolver_watermark (
                    phase VARCHAR(32) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        )
        conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS _graph_builder_watermark (
                    source_name VARCHAR(64) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    entities_created INTEGER NOT NULL DEFAULT 0,
                    edges_created INTEGER NOT NULL DEFAULT 0
                )
            """)
        )
        conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS _pattern_cascade_watermark (
                    body VARCHAR(64) PRIMARY KEY,
                    last_run_at TIMESTAMPTZ DEFAULT now(),
                    last_processed_id INTEGER DEFAULT 0,
                    items_processed INTEGER DEFAULT 0,
                    entities_created INTEGER DEFAULT 0,
                    edges_created INTEGER DEFAULT 0
                )
            """)
        )
        rows = conn.execute(
            text(f"SELECT phase FROM {WATERMARK_TABLE}")
        ).fetchall()
        return {r[0] for r in rows}


def _mark_watermark(engine, phase: str, duration_s: float,
                    entities: int = 0, edges: int = 0) -> None:
    """Record that a phase completed."""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                INSERT INTO {WATERMARK_TABLE} (phase, last_run_at, duration_s,
                                               entities_created, edges_created)
                VALUES (:p, now(), :dur, :ec, :edc)
                ON CONFLICT (phase) DO UPDATE SET
                    last_run_at = now(),
                    duration_s = :dur,
                    entities_created = :ec,
                    edges_created = :edc
            """),
            {"p": phase, "dur": duration_s, "ec": entities, "edc": edges},
        )


_PHASE_CACHE: dict[str, object] = {}


def _resolve_phase_fn(phase: dict) -> object | None:
    """Lazily import a phase module and return its run function."""
    module_path = phase.get("module")
    fn_name = phase.get("run_fn_name")
    if not module_path or not fn_name:
        log.warning("  [NOT IMPLEMENTED] %s — no module/run_fn_name", phase["description"])
        return None
    cache_key = f"{module_path}:{fn_name}"
    if cache_key in _PHASE_CACHE:
        return _PHASE_CACHE[cache_key]
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name, None)
        if fn is None:
            log.warning("  [MISSING] %s — function %s not found in %s",
                        phase["description"], fn_name, module_path)
            return None
        _PHASE_CACHE[cache_key] = fn
        return fn
    except ImportError as e:
        log.warning("  [MISSING] %s — import error: %s", phase["description"], e)
        return None


def _run_phase(phase: dict, engine, args) -> dict:
    """Execute one phase as a direct library call. Returns stats dict."""
    run_fn = _resolve_phase_fn(phase)
    if run_fn is None:
        return {"skipped": True}

    log.info("  → %s.%s()", phase["module"], phase["run_fn_name"])
    start = time.time()

    try:
        result = run_fn(
            engine,
            dry_run=args.dry_run,
            force=args.force,
            verbose=args.verbose,
        )
    except Exception as e:
        elapsed = time.time() - start
        log.error("  ✗ %s (%.1fs): %s", phase["description"], elapsed, e)
        return {
            "success": False,
            "duration_s": round(elapsed, 1),
            "entities_created": 0,
            "edges_created": 0,
            "error": str(e),
        }

    elapsed = time.time() - start
    success = result.get("success", True)
    status = "✓" if success else "✗"
    entities_created = result.get("entities_created", 0)
    edges_created = result.get("edges_created", 0)

    log.info("  %s %s (%.1fs)", status, phase["description"], elapsed)

    return {
        "success": success,
        "duration_s": round(elapsed, 1),
        "entities_created": entities_created,
        "edges_created": edges_created,
        "raw_result": result,
    }


def run_detection(engine, phases: list[str] | None = None,
                  dry_run: bool = False, force: bool = False,
                  verbose: bool = False) -> dict:
    """Run the entity detection pipeline. Returns summary dict."""
    completed = set()
    if not force:
        completed = _get_watermarks(engine)

    results = {
        "phases": [],
        "total_duration_s": 0,
        "total_entities": 0,
        "total_edges": 0,
        "errors": [],
    }

    for phase in PHASES:
        if phases and phase["name"] not in phases:
            continue

        if phase["name"] in completed and phase.get("allow_skip", True):
            log.info("  [SKIP] %s — already completed", phase["description"])
            results["phases"].append({
                "name": phase["name"],
                "status": "skipped",
            })
            continue

        phase_result = _run_phase(phase, engine, argparse.Namespace(
            dry_run=dry_run, force=force, verbose=verbose,
        ))

        if phase_result.get("skipped"):
            results["phases"].append({
                "name": phase["name"],
                "status": "skipped",
                "error": phase_result.get("error"),
            })
            continue

        results["phases"].append({
            "name": phase["name"],
            "status": "ok" if phase_result["success"] else "failed",
            "duration_s": phase_result["duration_s"],
            "entities_created": phase_result["entities_created"],
            "edges_created": phase_result["edges_created"],
        })
        results["total_duration_s"] += phase_result["duration_s"]
        results["total_entities"] += phase_result["entities_created"]
        results["total_edges"] += phase_result["edges_created"]

        if not phase_result["success"] and phase.get("critical"):
            log.error("  Aborting — critical phase %s failed", phase["name"])
            results["errors"].append({
                "phase": phase["name"],
                "error": "critical phase failed",
                "stderr": phase_result.get("stderr", ""),
            })
            break

        # Record watermark
        if phase_result["success"] and not dry_run:
            _mark_watermark(engine, phase["name"],
                            phase_result["duration_s"],
                            phase_result["entities_created"],
                            phase_result["edges_created"])

    return results


def main():
    parser = argparse.ArgumentParser(description="Unified entity detection pipeline")
    parser.add_argument("--phase", type=str, help="Run only one phase (name)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--force", action="store_true", help="Force re-run all phases")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    phases = [args.phase] if args.phase else None
    engine = get_engine()
    results = run_detection(engine, phases=phases,
                            dry_run=args.dry_run, force=args.force,
                            verbose=args.verbose)

    # Summary
    dry = " (DRY RUN)" if args.dry_run else ""
    ran = [p for p in results["phases"] if p["status"] not in ("skipped",)]
    skipped = [p for p in results["phases"] if p["status"] == "skipped"]
    failed = [p for p in results["phases"] if p["status"] == "failed"]

    parts = [f"{len(ran)} phase(s) ran"]
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    if results["total_entities"]:
        parts.append(f"{results['total_entities']} entities")
    if results["total_edges"]:
        parts.append(f"{results['total_edges']} edges")
    parts.append(f"{results['total_duration_s']:.0f}s")

    log.info("DONE%s — %s", dry, " | ".join(parts))

    if failed:
        log.error("Failed phases: %s", ", ".join(p["name"] for p in failed))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
