#!/usr/bin/env python3
"""
event_extract.py — Pattern-based event extraction from Meeting Result docs.

Reads supporting_documents (document_type='Meeting Result'), applies regex
patterns to extract candidate events, and writes to meeting_event_extractions.

Pipeline position: Step 1 — writes only to meeting_event_extractions.
The normalizer (Step 2) reads extractions and creates canonical meeting_events.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_extract.py
    PYTHONPATH=scripts python3 scripts/entities/event_extract.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_extract.py --limit 100
"""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("event_extract")

WATERMARK_TABLE = "_event_extract_watermark"
BATCH_SIZE = 50
EXTRACTOR_VERSION = "2026-07-27.1"

# ── Action verb patterns ────────────────────────────────────────────────
# Ordered by specificity (longer patterns first to avoid sub-matches)

ACTION_PATTERNS = [
    # Multi-word actions (must come before single-word)
    (r"APPROVED\s+WITH\s+STIPULATIONS",     "approved_with_conditions"),
    (r"APPROVED\s+WITH\s+CONDITIONS",        "approved_with_conditions"),
    (r"APPROVED\s+SUBJECT\s+TO",             "approved_with_conditions"),
    (r"DENIED\s+WITHOUT\s+PREJUDICE",        "denied_without_prejudice"),
    (r"RECEIVED\s+AND\s+FILED",              "received"),
    (r"CALLED\s+TO\s+ORDER",                 "called_to_order"),

    # Single-word actions
    (r"APPROVED",                            "approved"),
    (r"DENIED",                              "denied"),
    (r"CONTINUED",                           "continued"),
    (r"TABLED",                              "tabled"),
    (r"ADOPTED",                             "adopted"),
    (r"RECEIVED",                            "received"),
    (r"DISCUSSED",                           "discussed"),
    (r"WITHDRAWN",                           "withdrawn"),
    (r"INTRODUCED",                          "introduced"),
    (r"AMENDED",                             "amended"),
    (r"SUSTAINED",                           "sustained"),
    (r"VACATED",                             "vacated"),
    (r"EXTENDED",                            "extended"),
    (r"DEFERRED",                            "deferred"),

    # Discussion/status indicators
    (r"DISCUSSION\s+ONLY",                   "discussed"),
    (r"NO\s+ACTION",                         "no_action"),
    (r"NO\s+RESPONSE",                       "no_action"),
    (r"FOR\s+DISCUSSION",                    "discussed"),
    (r"PRELIMINARY\s+REVIEW",                "discussed"),
]

# Build combined pattern: groups of (full_pattern, outcome)
# We'll use a single regex with named groups via alternation
ACTION_PATTERN_PARTS = []
for pat, outcome in ACTION_PATTERNS:
    ACTION_PATTERN_PARTS.append(f"(?P<a{len(ACTION_PATTERN_PARTS)}>{pat})")

ACTION_RE = re.compile(
    "|".join(ACTION_PATTERN_PARTS),
    re.MULTILINE | re.IGNORECASE,
)

# ── Case/project number patterns ────────────────────────────────────────
CASE_RE = re.compile(
    r"(?:Z[-/\s]?\d{3,6}|"
    r"PLN\d{4,6}|"
    r"SPL\s*\d{3,6}|"
    r"V[OA]\s*\d{3,6}|"
    r"CASE\s*\d{4,9}|"
    r"Project\s*(?:Number|#|No\.?)\s*[-:.]?\s*\d{3,9}|"
    r"(?<!\w)(\d{2}-\d{4,6})(?!\w))",
    re.IGNORECASE,
)

# ── Item number pattern ────────────────────────────────────────────────
ITEM_NO_RE = re.compile(
    r"(?<!\w)(\d+)\.\s+",
)


def extract_events_from_text(doc_id: int, text_content: str) -> list[dict]:
    """Extract candidate events from a meeting result document.

    Returns list of dicts with keys: raw_text, action_verb, confidence,
    text_offset_start, text_offset_end, case_number (nullable).
    """
    events = []

    for m in ACTION_RE.finditer(text_content):
        action_verb = m.group(0).strip()
        action_start = m.start()
        action_end = m.end()

        # Determine outcome from which group matched
        outcome = None
        for idx, (pat, out) in enumerate(ACTION_PATTERNS):
            if m.group(f"a{idx}"):
                outcome = out
                break
        if not outcome:
            outcome = action_verb.lower().replace(" ", "_")

        # Grab context: from action verb to next section or ~200 chars
        context_end = min(action_end + 300, len(text_content))
        raw_text = text_content[action_start:context_end].strip()

        # Look for case/project number in the surrounding context (200 chars each side)
        context_window = text_content[
            max(0, action_start - 100):min(len(text_content), action_end + 200)
        ]
        case_match = CASE_RE.search(context_window)
        case_number = case_match.group(0) if case_match else None
        # Clean up case number
        if case_number:
            case_number = case_number.replace(" ", "").upper()

        # Confidence: higher for single-word matches near line start
        line_start = text_content.rfind("\n", 0, action_start) + 1
        if line_start == 0:
            line_start = 0
        col = action_start - line_start
        confidence = 0.9 if col < 15 else 0.7  # Near start of line = higher confidence

        events.append({
            "raw_text": raw_text[:1000],
            "action_verb": action_verb,
            "outcome": outcome,
            "confidence": confidence,
            "text_offset_start": action_start,
            "text_offset_end": action_end,
            "case_number": case_number,
        })

    return events


