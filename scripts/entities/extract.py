#!/usr/bin/env python3
"""
Entity extraction orchestrator + shared utilities.

Shared utilities (KNOWN_ORGANIZATIONS, normalize_name, DB helpers).

This module now only provides shared constants and helper functions.
The orchestration pipeline has moved to detect_entities.py.
Archived: scripts/entities/archive/
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

# ── Superseded ──────────────────────────────────────────────────────────
#
# The pipeline functions that lived here (seed_from_pz_items,
# scan_agenda_items, seed_known_organizations, detect_withdrawals, main)
# have been superseded by detect_entities.py.
#
# Refer to  for the original code.
