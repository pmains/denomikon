#!/usr/bin/env python3
"""
Targeted entity extractor — people.

Extracts applicants, attorneys, planners, and other named individuals
from agenda items and supporting documents.  Uses role-labeled regex
patterns and the applicant field parser to identify individuals and
their roles.

Usage:
    PYTHONPATH=scripts .venv/bin/python scripts/entities/people.py
    PYTHONPATH=scripts .venv/bin/python scripts/entities/people.py --dry-run
    PYTHONPATH=scripts .venv/bin/python scripts/entities/people.py --limit=100
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from db.core import get_engine
from db.models import Entity, EntityMention

from entities.extract import (
    KNOWN_ORGANIZATIONS,
    normalize_name,
    normalize_match,
    get_or_create_entity,
    create_mention,
)

log = logging.getLogger("entities.people")


# ═══════════════════════════════════════════════════════════════════════════
#  Regex patterns
# ═══════════════════════════════════════════════════════════════════════════

APPLICANT_PATTERN = re.compile(
    r"(?:Applicant|Applicant/Owner|Applicant/Agent|Petitioner)\s*:?\s*(.+?)(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)

ATTORNEY_PATTERN = re.compile(
    r"(?:Attorney|Represented by|Represented By|Counsel)\s*:?\s*(.+?)(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)

PLANNING_FIRM_PATTERN = re.compile(
    r"(?:Planner|Planning Consultant|Planning Firm|Planning & Landscape|Architect|Engineer)\s*:?\s*(.+?)(?:\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Person name heuristics
# ═══════════════════════════════════════════════════════════════════════════


def looks_like_person(name: str) -> bool:
    """Heuristic: check if a name looks like an individual person.

    - 2-4 words
    - All words start with capital letter
    - Doesn't contain common firm keywords
    """
    name = name.strip()
    words = name.split()
    if len(words) < 2 or len(words) > 4:
        return False
    if not all(w[0].isupper() for w in words if w):
        return False
    firm_keywords = [
        "LLC", "PLC", "PLLC", "PC", "PA", "INC", "CORP", "LTD",
        "Architecture", "Engineering", "Planning", "Consulting",
        "Group", "Associates", "Partners", "Law", "Firm",
        "Development", "Properties", "Investments", "Holdings",
        "Company", "Company,", "Co.", "LLP", "P.C.",
        "Landscape Architecture", "Surveying",
        "Corporation", "Incorporated", "Limited",
    ]
    name_upper = name.upper()
    for kw in firm_keywords:
        if kw.upper() in name_upper:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  Applicant field parser
# ═══════════════════════════════════════════════════════════════════════════


def parse_applicant_field(applicant_text: str) -> list[dict]:
    """Parse the pz_item_details 'applicant' field into structured entities.

    Format is typically: "Person Name, Law Firm" or "Person name" or "Firm name".

    Returns a list of dicts with keys ``name``, ``type`` (entity type), ``role``.
    """
    if not applicant_text or applicant_text.strip().lower() in (
        "n/a", "staff-initiated", "commission-initiated",
    ):
        return []

    results: list[dict] = []
    parts = [p.strip() for p in applicant_text.replace(" \u2013 ", ", ").split(",")]

    if len(parts) >= 2 and len(parts[-1]) > 4:
        # Has a firm name after the comma: "Person Name, Law Firm LLC"
        person_name = parts[0]
        firm_name = ",".join(parts[1:]).strip()

        # Check if firm name matches a known org
        for known_name, etype in KNOWN_ORGANIZATIONS.items():
            if normalize_match(firm_name, known_name):
                results.append({"name": known_name, "type": etype, "role": "firm"})
                break
        else:
            results.append({"name": firm_name, "type": "organization", "role": "firm"})

        if person_name and looks_like_person(person_name):
            results.append({"name": person_name, "type": "person", "role": "attorney"})
    elif len(parts) == 1:
        val = parts[0]
        # Single value — could be org or person
        for known_name, etype in KNOWN_ORGANIZATIONS.items():
            if normalize_match(val, known_name):
                results.append({"name": known_name, "type": etype, "role": "firm"})
                break
        else:
            if looks_like_person(val):
                results.append({"name": val, "type": "person", "role": "applicant"})
            else:
                results.append({"name": val, "type": "organization", "role": "applicant"})

    return results


# ═══════════════════════════════════════════════════════════════════════════
#  Agenda item scanning
# ═══════════════════════════════════════════════════════════════════════════


def scan_for_people(
    engine: Engine,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> int:
    """Scan agenda items for people (applicants, attorneys, planners).

    Returns the number of agenda items that had at least one person match.
    """
    known_names = list(KNOWN_ORGANIZATIONS.keys())
    query = "SELECT meeting_db_id, agenda_item_id, agenda_item_title, agenda_item_text FROM agenda_items"
    params: dict = {}
    if limit:
        query += " LIMIT :limit"
        params["limit"] = limit

    matched = 0
    with engine.connect() as conn:
        rows = conn.execute(text(query), params).fetchall()

    for row in rows:
        mid, aid, title, body = row
        combined = f"{title or ''}\n{body or ''}" if body else (title or "")
        if not combined:
            continue

        found_any = False

        # ── Applicant patterns ──
        for m in APPLICANT_PATTERN.finditer(combined):
            raw = m.group(1).strip()
            if looks_like_person(raw):
                if not dry_run:
                    eid = get_or_create_entity(engine, raw, "person")
                    create_mention(
                        engine, eid, "agenda_item", aid,
                        mention_text=raw,
                        role_in_context="Applicant",
                    )
                found_any = True

        # ── Attorney patterns ──
        for m in ATTORNEY_PATTERN.finditer(combined):
            raw = m.group(1).strip()
            if looks_like_person(raw):
                if not dry_run:
                    eid = get_or_create_entity(engine, raw, "person")
                    create_mention(
                        engine, eid, "agenda_item", aid,
                        mention_text=raw,
                        role_in_context="Attorney",
                    )
                found_any = True

        # ── Planner patterns ──
        for m in PLANNING_FIRM_PATTERN.finditer(combined):
            raw = m.group(1).strip()
            if looks_like_person(raw):
                if not dry_run:
                    eid = get_or_create_entity(engine, raw, "person")
                    create_mention(
                        engine, eid, "agenda_item", aid,
                        mention_text=raw,
                        role_in_context="Planner",
                    )
                found_any = True
            else:
                # Could be a firm — hand off to firm extractor
                if not dry_run:
                    # Check if already known
                    for known_name, etype in KNOWN_ORGANIZATIONS.items():
                        if normalize_match(raw, known_name):
                            eid = get_or_create_entity(engine, known_name, etype)
                            create_mention(
                                engine, eid, "agenda_item", aid,
                                mention_text=raw,
                                role_in_context="Firm",
                            )
                            break

        if found_any:
            matched += 1

    return matched


# ═══════════════════════════════════════════════════════════════════════════
#  Seed from pz_item_details applicant field
# ═══════════════════════════════════════════════════════════════════════════


def seed_from_applicants(engine: Engine, dry_run: bool = False) -> int:
    """Parse pz_item_details.applicant field into person + firm entities.

    Returns count of rows processed.
    """
    rows = []
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT agenda_item_id, applicant FROM pz_item_details "
                "WHERE applicant IS NOT NULL AND applicant != ''"
            )
        )
        rows = result.fetchall()

    processed = 0
    for aid, applicant_text in rows:
        entities = parse_applicant_field(applicant_text)
        if not entities:
            continue
        if not dry_run:
            for ent in entities:
                eid = get_or_create_entity(engine, ent["name"], ent["type"])
                create_mention(
                    engine, eid, "agenda_item", aid,
                    mention_text=ent["name"],
                    role_in_context=ent["role"].capitalize(),
                )
        processed += 1

    return processed


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract people from agenda items")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--limit", type=int, default=None, help="Limit agenda items")
    args = parser.parse_args()

    engine = get_engine()
    log.info("People extraction starting (dry_run=%s)", args.dry_run)

    # Phase 1: seed from pz_item_details
    log.info("Phase 1: Seeding from pz_item_details applicant field...")
    processed = seed_from_applicants(engine, dry_run=args.dry_run)
    log.info("  Processed %d rows", processed)

    # Phase 2: scan agenda items
    log.info("Phase 2: Scanning agenda items for people...")
    matched = scan_for_people(engine, dry_run=args.dry_run, limit=args.limit)
    log.info("  %d agenda items had person matches", matched)

    log.info("People extraction complete")


if __name__ == "__main__":
    main()
