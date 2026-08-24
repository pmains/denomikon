#!/usr/bin/env python3
"""
Entity detection backfill — one-time batch to populate entity graph
from all existing agenda items, pz_item_details, and supporting documents.

Runs as a standalone batch (not part of daily sync).

Usage:
    PYTHONPATH=scripts python3 scripts/entities/backfill.py
    PYTHONPATH=scripts python3 scripts/entities/backfill.py --limit=1000
    PYTHONPATH=scripts python3 scripts/entities/backfill.py --batch-size=200
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db.core import get_engine
from db.models import Entity, EntityMention, Base
from sqlalchemy import text
from sqlalchemy.orm import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("entity_backfill")


# ── Imports ────────────────────────────────────────────────────────────

# We import these for their data, using our own DB helpers for speed
from entities.extract import (
    KNOWN_ORGANIZATIONS,
    normalize_name,
)
from entities.people import looks_like_person
from entities.parcels import extract_apns, extract_addresses
from entities.cases import extract_all_case_numbers

# ── Batching ───────────────────────────────────────────────────────────

BATCH_SIZE = 500  # Items per transaction


def _get_or_create_entity(
    session: Session,
    name: str,
    entity_type: str,
    is_government: bool = False,
) -> int:
    """Find or create an entity within an existing session transaction."""
    norm = normalize_name(name)
    existing = session.query(Entity).filter(
        Entity.normalized_name == norm
    ).first()
    if existing:
        existing.last_seen_at = text("NOW()")
        existing.mention_count = (existing.mention_count or 0) + 1
        return existing.id

    entity = Entity(
        entity_type=entity_type,
        name=name[:254],
        normalized_name=norm[:254],
        is_government=is_government,
        mention_count=1,
    )
    session.add(entity)
    session.flush()  # Get ID
    return entity.id


def _create_mention(
    session: Session,
    entity_id: int,
    source_type: str,
    source_id: int,
    mention_text: str,
    context_snippet: Optional[str] = None,
    confidence: int = 80,
    extracted_by: str = "regex",
    role_in_context: Optional[str] = None,
):
    """Create a mention record within an existing session transaction."""
    # Check for duplicate in this batch
    existing = session.query(EntityMention).filter(
        EntityMention.entity_id == entity_id,
        EntityMention.source_type == source_type,
        EntityMention.source_id == source_id,
        EntityMention.role_in_context == role_in_context,
    ).first()
    if existing:
        return

    mention = EntityMention(
        entity_id=entity_id,
        source_type=source_type,
        source_id=source_id,
        mention_text=mention_text[:254],
        context_snippet=context_snippet[:1000] if context_snippet else None,
        confidence=confidence,
        extracted_by=extracted_by,
        role_in_context=role_in_context,
    )
    session.add(mention)


# ── Known organization patterns ──

def _build_org_pattern():
    """Build a compiled regex for scanning known org names."""
    known_names = list(KNOWN_ORGANIZATIONS.keys())
    known_names.sort(key=len, reverse=True)
    return re.compile(
        "(" + "|".join(re.escape(n) for n in known_names) + ")",
        re.IGNORECASE,
    )


ORG_PATTERN = _build_org_pattern()

# ── Phase 1: Seed known orgs ──

def seed_known_organizations(
    session: Session,
):
    """Ensure all KNOWN_ORGANIZATIONS exist as entity records."""
    for name, etype in KNOWN_ORGANIZATIONS.items():
        _get_or_create_entity(
            session, name, etype, is_government=(etype == "government"),
        )


# ── Phase 2: pz_item_details ──

def process_pz_items(
    session: Session,
    engine,
    limit: Optional[int] = None,
):
    """Process pz_item_details rows for case numbers, applicants, presenters.

    Returns count of rows processed.
    """
    query = """
        SELECT p.id, p.case_number, p.applicant, p.presented_by
        FROM pz_item_details p
        ORDER BY p.id
    """
    params = {}
    if limit:
        query += " LIMIT :limit"
        params["limit"] = limit

    rows = session.execute(text(query), params).fetchall()
    log.info("Processing %d pz_item_details rows...", len(rows))

    processed = 0
    for row in rows:
        pid, case_number, applicant, presented_by = row

        # ── Case entity ──
        if case_number:
            case_norm = case_number.upper().strip()
            eid = _get_or_create_entity(session, case_norm, "case")
            _create_mention(
                session, eid, "pz_item_detail", pid,
                case_norm, confidence=95, role_in_context="case_number",
            )

        # ── Applicant field ──
        if applicant and applicant.strip().lower() not in (
            "n/a", "staff-initiated", "commission-initiated", "",
        ):
            parts = [p.strip() for p in applicant.replace("–", ",").split(",")]
            if len(parts) >= 2 and len(parts[-1]) > 4:
                person_name = parts[0]
                firm_name = ",".join(parts[1:]).strip()

                # Check if firm is a known org
                firm_matched = False
                for known_name, etype in KNOWN_ORGANIZATIONS.items():
                    if normalize_name(firm_name) == normalize_name(known_name):
                        feid = _get_or_create_entity(session, known_name, etype)
                        _create_mention(
                            session, feid, "pz_item_detail", pid,
                            known_name, confidence=90, role_in_context="firm",
                        )
                        firm_matched = True
                        break

                if not firm_matched:
                    feid = _get_or_create_entity(session, firm_name, "organization")
                    _create_mention(
                        session, feid, "pz_item_detail", pid,
                        firm_name, confidence=90, role_in_context="firm",
                    )

                if person_name and looks_like_person(person_name):
                    peid = _get_or_create_entity(session, person_name, "person")
                    _create_mention(
                        session, peid, "pz_item_detail", pid,
                        person_name, confidence=90, role_in_context="attorney",
                    )

            elif len(parts) == 1:
                val = parts[0]
                matched = False
                for known_name, etype in KNOWN_ORGANIZATIONS.items():
                    if normalize_name(val) == normalize_name(known_name):
                        eid = _get_or_create_entity(session, known_name, etype)
                        _create_mention(
                            session, eid, "pz_item_detail", pid,
                            known_name, confidence=90, role_in_context="firm",
                        )
                        matched = True
                        break

                if not matched:
                    if looks_like_person(val):
                        eid = _get_or_create_entity(session, val, "person")
                        _create_mention(
                            session, eid, "pz_item_detail", pid,
                            val, confidence=90, role_in_context="applicant",
                        )
                    else:
                        eid = _get_or_create_entity(session, val, "organization")
                        _create_mention(
                            session, eid, "pz_item_detail", pid,
                            val, confidence=90, role_in_context="applicant",
                        )

        # ── Presented by ──
        if presented_by and presented_by.strip().lower() not in ("n/a", ""):
            for pname in [n.strip() for n in re.split(r"[;/]", presented_by)]:
                if pname and looks_like_person(pname):
                    peid = _get_or_create_entity(
                        session, pname, "person", is_government=True,
                    )
                    _create_mention(
                        session, peid, "pz_item_detail", pid,
                        pname, confidence=85, role_in_context="presenter",
                    )

        processed += 1
        if processed % 100 == 0:
            session.flush()
            log.info("  pz_items: %d / %d", processed, len(rows))

    return processed


# ── Phase 3: Scan agenda items for known orgs ──

def scan_agenda_orgs(
    session: Session,
    engine,
    limit: Optional[int] = None,
    batch_size: int = BATCH_SIZE,
):
    """Scan agenda items for known organization mentions. Returns match count."""
    query = """
        SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text
        FROM agenda_items ai
        ORDER BY ai.id
    """
    params = {}
    if limit:
        query += " LIMIT :limit"
        params["limit"] = limit

    rows = session.execute(text(query), params).fetchall()
    log.info("Scanning %d agenda items for known orgs (batch=%d)...", len(rows), batch_size)

    matched = 0
    for idx, item in enumerate(rows):
        item_id, title, text_content = item[0], item[1] or "", item[2] or ""
        full_text = f"{title}\n{text_content}"

        m = ORG_PATTERN.search(full_text)
        if m:
            found_name = m.group(1)
            etype = KNOWN_ORGANIZATIONS.get(found_name, "organization")
            eid = _get_or_create_entity(session, found_name, etype)
            ctx_start = max(0, m.start() - 100)
            ctx_end = min(len(full_text), ctx_start + 300)
            _create_mention(
                session, eid, "agenda_item", item_id,
                found_name, context_snippet=full_text[ctx_start:ctx_end],
                confidence=80, role_in_context="mentioned",
            )
            matched += 1

        if (idx + 1) % batch_size == 0:
            session.flush()
            log.info("  orgs: %d / %d (%d matches)", idx + 1, len(rows), matched)

    session.flush()
    return matched


# ── Phase 4: Scan agenda items for people ──

def scan_agenda_people(
    session: Session,
    engine,
    limit: Optional[int] = None,
    batch_size: int = BATCH_SIZE,
):
    """Scan agenda items for applicant/attorney/planner mentions."""
    from entities.extract import (
        APPLICANT_PATTERN,
        ATTORNEY_PATTERN,
        PLANNING_FIRM_PATTERN,
    )

    query = """
        SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text
        FROM agenda_items ai
        ORDER BY ai.id
    """
    params = {}
    if limit:
        query += " LIMIT :limit"
        params["limit"] = limit

    rows = session.execute(text(query), params).fetchall()
    log.info("Scanning %d agenda items for people (batch=%d)...", len(rows), batch_size)

    matched = 0
    for idx, item in enumerate(rows):
        item_id, title, text_content = item[0], item[1] or "", item[2] or ""
        combined = f"{title}\n{text_content}"

        found_any = False
        for pat, role in [
            (APPLICANT_PATTERN, "Applicant"),
            (ATTORNEY_PATTERN, "Attorney"),
            (PLANNING_FIRM_PATTERN, "Planner"),
        ]:
            for m in pat.finditer(combined):
                raw = m.group(1).strip()
                if raw and looks_like_person(raw):
                    eid = _get_or_create_entity(session, raw, "person")
                    _create_mention(
                        session, eid, "agenda_item", item_id,
                        raw, confidence=80, role_in_context=role,
                    )
                    found_any = True

        if found_any:
            matched += 1

        if (idx + 1) % batch_size == 0:
            session.flush()
            log.info("  people: %d / %d (%d matches)", idx + 1, len(rows), matched)

    session.flush()
    return matched


# ── Phase 5: Case number backfill (agenda items without case_number) ──

def backfill_case_numbers(
    session: Session,
    engine,
    limit: Optional[int] = None,
    batch_size: int = BATCH_SIZE,
):
    """Detect case numbers from agenda item text for items without one."""
    query = """
        SELECT ai.id, ai.body, ai.agenda_item_title, ai.agenda_item_text
        FROM agenda_items ai
        WHERE (ai.case_number IS NULL OR ai.case_number = '')
        ORDER BY ai.id
    """
    params = {}
    if limit:
        query += " LIMIT :limit"
        params["limit"] = limit

    rows = session.execute(text(query), params).fetchall()
    log.info(
        "Backfilling case numbers for %d items without case_number...",
        len(rows),
    )

    found = 0
    for idx, item in enumerate(rows):
        item_id, body, title, text_content = item[0], item[1], item[2] or "", item[3] or ""
        full_text = f"{title}\n{text_content}"

        case_nums = extract_all_case_numbers(full_text, body)
        for case_num in case_nums:
            eid = _get_or_create_entity(session, case_num, "case")
            _create_mention(
                session, eid, "agenda_item", item_id,
                case_num, confidence=85, role_in_context="case_number",
            )
            found += 1

        if (idx + 1) % batch_size == 0:
            session.flush()
            log.info(
                "  cases: %d / %d (%d found)",
                idx + 1, len(rows), found,
            )

    session.flush()
    return found


# ── Main ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Entity backfill")
    parser.add_argument("--limit", type=int, default=None,
                        help="Total items to process per phase")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Rows per transaction")
    parser.add_argument("--phases", type=str, default="1,2,3,4,5",
                        help="Comma-separated phases to run (1-5)")
    args = parser.parse_args()

    phases = [int(p.strip()) for p in args.phases.split(",")]

    engine = get_engine()
    log.info("Entity backfill starting (phases=%s, batch=%d, limit=%s)",
             phases, args.batch_size, args.limit or "full")

    t0 = time.time()

    # Phase 1: Seed known orgs
    if 1 in phases:
        log.info("Phase 1: Seeding known organizations...")
        session = Session(engine)
        try:
            seed_known_organizations(session)
            session.commit()
            log.info("Phase 1 done (%.0fs)", time.time() - t0)
        finally:
            session.close()

    # Phase 2: pz_item_details
    if 2 in phases:
        log.info("Phase 2: Processing pz_item_details...")
        session = Session(engine)
        try:
            processed = process_pz_items(
                session, engine, limit=args.limit,
            )
            session.commit()
            log.info("Phase 2 done: %d rows (%.0fs)", processed, time.time() - t0)
        finally:
            session.close()

    # Phase 3: Agenda item org scan
    if 3 in phases:
        log.info("Phase 3: Scanning agenda items for known orgs...")
        session = Session(engine)
        try:
            matched = scan_agenda_orgs(
                session, engine,
                limit=args.limit, batch_size=args.batch_size,
            )
            session.commit()
            log.info("Phase 3 done: %d matches (%.0fs)", matched, time.time() - t0)
        finally:
            session.close()

    # Phase 4: Agenda item people scan
    if 4 in phases:
        log.info("Phase 4: Scanning agenda items for people...")
        session = Session(engine)
        try:
            matched = scan_agenda_people(
                session, engine,
                limit=args.limit, batch_size=args.batch_size,
            )
            session.commit()
            log.info("Phase 4 done: %d matches (%.0fs)", matched, time.time() - t0)
        finally:
            session.close()

    # Phase 5: Case number backfill
    if 5 in phases:
        log.info("Phase 5: Backfilling case numbers...")
        session = Session(engine)
        try:
            found = backfill_case_numbers(
                session, engine,
                limit=args.limit, batch_size=args.batch_size,
            )
            session.commit()
            log.info("Phase 5 done: %d case numbers found (%.0fs)", found, time.time() - t0)
        finally:
            session.close()

    # Summary
    with engine.connect() as c:
        ent = c.execute(text("SELECT COUNT(*) FROM entities")).scalar()
        men = c.execute(text("SELECT COUNT(*) FROM entity_mentions")).scalar()
        rel = c.execute(text("SELECT COUNT(*) FROM entity_relationships")).scalar()

    log.info("=" * 50)
    log.info("Backfill complete (%.0fs)", time.time() - t0)
    log.info("Entities: %d | Mentions: %d | Relationships: %d", ent, men, rel)
    log.info("=" * 50)


if __name__ == "__main__":
    main()
