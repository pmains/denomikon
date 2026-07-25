#!/usr/bin/env python3
"""
Entity extraction orchestrator + shared utilities.

Orchestrates targeted extractors (people, parcels, cases, firms) and
houses shared helpers (KNOWN_ORGANIZATIONS, normalize_name, DB utils).

Targeted extractors live in sibling modules and can be run independently:
    python -m entities.people --help
    python -m entities.parcels --help
    python -m entities.cases --help

Usage:
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract.py
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract.py --seed-only
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract.py --dry-run
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from db.core import get_engine
from db.models import Entity, EntityMention, EntityRelationship, Base

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("entities")

# ── Normalization ──────────────────────────────────────────────────────

LEGAL_SUFFIXES = [
    r"\bP\.?L\.?C\.?", r"\bP\.?L\.?L\.?C\.?", r"\bP\.?C\.?",
    r"\bP\.?A\.?", r"\bL\.?L\.?C\.?", r"\bI\.?N\.?C\.?", r"\bL\.?T\.?D\.?",
    r"\bC\.?O\.?", r"\bC\.?O\.?R\.?P\.?",
]
LEGAL_SUFFIX_RE = re.compile(
    r"(?:\s+(" + "|".join(LEGAL_SUFFIXES) + r"))+\.?\s*$", re.IGNORECASE
)


def normalize_name(raw: str) -> str:
    """Normalize an entity name for deduplication.

    - Lowercase
    - Normalize & → and
    - Strip punctuation (except hyphens and apostrophes)
    - Strip legal entity suffixes (PLC, LLC, PC, PA, Inc, Corp, Ltd, etc.)
    - Collapse whitespace
    - Remove trailing "the"
    """
    name = raw.strip()
    # Normalize ampersand
    name = name.replace("&", " and ")
    # Remove trailing period from abbreviations like "P.C." → "PC"
    name = re.sub(r"\.", "", name)
    # Strip legal suffixes (PC, PLC, PLLC, LLC, Inc, Corp, Ltd, Co, PA, LLP, LP)
    name = LEGAL_SUFFIX_RE.sub("", name)
    # Lowercase, keep only word chars, hyphens, apostrophes, spaces
    name = re.sub(r"[^\w\s'\-]", "", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    # Remove trailing "the" articles
    name = re.sub(r"^the\s+", "", name)
    # Collapse doubled words from &→and normalization
    name = re.sub(r"\b(and)\s+\1\b", "and", name)
    return name


def normalize_match(a: str, b: str) -> bool:
    """Return True if two normalized names are close enough to be the same entity."""
    return normalize_name(a) == normalize_name(b)


# ── Known organization seed list ───────────────────────────────────────

# Major Valley developers, law firms, planning firms, and consultants
# that appear frequently in meeting records.
KNOWN_ORGANIZATIONS: dict[str, str] = {
    # Developers
    "Taylor Morrison": "developer",
    "Lennar": "developer",
    "Pulte Homes": "developer",
    "KB Home": "developer",
    "D.R. Horton": "developer",
    "Shea Homes": "developer",
    "Toll Brothers": "developer",
    "Fulton Homes": "developer",
    "Meritage Homes": "developer",
    "Richmond American Homes": "developer",
    "Beazer Homes": "developer",
    "Centex": "developer",
    "Standard Pacific Homes": "developer",
    "Clayton Homes": "developer",
    "Woodside Homes": "developer",
    "Ashton Woods": "developer",
    "M.D.C. Holdings": "developer",
    "LGI Homes": "developer",
    "Dream Finders Homes": "developer",
    "Landsea Homes": "developer",
    "Trilogy": "developer",
    "Ripson Homes": "developer",
    "Regal Homes": "developer",
    "A & B Homes": "developer",
    "Viking Development": "developer",
    "Origis Development": "developer",
    "Hicken Holdings": "developer",
    "SimonCRE": "developer",
    "Plus Power": "developer",
    "Avantus": "developer",
    "Recurrent Energy": "developer",
    "RWE": "developer",
    "DCR Transmission": "developer",
    "Montana Tractor & Plow": "developer",
    "Busby Permits": "developer",

    # Law firms
    "Gust Rosenfeld": "law_firm",
    "Tiffany & Bosco": "law_firm",
    "Snell & Wilmer": "law_firm",
    "Rose Law Group": "law_firm",
    "Quarles & Brady": "law_firm",
    "Gammage & Burnham": "law_firm",
    "Burch & Cracchiolo": "law_firm",
    "May Potenza Baran & Gillespie": "law_firm",
    "Withey Morris Baugh": "law_firm",
    "Berry Riddell": "law_firm",
    "Pew & Lake": "law_firm",
    "Gilbert & Blilie": "law_firm",
    "Bergin Frakes Smalley Oberholtzer": "law_firm",
    "Smalley & Oberholtzer": "law_firm",
    "Earl & Curley": "law_firm",
    "Ray Law Firm": "law_firm",
    "Greenman Law Firm": "law_firm",
    "BFSO Law": "law_firm",
    "Beus Gilbert MacGroder": "law_firm",

    # Planning firms / consultants
    "RVi Planning + Landscape Architecture": "planning_firm",
    "Gilmore Planning & Landscape Architecture": "planning_firm",
    "Logan Simpson": "planning_firm",
    "Kimley-Horn": "planning_firm",
    "Norris Design": "planning_firm",
    "Huitt-Zollars": "planning_firm",
    "EPS Group": "planning_firm",
    "Pinnacle Consulting": "planning_firm",
    "CVL Consultants": "planning_firm",
    "IPlan Consulting": "planning_firm",
    "KP Environmental": "planning_firm",
    "State 48 Consulting": "planning_firm",
    "Upfront Planning & Entitlements": "planning_firm",
    "Coal Creek Consulting": "planning_firm",
    "Anderson Development Engineering": "planning_firm",
    "Keogh Engineering": "planning_firm",
    "Edifice Architecture": "planning_firm",
    "RBA Architecture": "planning_firm",
    "Merge Architecture Group": "planning_firm",
    "Butler Design Group": "planning_firm",
    "Almond ADG": "planning_firm",
    "Sefdesign": "planning_firm",
    "Young Design": "planning_firm",
    "M & H Pools and Spas": "planning_firm",
    "RAP LLC": "planning_firm",
    "State 48 Development Consulting": "planning_firm",

    # Government entities
    "Arizona Public Service": "utility",
    "Salt River Project": "utility",
    "Southwest Gas": "utility",
    "Century Link": "utility",

    # Neighborhood / advocacy groups
    "Save Our Scottsdale": "advocacy_group",
}

# ── Regex patterns ─────────────────────────────────────────────────────

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

CASE_NUMBER_PATTERN = re.compile(
    r"\b(ZON|PLN|CU|SPR|CPA|MCP|SPL|USE|Z|P|CASE)[-\s]?\d{2,}[-]\d{2,}\b",
    re.IGNORECASE,
)

KNOWN_ORG_PATTERN = re.compile(
    r"(" + "|".join(re.escape(name) for name in KNOWN_ORGANIZATIONS) + r")",
    re.IGNORECASE,
)


def extract_from_applicant_field(applicant_text: str) -> list[dict]:
    """Parse the pz_item_details 'applicant' field into structured entities.

    Format is typically: "Person Name, Law Firm" or "Person name" or "Firm name"
    """
    if not applicant_text or applicant_text.strip().lower() in ("n/a", "staff-initiated", "commission-initiated"):
        return []

    results = []
    parts = [p.strip() for p in applicant_text.replace(" – ", ", ").split(",")]

    if len(parts) >= 2 and len(parts[-1]) > 4:
        # Has a firm name after the comma
        person_name = parts[0]
        firm_name = ",".join(parts[1:]).strip()

        # Check if firm name matches a known org
        for known_name, etype in KNOWN_ORGANIZATIONS.items():
            if normalize_match(firm_name, known_name):
                results.append({"name": known_name, "type": etype, "role": "firm"})
                break
        else:
            # Unknown org — add it as organization
            results.append({"name": firm_name, "type": "organization", "role": "firm"})

        if person_name and _looks_like_person(person_name):
            results.append({"name": person_name, "type": "person", "role": "attorney"})
    elif len(parts) == 1:
        val = parts[0]
        # Single value — could be org or person
        for known_name, etype in KNOWN_ORGANIZATIONS.items():
            if normalize_match(val, known_name):
                results.append({"name": known_name, "type": etype, "role": "firm"})
                break
        else:
            if _looks_like_person(val):
                results.append({"name": val, "type": "person", "role": "applicant"})
            else:
                results.append({"name": val, "type": "organization", "role": "applicant"})

    return results


def _looks_like_person(name: str) -> bool:
    """Heuristic: check if a name looks like an individual person.

    - 2-4 words
    - All words start with capital letter
    - Doesn't contain common firm keywords
    """
    name = name.strip()
    if not name:
        return False

    words = name.split()
    if len(words) < 2 or len(words) > 5:
        return False

    firm_keywords = {"law", "group", "development", "planning", "engineering",
                     "consulting", "design", "architecture", "construction",
                     "properties", "homes", "llc", "plc", "inc", "corp",
                     "company", "associates", "partners", "solutions"}
    name_lower = name.lower()
    if any(kw in name_lower for kw in firm_keywords):
        return False

    return all(w[0].isupper() for w in words if w)


# ── Database operations ────────────────────────────────────────────────


def get_or_create_entity(engine: Engine, name: str, etype: str,
                         jurisdiction_id: int = None,
                         is_government: bool = False) -> int:
    """Find existing entity by normalized name, or create. Returns entity id."""
    norm = normalize_name(name)
    with engine.begin() as c:
        existing = c.execute(
            text("SELECT id FROM entities WHERE normalized_name = :norm"),
            {"norm": norm},
        ).fetchone()
        if existing:
            # Touch last_seen_at
            c.execute(
                text("UPDATE entities SET last_seen_at = NOW(), mention_count = mention_count + 1 WHERE id = :id"),
                {"id": existing[0]},
            )
            return existing[0]

        # Create
        result = c.execute(
            text(
                "INSERT INTO entities (entity_type, name, normalized_name, "
                "jurisdiction_id, is_government, first_seen_at, last_seen_at, "
                "mention_count, created_at, updated_at) "
                "VALUES (:etype, :name, :norm, :jid, :gov, NOW(), NOW(), 1, NOW(), NOW()) "
                "RETURNING id"
            ),
            {"etype": etype, "name": name, "norm": norm,
             "jid": jurisdiction_id, "gov": is_government},
        )
        return result.scalar()


def create_mention(engine: Engine, entity_id: int, source_type: str,
                   source_id: int, mention_text: str, context_snippet: str = None,
                   confidence: int = 0, extracted_by: str = "regex",
                   role_in_context: str = None):
    """Record a mention of an entity in a source document."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO entity_mentions "
                "(entity_id, source_type, source_id, mention_text, context_snippet, "
                "confidence, extracted_by, role_in_context, created_at) "
                "VALUES (:eid, :st, :sid, :mt, :cs, :conf, :eb, :ric, NOW()) "
                "ON CONFLICT DO NOTHING"
            ),
            {"eid": entity_id, "st": source_type, "sid": source_id,
             "mt": mention_text, "cs": context_snippet,
             "conf": confidence, "eb": extracted_by, "ric": role_in_context},
        )


