#!/usr/bin/env python3
"""
event_link.py — Link meeting_events to entity graph via event_participants.

Step 3 of the Phase 5 pipeline. Reads canonical meeting_events plus their
extraction raw_text, extracts entity names, matches them to known entities,
infers roles, and writes event_participants records.

Strategy:
    Strategy A — extract name-like tokens from extraction raw_text using patterns
    Strategy B — match extracted names against entities.normalized_name (fuzzy)
    Strategy C — also link through agenda_items via shared meeting context

Usage:
    PYTHONPATH=scripts python3 scripts/entities/event_link.py
    PYTHONPATH=scripts python3 scripts/entities/event_link.py --dry-run
    PYTHONPATH=scripts python3 scripts/entities/event_link.py --limit 1000
    PYTHONPATH=scripts python3 scripts/entities/event_link.py --reprocess
"""

import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db import get_engine
from sqlalchemy import text

log = logging.getLogger("event_link")

BATCH_SIZE = 500
LINKER_VERSION = "2026-07-27.1"

# ── Name extraction patterns ─────────────────────────────────────────────
#
# Extraction raw_text is extracted from Meeting Result PDFs (via pdftotext).
# It typically contains lines like:
#
#   "APPROVED      2.   Review and approval of items..."
#   "DISCUSSED     4.    ...                        Name, Title"
#   "– Heather Ross, Chair"
#   "Denied    10. Application #: ZA-101-26-5  ...  Applicant: Harminder Singh"
#   "For information, please call Crystal Rosa-Duran, Admin. Assistant"
#
# Person names typically appear as:
#   - "– Name, Title" at end of item line
#   - "Name, Title" as standalone at end of action text
#   - "Applicant: Name" in zoning items
#   - "please call Name, Title" as staff contact
#   - Two CamelCase words separated by whitespace near end of line

# Pattern A: Dash-introduced name with optional title
#   "– Nicole Anderson, MCDI Chair"
#   "– Heather Ross, Chair"
#   "- Name"
DASH_NAME_ROLE_RE = re.compile(
    r"[–-]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"  # Name: First Last or First M Last
    r"(?:,\s*([A-Z][A-Za-z\s.'&-]+))?",           # Optional title
)