def process_docs(engine, limit: int = None, dry_run: bool = False) -> dict:
    """Process supporting_documents and write extractions.

    Returns stats dict.
    """
    # Get watermark
    watermark = 0
    if not dry_run:
        with engine.connect() as conn:
            try:
                row = conn.execute(
                    text(f"SELECT COALESCE(MAX(last_doc_id), 0) FROM {WATERMARK_TABLE}")
                ).scalar()
                watermark = row or 0
            except Exception:
                watermark = 0
    log.info("Watermark last_doc_id=%d (dry_run=%s)", watermark, dry_run)

    grand = {"docs": 0, "events": 0}
    done = False

    while not done:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, text_content, meeting_id, body
                    FROM supporting_documents
                    WHERE id > :wm
                      AND document_type = 'Meeting Result'
                      AND text_content IS NOT NULL AND text_content != ''
                    ORDER BY id
                    LIMIT :limit
                """),
                {"wm": watermark, "limit": BATCH_SIZE},
            ).fetchall()

        if not rows:
            break

        # Collect all events for this batch
        batch_events = []  # list of (doc_id, events_list)
        for row in rows:
            doc_id = int(row[0])
            text_content = str(row[1] or "")

            events = extract_events_from_text(doc_id, text_content)

            grand["docs"] += 1
            grand["events"] += len(events)
            batch_events.append((doc_id, events))
            watermark = doc_id

            if limit and grand["docs"] >= limit:
                done = True
                break

        # Single transaction for the batch — multi-row INSERT via executemany
        if not dry_run and batch_events:
            last_doc_id = batch_events[-1][0]
            total_events = sum(len(evts) for _, evts in batch_events)
            now_ts = datetime.now(timezone.utc)
            with engine.begin() as conn:
                raw_conn = conn.connection
                if hasattr(raw_conn, 'driver_connection'):
                    pg_conn = raw_conn.driver_connection
                else:
                    pg_conn = raw_conn
                
                # Collect all event params
                event_rows = []
                for doc_id, events_list in batch_events:
                    for ev in events_list:
                        event_rows.append((
                            EXTRACTOR_VERSION,
                            ev["raw_text"],
                            ev["confidence"],
                            now_ts,
                            doc_id,
                            ev["action_verb"],
                            ev["text_offset_start"],
                            ev["text_offset_end"],
                            ev.get("case_number"),
                        ))
                
                if event_rows:
                    # Use psycopg2.extras.execute_values for safe multi-row INSERT
                    from psycopg2.extras import execute_values
                    execute_values(
                        pg_conn.cursor(),
                        """
                            INSERT INTO meeting_event_extractions
                                (meeting_event_id, extractor, extractor_version,
                                 raw_text, confidence, created_at,
                                 supporting_doc_id, action_verb, text_offset_start,
                                 text_offset_end, case_number)
                            VALUES %s
                        """,
                        event_rows,
                        template="(NULL, 'pattern', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    )
                
                # Single watermark for the batch
                conn.execute(
                    text(
                        f"INSERT INTO {WATERMARK_TABLE} "
                        f"(last_doc_id, docs_processed, events_found, run_at) "
                        f"VALUES (:doc_id, :docs, :events, :now)"
                    ),
                    {"doc_id": last_doc_id,
                     "docs": len(batch_events),
                     "events": total_events,
                     "now": now_ts},
                )

        if grand["docs"] % 100 == 0:
            log.info("  Progress: %d docs, %d events", grand["docs"], grand["events"])

        if done:
            break

    return grand


def ensure_watermark_table(engine):
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                    id SERIAL PRIMARY KEY,
                    last_doc_id INTEGER NOT NULL DEFAULT 0,
                    docs_processed INTEGER NOT NULL DEFAULT 0,
                    events_found INTEGER NOT NULL DEFAULT 0,
                    run_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Phase 5 pattern-based event extraction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    level = logging.DEBUG if args.dry_run else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = get_engine()

    if not args.dry_run:
        ensure_watermark_table(engine)

    start = time.time()
    stats = process_docs(engine, limit=args.limit, dry_run=args.dry_run)
    elapsed = time.time() - start

    mode = "DRY RUN" if args.dry_run else "DONE"
    log.info(
        "%s — %d docs, %d events extracted, %.1fs",
        mode, stats["docs"], stats["events"], elapsed,
    )


if __name__ == "__main__":
    main()