def create_relationship(engine: Engine, from_eid: int, to_eid: int,
                        rel_type: str, source_type: str = None,
                        source_id: int = None, confidence: int = 50):
    """Create a typed relationship between two entities."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO entity_relationships "
                "(from_entity_id, to_entity_id, relationship, source_type, source_id, "
                "confidence, created_at) "
                "VALUES (:fe, :te, :rel, :st, :sid, :conf, NOW()) "
                "ON CONFLICT (from_entity_id, to_entity_id, relationship) DO NOTHING"
            ),
            {"fe": from_eid, "te": to_eid, "rel": rel_type,
             "st": source_type, "sid": source_id, "conf": confidence},
        )


# ── Seed passes ────────────────────────────────────────────────────────


def seed_from_pz_items(engine: Engine, dry_run: bool = False) -> int:
    """Extract entities from pz_item_details.applicant and .presented_by fields.

    Returns count of unique source rows processed.
    """
    with engine.connect() as c:
        rows = c.execute(
            text("""
                SELECT p.id, p.case_number, p.project_name, p.applicant,
                       p.presented_by, p.recommendation, p.meeting_db_id,
                       p.agenda_item_number
                FROM pz_item_details p
                ORDER BY p.id
            """)
        ).fetchall()

    processed = 0
    for row in rows:
        pid, case_number, project_name, applicant, presented_by = \
            row[0], row[1], row[2], row[3], row[4]
        meeting_db_id, agenda_item_number = row[5], row[6]

        entities_in_row = []

        # ── Case entity ──
        if case_number:
            if not dry_run:
                cas_id = get_or_create_entity(engine, case_number.upper(), "case",
                                              jurisdiction_id=4)  # Maricopa County
                create_mention(engine, cas_id, "pz_item_detail", pid,
                               case_number.upper(), confidence=95, role_in_context="case_number")
                entities_in_row.append(cas_id)
            else:
                log.info("  [dry-run] case: %s", case_number.upper())

        # ── Applicant field ──
        if applicant:
            extracted = extract_from_applicant_field(applicant)
            for ent in extracted:
                if not dry_run:
                    eid = get_or_create_entity(engine, ent["name"], ent["type"])
                    create_mention(engine, eid, "pz_item_detail", pid,
                                   ent["name"], confidence=90, extracted_by="regex",
                                   role_in_context=ent["role"])
                    entities_in_row.append(eid)
                else:
                    log.info("  [dry-run] %s: %s (role=%s)", ent["type"], ent["name"], ent["role"])

            # ── Relationships between firm and person ──
            people = [e for e in extracted if e["type"] == "person"]
            firms = [e for e in extracted if e["type"] != "person"]
            if people and firms and not dry_run:
                for p in people:
                    p_eid = get_or_create_entity(engine, p["name"], p["type"])
                    for f in firms:
                        f_eid = get_or_create_entity(engine, f["name"], f["type"])
                        create_relationship(engine, p_eid, f_eid, "represents",
                                            source_type="pz_item_detail", source_id=pid,
                                            confidence=80)

        # ── Presented by ──
        if presented_by and presented_by.strip().lower() not in ("n/a", ""):
            presented_names = [n.strip() for n in re.split(r"[;/]", presented_by)]
            for pname in presented_names:
                if _looks_like_person(pname):
                    if not dry_run:
                        peid = get_or_create_entity(engine, pname, "person",
                                                     jurisdiction_id=4,
                                                     is_government=True)
                        create_mention(engine, peid, "pz_item_detail", pid,
                                       pname, confidence=85, extracted_by="regex",
                                       role_in_context="presenter")
                    else:
                        log.info("  [dry-run] government_person: %s (role=presenter)", pname)

        processed += 1
        if processed % 50 == 0:
            log.info("  processed %d / %d pz_item_details rows", processed, len(rows))

    return processed


def seed_known_organizations(engine: Engine, dry_run: bool = False) -> int:
    """Seed the KNOWN_ORGANIZATIONS list so they exist for matching during agenda scan."""
    created = 0
    for name, etype in KNOWN_ORGANIZATIONS.items():
        is_gov = (etype == "government")
        if not dry_run:
            eid = get_or_create_entity(engine, name, etype, is_government=is_gov)
            if eid:
                created += 1
    log.info("Seeded %d known organizations (%d created/updated)", len(KNOWN_ORGANIZATIONS), created)
    return created


def scan_agenda_items(engine: Engine, dry_run: bool = False, limit: int = None) -> int:
    """Scan all agenda items for entity mentions.

    Uses regex patterns for applicant, attorney, and known organization names.
    This is the large-scale pass — processes all 93K items.
    """
    with engine.connect() as c:
        query = """
            SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text,
                   ai.meeting_db_id
            FROM agenda_items ai
            ORDER BY ai.id
        """
        if limit:
            query += f" LIMIT {limit}"
        items = c.execute(text(query)).fetchall()

    # Build a single compound regex matching all known org names once
    known_names = list(KNOWN_ORGANIZATIONS.keys())
    # Sort by length descending so longer names match before substrings
    known_names.sort(key=len, reverse=True)
    pattern = re.compile(
        "(" + "|".join(re.escape(n) for n in known_names) + ")",
        re.IGNORECASE,
    )

    matched = 0
    for idx, item in enumerate(items):
        item_id, title, text_content = item[0], item[1] or "", item[2] or ""
        full_text = f"{title}\n{text_content}"

        # Single regex pass matches all known orgs
        m = pattern.search(full_text)
        if m:
            found_name = m.group(1)
            etype = KNOWN_ORGANIZATIONS.get(found_name, "organization")
            if not dry_run:
                eid = get_or_create_entity(engine, found_name, etype)
                context_start = max(0, m.start() - 100)
                context = full_text[context_start:context_start + 300]
                create_mention(engine, eid, "agenda_item", item_id,
                               found_name, context.strip(),
                               confidence=80, extracted_by="regex",
                               role_in_context="mentioned")
                matched += 1

        if idx > 0 and idx % 5000 == 0:
            log.info("  scanned %d / %d agenda items (%d matches)", idx, len(items), matched)

        if limit and idx >= limit - 1:
            break

    log.info("Agenda item scan complete: %d / %d items matched", matched, len(items))
    return matched


# ── Withdrawal detection ────────────────────────────────────────────────

WITHDRAWAL_PATTERNS = [
    re.compile(r"(?:item|application|request|petition)\s+(?:is\s+)?withdrawn", re.IGNORECASE),
    re.compile(r"withdrawn\s+(?:by|at|per|pursuant)", re.IGNORECASE),
    re.compile(r"deferred\s+sine\s+die", re.IGNORECASE),
    re.compile(r"(?:removed|pulled|taken)\s+(?:at|from|by|per)\s+(?:the\s+)?(?:request|direction)\s+of", re.IGNORECASE),
    re.compile(r"(?:removed|pulled|taken)\s+(?:from|off)\s+(?:the\s+)?(?:agenda|calendar|docket|consideration)", re.IGNORECASE),
    re.compile(r"no\s+action\s+(?:taken|recommended)\s+(?:at|by|per)\s+(?:the\s+)?(?:request|direction)\s+of", re.IGNORECASE),
    re.compile(r"motion\s+(?:to\s+)?withdraw", re.IGNORECASE),
    re.compile(r"(?:withdrawal|withdrawing)\s+(?:of|by)", re.IGNORECASE),
]


def detect_withdrawals(engine: Engine, dry_run: bool = False, limit: int = None) -> int:
    """Scan all agenda items for withdrawal language.

    Flags any entity_mention whose source agenda item contains
    withdrawal keywords. Also creates withdrawal-flagged mentions
    for items that mention entities and contain withdrawal language.
    """
    with engine.connect() as c:
        items = c.execute(
            text("""
                SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text
                FROM agenda_items ai
                ORDER BY ai.id
            """)
        ).fetchall()

    total = len(items)
    withdrawn_count = 0
    flagged_entities = set()

    for idx, item in enumerate(items):
        item_id, title, text_content = item[0], item[1] or "", item[2] or ""
        full_text = f"{title}\n{text_content}"

        # Check each withdrawal pattern
        is_withdrawn = False
        reason = None
        for pat in WITHDRAWAL_PATTERNS:
            m = pat.search(full_text)
            if m:
                is_withdrawn = True
                reason = m.group(0)[:120]
                break

        if not is_withdrawn:
            continue

        withdrawn_count += 1

        if dry_run:
            continue

        # Flag any existing entity_mentions for this agenda item as withdrawn
        with engine.begin() as c:
            result = c.execute(
                text("""
                    UPDATE entity_mentions
                    SET is_withdrawn = true, flag_reason = :reason
                    WHERE source_type = 'agenda_item'
                      AND source_id = :item_id
                      AND NOT is_withdrawn
                """),
                {"reason": reason, "item_id": item_id},
            )
            if result.rowcount:
                flagged_entities.add(item_id)

        if idx > 0 and idx % 10000 == 0:
            log.info("  scanned %d / %d agenda items (%d withdrawn, %d flagged)",
                     idx, total, withdrawn_count, len(flagged_entities))

        if limit and idx >= limit - 1:
            break

    log.info("Withdrawal detection complete: %d / %d items flagged as withdrawn (%d entities linked)",
             withdrawn_count, total, len(flagged_entities))
    return withdrawn_count


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Entity extraction orchestrator")
    parser.add_argument("--seed-only", action="store_true",
                        help="Only seed known orgs + pz_item_details")
    parser.add_argument("--scan-only", action="store_true",
                        help="Only scan agenda items (skip seed)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit agenda items processed (for testing)")
    parser.add_argument("--withdrawals", action="store_true",
                        help="Run withdrawal detection on agenda items")
    args = parser.parse_args()

    engine = get_engine()
    log.info("Entity extraction pipeline starting (dry_run=%s)", args.dry_run)

    # ── 1. Seed known organizations ──
    if not args.scan_only and not args.withdrawals:
        log.info("Phase 1: Seed known organizations (%d entries)...", len(KNOWN_ORGANIZATIONS))
        seed_known_organizations(engine, dry_run=args.dry_run)

        # ── 2. Seed people & firms from pz_item_details ──
        log.info("Phase 2: Seed people from pz_item_details...")
        try:
            from entities.people import seed_from_applicants
            processed = seed_from_applicants(engine, dry_run=args.dry_run)
            log.info("Phase 2 complete: %d rows processed", processed)
        except Exception as e:
            log.warning("Phase 2 (people) skipped: %s", e)

    # ── 3. Scan agenda items for people ──
    if (not args.seed_only or args.scan_only) and not args.withdrawals:
        log.info("Phase 3: Scanning agenda items for people...")
        try:
            from entities.people import scan_for_people
            matched = scan_for_people(engine, dry_run=args.dry_run, limit=args.limit)
            log.info("Phase 3 complete: %d items matched", matched)
        except Exception as e:
            log.warning("Phase 3 (people) skipped: %s", e)

    # ── 4. Withdrawal detection ──
    if args.withdrawals:
        log.info("Phase 4: Withdrawal detection...")
        withdrawn = detect_withdrawals(engine, dry_run=args.dry_run, limit=args.limit)
        log.info("Phase 4 complete: %d items flagged as withdrawn", withdrawn)

    # ── Summary ──
    with engine.connect() as c:
        ent_count = c.execute(text("SELECT COUNT(*) FROM entities")).scalar()
        mention_count = c.execute(text("SELECT COUNT(*) FROM entity_mentions")).scalar()
        rel_count = c.execute(text("SELECT COUNT(*) FROM entity_relationships")).scalar()
    log.info("── Entity Layer Summary ──")
    log.info("  Entities:      %d", ent_count)
    log.info("  Mentions:      %d", mention_count)
    log.info("  Relationships: %d", rel_count)


if __name__ == "__main__":
    main()