# Pattern B: "Applicant: Name"  or "Applicant: Name, Title"
APPLICANT_RE = re.compile(
    r"Applicant:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"(?:,\s*([A-Za-z][A-Za-z\s.'&-]+))?",
)

# Pattern C: "please call Name, Title" (staff contact)
STAFF_CALL_RE = re.compile(
    r"please\s+call\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"(?:,\s*([A-Za-z][A-Za-z\s.'&-]+))?",
    re.IGNORECASE,
)

# Pattern D: "Presented by Name" / "Presentation by Name"
PRESENTED_BY_RE = re.compile(
    r"(?:Presented|Presentation)\s+by\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
    re.IGNORECASE,
)

# Pattern E: End-of-line name — the most common pattern in extraction texts
#   "...Trails/Heat Update                  Jarod Rogers"
#   "...Park Steward Update                                Josh Parnell"
#   "JUNE 16, 2026               Announcement of future meeting...                     Board"
EOL_NAME_RE = re.compile(
    r"\s{5,}([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$",
    re.MULTILINE,
)

# Pattern F: Role line — "Name, Title" at line start after indent
#   "Carrie Brown, Interim Director"
#   "Debra Larson, Vice-Chair"
LINE_ROLE_RE = re.compile(
    r"(?:^|\n)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r",\s*(Chair|Vice Chair|Vice-Chair|Director|Interim Director|"
    r"Deputy Director|Committee Chair|Team Leader|Manager|"
    r"Administrative Assistant|Commissioner|President|Secretary|Treasurer|"
    r"Member|Board Member|Subcommittee Chair|"
    r"Assistant Director|Planning Director|Development Director|"
    r"Project Manager|Senior Planner|Planner|Principal Planner)",
)

# Pattern G: "For Information: Name – Role"  (Phoenix meeting docs)
INFO_NAME_RE = re.compile(
    r"For\s+(?:Information|Discussion|Action):\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"(?:\s+[–-]\s+([A-Za-z][A-Za-z\s.'&-]+))?",
    re.IGNORECASE,
)


# ── Role inference ───────────────────────────────────────────────────────

def infer_role(pattern: str, title: str | None = None) -> str:
    """Map extraction pattern + title to canonical role."""
    if pattern == "applicant":
        return "applicant"
    if pattern == "staff_call":
        return "staff"

    if title:
        tl = title.lower().strip()
        if "chair" in tl:
            return "chair"
        if "vice" in tl:
            return "vice_chair"
        if "commissioner" in tl:
            return "commissioner"
        if "member" in tl:
            return "board_member"
        if any(w in tl for w in ("director", "manager", "planner", "leader",
                                  "assistant", "staff", "president", "secretary",
                                  "treasurer")):
            return "staff"
        if "applicant" in tl:
            return "applicant"

    # Default by pattern
    default_roles = {
        "dash_name_role": "presenter",
        "presented_by": "presenter",
        "line_role": "board_member",
        "info_name": "presenter",
        "eol": "presenter",
    }
    return default_roles.get(pattern, "participant")


# ── Name normalization ───────────────────────────────────────────────────

def normalize_name(raw: str) -> str:
    """Normalize a name for entity lookup."""
    return re.sub(r'\s+', ' ', raw.strip().lower())


# ── Entity matching ──────────────────────────────────────────────────────

def load_entity_lookup(engine) -> list[dict]:
    """Load active entities."""
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, name, normalized_name, entity_type
            FROM entities
            WHERE resolution_status IS NULL OR resolution_status = 'canonical'
        """)).fetchall()
    return [
        {"id": int(r[0]), "name": str(r[1] or ""),
         "normalized_name": str(r[2] or ""),
         "entity_type": str(r[3] or "")}
        for r in rows
    ]


def match_entity(name: str, entities: list[dict]) -> list[tuple]:
    """Match extracted name against entities. Returns [(entity, confidence, method)]."""
    norm = normalize_name(name)
    if not norm or len(norm) < 3:
        return []

    matches = []
    for ent in entities:
        en = ent["normalized_name"]
        if not en or len(en) < 3:
            continue

        # Exact match
        if norm == en:
            matches.append((ent, 0.95, "exact"))
            continue

        # Name is contained in entity name or vice versa
        if norm in en or en in norm:
            shorter = min(len(norm), len(en))
            longer = max(len(norm), len(en))
            ratio = shorter / longer
            if ratio >= 0.6:
                matches.append((ent, 0.7 * ratio, "contained"))
                continue

        # Word overlap (at least 2 significant words)
        nw = {w for w in norm.split() if len(w) > 2}
        ew = {w for w in en.split() if len(w) > 2}
        overlap = nw & ew
        if len(overlap) >= 2:
            score = 0.6 * len(overlap) / max(len(nw | ew), 1)
            matches.append((ent, score, "partial"))

    # Deduplicate by entity_id, keep highest confidence
    best = {}
    for ent, conf, method in matches:
        if ent["id"] not in best or conf > best[ent["id"]][1]:
            best[ent["id"]] = (ent, conf, method)

    return sorted(best.values(), key=lambda x: x[1], reverse=True)


# ── Name extraction ──────────────────────────────────────────────────────

def extract_names(raw_text: str) -> list[dict]:
    """Extract entity names from extraction raw_text.

    Returns list of dicts: name, role, pattern, title, confidence.
    """
    results = []
    seen = set()  # (normalized_name, role) dedup

    def add(name: str, role: str, pattern: str, title: str | None, confidence: float):
        key = (normalize_name(name), role)
        if key not in seen and len(name) > 3:
            seen.add(key)
            results.append({
                "name": name, "role": role, "pattern": pattern,
                "title": title, "confidence": confidence,
            })

    # Pattern A: Dash-introduced names
    for m in DASH_NAME_ROLE_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, infer_role("dash_name_role", title), "dash_name_role", title, 0.8)

    # Pattern B: Applicant
    for m in APPLICANT_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, "applicant", "applicant", title, 0.95)

    # Pattern C: Staff call
    for m in STAFF_CALL_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, "staff", "staff_call", title, 0.9)

    # Pattern D: Presented by
    for m in PRESENTED_BY_RE.finditer(raw_text):
        add(m.group(1).strip(), "presenter", "presented_by", None, 0.85)

    # Pattern E: End-of-line names
    for m in EOL_NAME_RE.finditer(raw_text):
        name = m.group(1).strip()
        # Skip if it looks like a month name, day, or number
        if name.split()[0] in ("January", "February", "March", "April", "May", "June",
                                "July", "August", "September", "October", "November",
                                "December", "Monday", "Tuesday", "Wednesday", "Thursday",
                                "Friday", "Saturday", "Sunday"):
            continue
        add(name, "presenter", "eol", None, 0.6)

    # Pattern F: Line-start role lines
    for m in LINE_ROLE_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, infer_role("line_role", title), "line_role", title, 0.75)

    # Pattern G: For Information/Name-Role
    for m in INFO_NAME_RE.finditer(raw_text):
        name = m.group(1).strip()
        title = m.group(2).strip() if m.lastindex and m.group(2) else None
        add(name, infer_role("info_name", title), "info_name", title, 0.7)

    return results


# ── Strategy B: Cross-reference through meetings ─────────────────────────

def load_meeting_entity_lookup(engine) -> dict:
    """Build lookup: meeting_id -> [(entity_id, entity_name, role)] from entity_mentions.

    Uses entity_mentions.source_type/source_id to find entities linked to
    agenda_items and supporting_documents for each meeting.
    """
    lookup = {}

    # Get entity mentions for agenda_items (which have meeting_id)
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT ai.meeting_id, em.entity_id, e.name, e.normalized_name,
                   em.role_in_context
            FROM entity_mentions em
            JOIN entities e ON e.id = em.entity_id
            JOIN agenda_items ai ON ai.id = em.source_id
            WHERE em.source_type = 'agenda_item'
              AND (e.resolution_status IS NULL OR e.resolution_status = 'canonical')
        """)).fetchall()

    for r in rows:
        meeting_id = str(r[0] or "")
        entity_id = int(r[1])
        entity_name = str(r[2] or "")
        norm_name = str(r[3] or "")
        role = str(r[4] or "") or "participant"

        if meeting_id not in lookup:
            lookup[meeting_id] = {}
        if entity_id not in lookup[meeting_id]:
            lookup[meeting_id][entity_id] = {
                "id": entity_id, "name": entity_name,
                "normalized_name": norm_name, "role": role,
            }

    # Also get from supporting_documents
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT sd.meeting_id, em.entity_id, e.name, e.normalized_name,
                   em.role_in_context
            FROM entity_mentions em
            JOIN entities e ON e.id = em.entity_id
            JOIN supporting_documents sd ON sd.id = em.source_id
            WHERE em.source_type = 'supporting_document'
              AND sd.meeting_id IS NOT NULL
              AND (e.resolution_status IS NULL OR e.resolution_status = 'canonical')
        """)).fetchall()

    for r in rows:
        meeting_id = str(r[0] or "")
        if not meeting_id:
            continue
        entity_id = int(r[1])
        entity_name = str(r[2] or "")
        norm_name = str(r[3] or "")
        role = str(r[4] or "") or "participant"

        if meeting_id not in lookup:
            lookup[meeting_id] = {}
        if entity_id not in lookup[meeting_id]:
            lookup[meeting_id][entity_id] = {
                "id": entity_id, "name": entity_name,
                "normalized_name": norm_name, "role": role,
            }

    return lookup


# ── Main processing ──────────────────────────────────────────────────────

def link_events(engine, entity_lookup: list[dict],
                meeting_entity_lookup: dict,
                limit: int = None, dry_run: bool = False,
                reprocess: bool = False) -> dict:
    """Process meeting_events and write event_participants."""
    stats = {"events_processed": 0, "names_from_text": 0, "matched_via_text": 0,
             "names_via_meeting": 0, "matched_via_meeting": 0,
             "participants_written": 0, "errors": 0}
    done = False
    cursor_id = 0  # Cursor-based pagination: last processed event ID

    while not done:
        with engine.connect() as c:
            rows = c.execute(text("""
                SELECT e.id, e.meeting_id, e.outcome, e.case_number,
                       ee.raw_text, et.slug AS event_type_slug,
                       e.supporting_doc_id
                FROM meeting_events e
                JOIN meeting_event_extractions ee ON ee.meeting_event_id = e.id
                JOIN meeting_event_types et ON et.id = e.event_type_id
                WHERE e.id > :cursor
                ORDER BY e.id
                LIMIT :limit
            """), {"cursor": cursor_id, "limit": BATCH_SIZE}).fetchall()

        if not rows:
            break

        batch_participants = []

        for row in rows:
            event_id = int(row[0])
            meeting_id = str(row[1] or "")
            raw_text = str(row[4] or "")
            cursor_id = event_id
            stats["events_processed"] += 1

            if not raw_text or len(raw_text) < 20:
                continue

            # Strategy A: Extract names from raw text
            names = extract_names(raw_text)
            stats["names_from_text"] += len(names)

            matched_pairs = set()  # (entity_id, role) already written

            for ni in names:
                matches = match_entity(ni["name"], entity_lookup)
                for ent, conf, method in matches:
                    pair = (ent["id"], ni["role"])
                    if pair not in matched_pairs:
                        batch_participants.append((event_id, ent["id"], ni["role"], conf))
                        matched_pairs.add(pair)
                        stats["matched_via_text"] += 1

            # Strategy C: Cross-reference through meeting context
            if meeting_id and meeting_id in meeting_entity_lookup:
                for eid, info in meeting_entity_lookup[meeting_id].items():
                    pair = (eid, info["role"])
                    if pair not in matched_pairs:
                        batch_participants.append((event_id, eid, info["role"], 0.5))
                        matched_pairs.add(pair)
                        stats["names_via_meeting"] += 1
                        stats["matched_via_meeting"] += 1

        # Write batch
        if batch_participants and not dry_run:
            with engine.begin() as c:
                raw_conn = c.connection
                if hasattr(raw_conn, 'driver_connection'):
                    pg_conn = raw_conn.driver_connection
                else:
                    pg_conn = raw_conn

                from psycopg2.extras import execute_values

                execute_values(
                    pg_conn.cursor(),
                    """
                        INSERT INTO event_participants
                            (meeting_event_id, entity_id, role_in_event, confidence)
                        VALUES %s
                        ON CONFLICT (meeting_event_id, entity_id, role_in_event)
                        DO UPDATE SET
                            confidence = GREATEST(event_participants.confidence, EXCLUDED.confidence)
                    """,
                    batch_participants,
                    template="(%s, %s, %s, %s)",
                )

            stats["participants_written"] += len(batch_participants)

        if len(rows) < BATCH_SIZE:
            break
        if limit and stats["events_processed"] >= limit:
            break
        if stats["events_processed"] % 500 == 0:
            log.info("  Progress: %d events, %d participants written",
                     stats["events_processed"], stats["participants_written"])

    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 5 entity linker")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reprocess", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.dry_run else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    engine = get_engine()

    # Load entity lookup
    log.info("Loading entity lookup...")
    entities = load_entity_lookup(engine)
    log.info("Loaded %d active entities", len(entities))

    orgs = [e for e in entities if e["entity_type"] in ("organization", "developer",
                                                         "planning_firm", "law_firm")]
    people = [e for e in entities if e["entity_type"] == "person"]
    log.info("  %d organizations/firms, %d people", len(orgs), len(people))

    # Load meeting→entity cross-reference
    log.info("Loading meeting→entity cross-reference...")
    meeting_entities = load_meeting_entity_lookup(engine)
    total_linked = sum(len(v) for v in meeting_entities.values())
    log.info("  %d meetings with entity mentions, %d total entity links",
             len(meeting_entities), total_linked)

    # Count pending
    if not args.reprocess:
        with engine.connect() as c:
            pending = c.execute(text("""
                SELECT COUNT(*) FROM meeting_events e
                WHERE NOT EXISTS (
                    SELECT 1 FROM event_participants ep
                    WHERE ep.meeting_event_id = e.id
                )
            """)).scalar()
        log.info("Events without participants: %d", pending)

    # Run
    start = time.time()
    stats = link_events(engine, entities, meeting_entities,
                        limit=args.limit, dry_run=args.dry_run,
                        reprocess=args.reprocess)
    elapsed = time.time() - start

    mode = "DRY RUN" if args.dry_run else "DONE"
    log.info(
        "%s — %d events, %d names from text (%d matched), "
        "%d via meeting context (%d matched), "
        "%d participants written, %.1fs",
        mode, stats["events_processed"],
        stats["names_from_text"], stats["matched_via_text"],
        stats["names_via_meeting"], stats["matched_via_meeting"],
        stats["participants_written"], elapsed,
    )


if __name__ == "__main__":
    main()
