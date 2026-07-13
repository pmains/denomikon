#!/usr/bin/env python3
"""
Entity candidate discovery — hybrid approach, Phase 1.

Does a broad regex sweep over agenda items and supporting-document text
to find candidate organization and person names that aren't already in
our entity graph. Outputs a deduplicated candidate list with frequency
counts, jurisdiction distribution, and sample contexts — ready for
LLM validation in Phase 2.

Usage:
    PYTHONPATH=scripts python3 scripts/entities/discover_candidates.py
    PYTHONPATH=scripts python3 scripts/entities/discover_candidates.py --output data/entity-candidates.json
    PYTHONPATH=scripts python3 scripts/entities/discover_candidates.py --min-frequency 3 --output data/entity-candidates.json
    PYTHONPATH=scripts python3 scripts/entities/discover_candidates.py --dry-run  (stats only)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db.core import get_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
)
log = logging.getLogger("discover")

# ═══════════════════════════════════════════════════════════════════════
#  Normalization (lifted from extract.py for consistency)
# ═══════════════════════════════════════════════════════════════════════

LEGAL_SUFFIXES = [
    r"\bP\.?L\.?C\.?", r"\bP\.?L\.?L\.?C\.?", r"\bP\.?C\.?",
    r"\bP\.?A\.?", r"\bL\.?L\.?C\.?", r"\bI\.?N\.?C\.?", r"\bL\.?T\.?D\.?",
    r"\bC\.?O\.?", r"\bC\.?O\.?R\.?P\.?",
]
LEGAL_SUFFIX_RE = re.compile(
    r"(?:\s+(" + "|".join(LEGAL_SUFFIXES) + r"))+\.?\s*$", re.IGNORECASE
)
ENTITY_KEYWORDS = {
    "homes", "development", "design", "architecture", "engineering",
    "consulting", "construction", "properties", "planning", "associates",
    "partners", "solutions", "communities", "ventures", "holdings",
    "group", "law", "landscape", "realty", "capital", "investments",
    "management", "enterprises", "industries", "company", "builders",
}


def normalize_name(raw: str) -> str:
    """Normalize an entity name for deduplication."""
    name = raw.strip()
    name = name.replace("&", " and ")
    name = re.sub(r"\.", "", name)
    name = LEGAL_SUFFIX_RE.sub("", name)
    name = re.sub(r"[^\w\s'\-]", "", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^the\s+", "", name)
    name = re.sub(r"\b(and)\s+\1\b", "and", name)
    # Remove trailing single words that are just location/city names
    for trailing in {"arizona", "maricopa", "phoenix", "chandler", "mesa",
                     "glendale", "tempe", "scottsdale", "peoria", "surprise"}:
        if name.endswith(" " + trailing):
            name = name[:-(len(trailing) + 1)]
    return name


# ═══════════════════════════════════════════════════════════════════════
#  Patterns
# ═══════════════════════════════════════════════════════════════════════

# ── Structured-field patterns ─────────────────────────────────────────
# These extract explicitly-labeled entities from agenda item text.
FIELD_PATTERNS = [
    # Applicant / Owner / Petitioner
    re.compile(
        r"(?:Applicant|Applicant/Owner|Applicant/Agent|Petitioner|Owner)\s*:?\s*"
        r"([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,6})",
    ),
    # Attorney / Counsel / Represented by
    re.compile(
        r"(?:Attorney|Counsel|Represented by|Represented By)\s*:?\s*"
        r"([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,6})",
    ),
    # Planner / Planning Consultant / Architect / Engineer
    re.compile(
        r"(?:Planner|Planning Consultant|Planning Firm|Architect|Engineer|"
        r"Landscape Architect|Traffic Engineer)\s*:?\s*"
        r"([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,6})",
    ),
    # Consultant
    re.compile(
        r"(?:Consultant|Planning & Landscape|Project Manager)\s*:?\s*"
        r"([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,6})",
    ),
]

# ── "X & Y" pattern ──────────────────────────────────────────────────
# Captures "X & Y" where X and Y are capitalized words, optionally
# followed by entity keywords.
AMPERSAND_PATTERN = re.compile(
    r"([A-Z][A-Za-z0-9'.\-]+\s+&\s+[A-Z][A-Za-z0-9'.\-]+"
    r"(?:\s+[A-Z][A-Za-z0-9'.\-]+){0,3})",
)

# ── Organization keyword pattern ──────────────────────────────────────
# Captures capitalized multi-word phrases ending with known entity
# keywords (Development, Homes, Group, LLC, etc.)
ORG_KEYWORD_PATTERN = re.compile(
    r"([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,5}"
    r"\s+(?:Homes|Development|Group|Design|Architecture|Engineering|"
    r"Consulting|Construction|Properties|Planning|Associates|Partners|"
    r"Solutions|Communities|Ventures|Holdings|Reality|Capital|"
    r"Investments|Management|Enterprises|Industries|Builders|"
    r"Landscape|Law\s+(?:Group|Firm|Offices|Practice)|"
    r"LLC|P\.?L\.?L\.?C\.?|P\.?L\.?C\.?|P\.?C\.?|P\.?A\.?|"
    r"L\.?L\.?C\.?|I\.?N\.?C\.?|L\.?T\.?D\.?|C\.?O\.?R\.?P\.?))",
)

# ── Legal suffix pattern ──────────────────────────────────────────────
# Captures "X Y Z, PLC" type names
LEGAL_END_PATTERN = re.compile(
    r"([A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-]+){1,5}"
    r"(?:,?\s+(?:P\.?L\.?L\.?C\.?|P\.?L\.?C\.?|P\.?C\.?|P\.?A\.?|"
    r"L\.?L\.?C\.?|I\.?N\.?C\.?|L\.?T\.?D\.?|C\.?O\.?R\.?P\.?)))",
)

# ── Person-in-context pattern ─────────────────────────────────────────
# Captures "First Last" when preceded by role indicators.
ROLE_TRIGGERS = r"(?:represented\s+by|presented\s+by|submitted\s+by|prepared\s+by|on\s+behalf\s+of|appearing\s+for|for\s+)"
PERSON_PATTERN = re.compile(
    ROLE_TRIGGERS + r"\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
)

# ── Exclusions ────────────────────────────────────────────────────────
# Generic planning/engineering/government phrases that aren't entities
GENERIC_PLANNING_PHRASES = {
    normalize_name(n)
    for n in [
        # Planning & zoning terms
        "Planned Area Development", "Planned Unit Development",
        "Planned Development", "Planned Residential Development",
        "Low Impact Development", "Alternative Stormwater Management",
        "Planned Development District", "Planned Shopping Center",
        "Planned Business Park", "Development Services Department",
        "Development Services", "Planning and Development",
        "Planning and Development Department", "Community Development",
        "Community Development Department", "Economic Development",
        "Economic Development Department", "Public Works Department",
        "Public Works", "Engineering Department",
        "Building Safety Department", "Building Department",
        "Water Services Department", "Transportation Department",
        "Parks and Recreation Department", "Fire Department",
        "Police Department", "Public Safety",
        "Information Technology", "Human Resources",
        "Finance Department", "City Manager's Office",
        "City Attorney's Office", "City Clerk's Office",
        "City Council District", "Supervisor District",
        "Council District", "Legislative District",
        "Occupied Residential Vacancy",
        "Roadway Design", "MCDOT Roadway Design",
        "Road Improvement Program", "Capital Improvement Program",
        "Capital Improvements", "Capital Projects",
        "Street Improvement", "Traffic Engineering",
        "Maricopa County Planning", "Flood Control District",
        "Federal Emergency Management",
        "Zoning Ordinance", "Subdivision Ordinance",
        "Development Code", "Municipal Code",
        "Design Guidelines", "Development Standards",
        "General Plan Amendment", "Comprehensive Plan",
        "Area Plan", "Specific Plan", "Master Plan",
        "Land Use", "Land Use Plan", "Existing Zoning",
        "Proposed Zoning", "Site Plan", "Conceptual Site Plan",
        "Preliminary Plat", "Final Plat", "Subdivision Plat",
        "Property Address", "Site Address", "Case Address",
        "Property Information", "Assessor Parcel Number",
        "Parcel Number", "APN Number",
        "Legal Description", "Property Description",
        "Location Map", "Vicinity Map", "Aerial Photograph",
        "Existing Conditions", "Surrounding Land Uses",
        "Zoning District Classification",
        "Summary Analysis", "Background Synopsis",
        "Staff Analysis", "Staff Findings",
        "Applicant Request", "Applicant Proposal",
        "Applicant Representation", "Applicant Statement",
        "REQUESTED ACTION", "RECOMMENDED ACTION",
        "Recommendation Summary",
        "Reviewed By", "Approved By",
        "APPROVED AS TO FORM", "APPROVED FOR DISTRIBUTION",
        "Reviewed and Approved",
        "Legal Company Name", "Company Name",
        "Business Name", "Fictitious Business Name",
        "Occupant Name", "Owner Name", "Tenant Name",
        "Rental Property", "Commercial Property",
        "Residential Property", "Vacant Property",
        "Subject Property", "Adjacent Property",
        "Mailing Address", "Physical Address", "PO Box", "P O Box",
        "Route Number", "Street Number", "Street Name",
        "Cross Street", "Major Cross Streets",
        "Traffic Impact Analysis", "Traffic Study",
        "Environmental Assessment", "Phase I Environmental",
        "Wetland Delineation", "Biological Assessment",
        "Cultural Resource Survey", "Geotechnical Investigation",
        "Drainage Report", "Hydrology Report",
        "Hydrology Study", "Floodplain Study",
        "Homeowners Association", "Property Owners Association",
        "Neighborhood Association", "Homeowner Association",
        "HOME Investment Partners",
        "Board of Directors", "Board of Appeals",
        "Board of Adjustment", "Board of Supervisors",
        "City Council", "Planning Commission",
        "Board of Trustees", "Board of Education",
        "School District Governing Board",
        "Architectural Review Committee",
        "Design Review Board", "Design Review Committee",
        "Board of Zoning Adjustment", "Zoning Board of Appeals",
        "Hearing Officer", "Zoning Administrator",
        "City Planner", "Town Planner", "County Planner",
        "Senior Planner", "Principal Planner",
        "Assistant Planner", "Associate Planner",
        "Building Official", "Fire Marshal", "City Engineer",
        "Town Engineer", "County Engineer", "Traffic Engineer",
        "City Manager", "Town Manager", "County Manager",
        "Assistant City Manager", "Deputy City Manager",
        "Finance Director", "Public Works Director",
        "Community Development Director",
        "Development Services Director",
        "Agenda Date", "Meeting Date", "Hearing Date",
        "Date Page", "Time Page",
        "CASE NUMBER", "CASE NO", "PROJECT NUMBER",
        "Page of", "Page 2", "Page 3",
        "APPLICATION TYPE", "REQUEST TYPE",
        "PROJECT DESCRIPTION", "NATURE OF REQUEST",
        "MOTION CARRIED", "MOTION DENIED",
        "ROLL CALL", "VOTE SUMMARY", "VOTING RECORD",
        "Councilmember Encinas", "Councilmember Gallego",
        "Councilmember Robinson", "Councilmember Jimenez",
        "Councilmember Starr", "Councilmember Parker",
        "Councilmember Angel Encinas", "Councilmember Ann O'Brien",
        "Councilmember Debra Stark", "Councilmember Betty Guardado",
        "Vice Mayor", "Mayor and Council", "Mayor and Board",
        "Chair and Commission",
        "Request to Speak", "Public Comment",
        "Oral Comments", "Written Comments",
        "Staff Contact", "Project Contact", "Contact Person",
        "No Action Taken", "Action Taken",
        "Information Only", "Information Report",
        "Discussion Only", "Discussion Item",
        "Action Item", "Consent Agenda", "Regular Agenda",
        "Public Hearing", "Executive Session",
        "Call to Order", "Roll Call", "Approved", "Denied",
        "Continued", "Minutes", "Resolution", "Ordinance",
        "Staff", "Staff Report", "Staff Recommendation",
        "Motion", "Second", "The Chair", "The Board",
        "The Commission", "The Council", "The Applicant",
        "The Owner", "The City", "The Town", "The County",
        "The State", "The Department",
        "N/A", "None", "TBD", "TBA", "Various", "Not applicable",
    ]
}

# Words whose presence in a candidate (not as the final keyword) suggests
# it's a generic planning phrase, not a real entity name.
GENERIC_CONTEXT_WORDS = {
    "amendment", "amended", "application", "applicant", "applicant's",
    "item", "case", "request", "report", "meeting", "hearing",
    "section", "page", "exhibit", "attachment", "revised", "updated",
    "effective", "received", "submitted", "proposed", "existing",
    "proposed", "adjacent", "surrounding", "vicinity", "subject",
    "vacant", "occupied", "residential", "commercial", "industrial",
    "mixed-use", "office", "retail", "warehouse", "storage",
    "apartment", "condominium", "townhouse", "townhome",
    "single-family", "single family", "multi-family", "multifamily",
    "preliminary", "final", "conceptual", "schematic", "detailed",
    "comprehensive", "general", "specific", "area", "master",
    "land", "water", "power", "electric", "gas", "sewer",
    "drainage", "flood", "traffic", "transportation", "transit",
    "parking", "landscape", "lighting", "signage", "grading",
    "excavation", "demolition", "construction", "renovation",
    "remodel", "addition", "expansion", "modification", "change",
    "improvement", "upgrade", "new", "replacement", "repair",
    "maintenance", "operation", "management", "service", "services",
    "department", "division", "bureau", "office", "agency",
    "authority", "commission", "board", "committee", "task force",
    "working group", "district", "region", "regional", "citywide",
    "countywide", "statewide", "phoenix", "chandler", "mesa",
    "glendale", "tempe", "scottsdale", "peoria", "surprise",
    "goodyear", "avondale", "tolleson", "buckeye", "el mirage",
    "apache junction", "fountain hills", "paradise valley",
    "queen creek", "wickenburg", "gilbert",
    "arizona", "maricopa",
    "policy", "procedure", "regulation", "standard", "guideline",
    "requirement", "criteria", "ordinance", "code", "statute",
    "rule", "permit", "license", "approval", "entitlement",
    "rezone", "rezoning", "platting", "subdivision",
    "evaluation", "assessment", "study", "analysis", "report",
    "plan", "program", "project", "initiative", "schedule",
    "phasing", "action", "recommendation", "finding", "summary",
    "pursuant", "accordance", "compliance", "conformance",
    "address", "location", "site", "property", "parcel", "lot",
    "block", "tract", "section", "township", "range", "apn",
    "owner", "operator", "builder", "contractor", "subcontractor",
    "vendor", "supplier", "consultant", "agent", "representative",
    "councilmember", "councilman", "councilwoman", "mayor",
    "vice mayor", "supervisor", "commissioner",
    "director", "manager", "coordinator", "specialist",
    "administrator", "clerk", "attorney", "solicitor",
    "inspector", "planner", "engineer", "architect",
    "information", "description", "number", "name", "type",
    "date", "time", "status",
}

# Common first names for filtering person candidates
COMMON_FIRST_NAMES = {
    "michael", "james", "robert", "john", "david", "william", "richard",
    "joseph", "thomas", "christopher", "charles", "daniel", "matthew",
    "anthony", "mark", "donald", "steven", "paul", "andrew", "joshua",
    "kenneth", "kevin", "brian", "george", "timothy", "ronald", "edward",
    "jason", "jeffrey", "ryan", "jacob", "gary", "nicholas", "eric",
    "jonathan", "stephen", "larry", "justin", "scott", "brandon",
    "benjamin", "samuel", "raymond", "gregory", "frank", "alexander",
    "patrick", "jack", "dennis", "jerry", "tyler", "aaron", "jose",
    "nathan", "henry", "douglas", "peter", "adam", "zachary", "nathaniel",
    "mary", "patricia", "jennifer", "linda", "barbara", "elizabeth",
    "susan", "jessica", "sarah", "karen", "lisa", "nancy", "betty",
    "margaret", "sandra", "ashley", "dorothy", "kimberly", "emily",
    "donna", "michelle", "carol", "amanda", "melissa", "deborah",
    "stephanie", "rebecca", "sharon", "laura", "cynthia", "kathleen",
    "amy", "angela", "shirley", "anna", "brenda", "pamela", "emma",
}


def _is_person_candidate(name: str) -> bool:
    """Heuristic: check if a name looks like an individual person."""
    words = name.lower().strip().split()
    if len(words) < 2 or len(words) > 5:
        return False
    firm_keywords = {
        "law", "group", "development", "planning", "engineering",
        "consulting", "design", "architecture", "construction",
        "properties", "homes", "llc", "plc", "inc", "corp",
        "company", "associates", "partners", "solutions",
    }
    if any(kw in words for kw in firm_keywords):
        return False
    # First word should be a known first name or look like one
    first = words[0]
    if first not in COMMON_FIRST_NAMES:
        return False
    return True


def _looks_like_org(raw: str) -> bool:
    """Check if a candidate has organization-like structure."""
    name = raw.strip()
    # Has legal suffix
    if LEGAL_SUFFIX_RE.search(name):
        return True
    # Has entity keyword
    lower = name.lower()
    if any(kw in lower for kw in ENTITY_KEYWORDS):
        return True
    # Contains "&"
    if "&" in name:
        return True
    return False


def _is_valid_candidate(raw: str) -> bool:
    """Filter out noise that matches patterns but isn't a real entity."""
    name = raw.strip()
    if not name or len(name) < 5:
        return False

    # Must be mostly capitalized words
    words = name.split()
    capitalized = sum(1 for w in words if w[0].isupper())
    if capitalized < 2 or capitalized < len(words) * 0.5:
        return False

    # Exclude single-word + number combos
    if any(w.isdigit() for w in words):
        return False

    # Exclude very short names
    if len(words) < 2:
        return False

    norm = normalize_name(name)

    # Exclude known noise
    if norm in GENERIC_PLANNING_PHRASES:
        return False

    # If every word (except possibly the last) is a generic context word,
    # it's not a real entity name
    middle_words = [w.lower().strip(".") for w in words[:-1]] if len(words) > 1 else []
    last_word = words[-1].lower().strip(".") if words else ""
    # If the last word isn't an entity keyword and all middle words are generic, reject
    entity_kw_lower = {k.lower() for k in ENTITY_KEYWORDS}
    if middle_words and last_word not in entity_kw_lower:
        if all(w in GENERIC_CONTEXT_WORDS for w in middle_words):
            return False
    # Even if last word IS an entity keyword, if ALL words are generic context -> reject
    all_words_plain = words[1:] if len(words) > 1 else words  # skip first word
    non_first = [w.lower().strip(".") for w in all_words_plain]
    if all(w in GENERIC_CONTEXT_WORDS for w in non_first):
        return False

    # Reject if it looks like a generic heading (all words common English)
    # Check if there's at least one word that's > 4 chars and not in GENERIC_CONTEXT_WORDS
    long_non_generic = sum(1 for w in words[1:]
                           if len(w) > 4 and w.lower().strip(".") not in GENERIC_CONTEXT_WORDS
                           and w.lower().strip(".") not in entity_kw_lower)
    if long_non_generic == 0 and len(words) > 2:
        return False

    return True


# ═══════════════════════════════════════════════════════════════════════
#  Extraction
# ═══════════════════════════════════════════════════════════════════════


def extract_candidates(text: str, jurisdiction_id: int,
                       source_type: str, source_id: int) -> list[dict]:
    """Run all patterns against a block of text, return candidate records.

    Each record: { normalized_name, display_name, pattern, jurisdiction_id,
                    source_type, source_id, context_snippet }
    """
    if not text:
        return []

    results = []
    seen_norms = set()

    def add(norm: str, display: str, pat_name: str, ctx: str):
        if norm in seen_norms:
            return
        if not _is_valid_candidate(display):
            return
        seen_norms.add(norm)
        results.append({
            "normalized_name": norm,
            "display_name": display,
            "pattern": pat_name,
            "jurisdiction_id": jurisdiction_id,
            "source_type": source_type,
            "source_id": source_id,
            "context_snippet": ctx[:200],
        })

    # Structured field patterns
    for pat in FIELD_PATTERNS:
        for m in pat.finditer(text):
            candidate = m.group(1).strip()
            norm = normalize_name(candidate)
            ctx_start = max(0, m.start() - 40)
            ctx = text[ctx_start:m.end() + 60]
            add(norm, candidate, "field", ctx)

    # Ampersand patterns
    for m in AMPERSAND_PATTERN.finditer(text):
        candidate = m.group(1).strip()
        norm = normalize_name(candidate)
        ctx_start = max(0, m.start() - 40)
        ctx = text[ctx_start:m.end() + 60]
        add(norm, candidate, "ampersand", ctx)

    # Organization keyword patterns
    for m in ORG_KEYWORD_PATTERN.finditer(text):
        candidate = m.group(1).strip()
        norm = normalize_name(candidate)
        ctx_start = max(0, m.start() - 40)
        ctx = text[ctx_start:m.end() + 60]
        add(norm, candidate, "org_keyword", ctx)

    # Legal suffix patterns
    for m in LEGAL_END_PATTERN.finditer(text):
        candidate = m.group(1).strip()
        norm = normalize_name(candidate)
        ctx_start = max(0, m.start() - 40)
        ctx = text[ctx_start:m.end() + 60]
        add(norm, candidate, "legal_suffix", ctx)

    # Person-in-context patterns
    for m in PERSON_PATTERN.finditer(text):
        candidate = m.group(1).strip()
        if _is_person_candidate(candidate):
            norm = normalize_name(candidate)
            ctx_start = max(0, m.start() - 40)
            ctx = text[ctx_start:m.end() + 60]
            add(norm, candidate, "person_role", ctx)

    return results


# ═══════════════════════════════════════════════════════════════════════
#  Batch processing
# ═══════════════════════════════════════════════════════════════════════


def process_agenda_items(engine, existing_norms: set[str]) -> dict[str, dict]:
    """Scan agenda items for entity candidates. Returns aggregated results."""
    log.info("Scanning agenda items...")

    with engine.connect() as c:
        rows = c.execute(
            text("""
                SELECT ai.id, ai.agenda_item_title, ai.agenda_item_text,
                       ai.meeting_db_id, m.public_body_id, b.jurisdiction_id
                FROM agenda_items ai
                JOIN meetings m ON ai.meeting_db_id = m.id
                JOIN public_bodies b ON m.public_body_id = b.id
                WHERE (ai.agenda_item_text IS NOT NULL AND ai.agenda_item_text != '')
                   OR (ai.agenda_item_title IS NOT NULL AND ai.agenda_item_title != '')
                ORDER BY ai.id
            """)
        ).fetchall()

    total = len(rows)
    log.info("  Processing %d agenda items...", total)

    # Aggregate by normalized_name
    aggregated: dict[str, dict] = {}
    processed = 0

    for row in rows:
        item_id, title, text_content = row[0], row[1] or "", row[2] or ""
        meeting_db_id, body_id, jid = row[3], row[4], row[5] or 0
        full_text = f"{title}\n{text_content}"

        candidates = extract_candidates(full_text, jid, "agenda_item", item_id)
        for cand in candidates:
            norm = cand["normalized_name"]
            if norm in existing_norms:
                continue
            if norm not in aggregated:
                aggregated[norm] = {
                    "normalized_name": norm,
                    "display_name": cand["display_name"],
                    "best_display": cand["display_name"],
                    "patterns": set(),
                    "jurisdictions": defaultdict(int),
                    "source_count": 0,
                    "total_occurrences": 0,
                    "contexts": [],
                    "is_person": _is_person_candidate(cand["display_name"]),
                }
            entry = aggregated[norm]
            entry["patterns"].add(cand["pattern"])
            entry["jurisdictions"][cand["jurisdiction_id"]] += 1
            entry["source_count"] += 1
            entry["total_occurrences"] += 1
            # Track best display (most common variation)
            if len(entry["contexts"]) < 5:
                entry["contexts"].append(cand["context_snippet"])

        processed += 1
        if processed % 10000 == 0:
            log.info("    %d / %d agenda items scanned (%d unique candidates)",
                     processed, total, len(aggregated))

    log.info("  Agenda items done: %d unique candidates", len(aggregated))
    return aggregated


def process_supporting_docs(engine, existing_norms: set[str]) -> dict[str, dict]:
    """Scan supporting document text for entity candidates."""
    log.info("Scanning supporting documents...")

    with engine.connect() as c:
        rows = c.execute(
            text("""
                SELECT sd.id, sd.text_content, sd.jurisdiction_id
                FROM supporting_documents sd
                WHERE sd.text_content IS NOT NULL AND sd.text_content != ''
                ORDER BY sd.id
            """)
        ).fetchall()

    total = len(rows)
    log.info("  Processing %d supporting documents...", total)

    aggregated: dict[str, dict] = {}
    processed = 0

    for row in rows:
        doc_id, text_content, jid = row[0], row[1] or "", row[2] or 0

        candidates = extract_candidates(text_content, jid, "supporting_document", doc_id)
        for cand in candidates:
            norm = cand["normalized_name"]
            if norm in existing_norms:
                continue
            if norm not in aggregated:
                aggregated[norm] = {
                    "normalized_name": norm,
                    "display_name": cand["display_name"],
                    "best_display": cand["display_name"],
                    "patterns": set(),
                    "jurisdictions": defaultdict(int),
                    "source_count": 0,
                    "total_occurrences": 0,
                    "contexts": [],
                    "is_person": _is_person_candidate(cand["display_name"]),
                }
            entry = aggregated[norm]
            entry["patterns"].add(cand["pattern"])
            entry["jurisdictions"][cand["jurisdiction_id"]] += 1
            entry["source_count"] += 1
            entry["total_occurrences"] += 1
            if len(entry["contexts"]) < 5:
                entry["contexts"].append(cand["context_snippet"])

        processed += 1
        if processed % 10000 == 0:
            log.info("    %d / %d docs scanned (%d unique candidates)",
                     processed, total, len(aggregated))

    log.info("  Supporting docs done: %d unique candidates", len(aggregated))
    return aggregated


def load_existing_norms(engine) -> set[str]:
    """Load normalized names of entities already in the database."""
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT normalized_name FROM entities WHERE normalized_name IS NOT NULL")
        ).fetchall()
    norms = {row[0] for row in rows}
    log.info("Loaded %d existing entity normalized names", len(norms))
    return norms


def load_known_org_norms() -> set[str]:
    """Load normalized names from the KNOWN_ORGANIZATIONS dict."""
    from entities.extract import KNOWN_ORGANIZATIONS
    return {normalize_name(name) for name in KNOWN_ORGANIZATIONS}


# ═══════════════════════════════════════════════════════════════════════
#  Output
# ═══════════════════════════════════════════════════════════════════════


def build_output(aggregated: dict[str, dict], min_frequency: int = 2) -> list[dict]:
    """Build sorted output list from aggregated results."""
    output = []
    for norm, entry in aggregated.items():
        if entry["total_occurrences"] < min_frequency:
            continue
        # Pick best display (the most common one from our fragments — best guess)
        best_display = entry["best_display"]
        # Guessed type: only tag as org if it has strong org indicators
        has_strong_org = bool(entry["patterns"] & {"ampersand", "legal_suffix"}) or any(
            kw in entry["display_name"].lower()
            for kw in ["llc", "pllc", "pc", "pa", "inc", "corp", "ltd"]
        )
        guessed_type = "person" if entry["is_person"] else (
            "organization" if has_strong_org else "uncertain"
        )
        output.append({
            "normalized_name": norm,
            "display_name": best_display,
            "guessed_type": guessed_type,
            "patterns": sorted(entry["patterns"]),
            "occurrences": entry["total_occurrences"],
            "sources": entry["source_count"],
            "jurisdictions": dict(entry["jurisdictions"]),
            "context_samples": entry["contexts"][:3],
            "needs_review": True,
        })

    # Sort by occurrence count descending
    output.sort(key=lambda x: (-x["occurrences"], x["display_name"]))
    return output


def print_stats(engine, aggregated: dict[str, dict], min_frequency: int = 2):
    """Print summary statistics."""
    output = build_output(aggregated, min_frequency=min_frequency)

    orgs = [c for c in output if c["guessed_type"] == "organization"]
    people = [c for c in output if c["guessed_type"] == "person"]
    uncertain = [c for c in output if c["guessed_type"] == "uncertain"]

    log.info("── Candidate Summary ──")
    log.info("  Total unique candidates:    %d", len(aggregated))
    log.info("  Above min_frequency (%d):   %d", min_frequency, len(output))
    log.info("    Organizations (guessed):  %d", len(orgs))
    log.info("    People (guessed):         %d", len(people))
    log.info("    Uncertain (needs LLM):   %d", len(uncertain))
    if orgs:
        log.info("")
        log.info("  Top 20 organization candidates:")
        for c in orgs[:20]:
            jids = ", ".join(str(j) for j in sorted(c["jurisdictions"].keys()))
            log.info("    %5d  %-45s  jurisdictions: [%s]  patterns: %s",
                     c["occurrences"], c["display_name"][:45], jids,
                     ", ".join(c["patterns"]))
    if people:
        log.info("")
        log.info("  Top 10 person candidates:")
        for c in people[:10]:
            log.info("    %5d  %-30s  sample: %s",
                     c["occurrences"], c["display_name"][:30],
                     c["context_samples"][0][:80] if c["context_samples"] else "")


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Discover entity candidates not yet in the entity graph"
    )
    parser.add_argument(
        "--output", type=str,
        default="data/entity-candidates.json",
        help="Output JSON path (default: data/entity-candidates.json)",
    )
    parser.add_argument(
        "--people-output", type=str,
        default="data/entity-candidates-people.json",
        help="Output path for filtered person candidates",
    )
    parser.add_argument(
        "--orgs-output", type=str,
        default="data/entity-candidates-orgs.json",
        help="Output path for filtered org candidates",
    )
    parser.add_argument(
        "--min-frequency", type=int, default=3,
        help="Minimum occurrence threshold (default: 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show stats only without writing output",
    )
    parser.add_argument(
        "--skip-docs", action="store_true",
        help="Skip supporting document scan (agenda items only)",
    )
    args = parser.parse_args()

    engine = get_engine()
    start = time.time()

    # Load existing entities to avoid duplicating known ones
    existing = load_existing_norms(engine)
    existing |= load_known_org_norms()

    # Phase 1: Agenda items
    log.info("─" * 50)
    log.info("Phase 1: Scanning agenda items...")
    ai_candidates = process_agenda_items(engine, existing)

    # Phase 2: Supporting documents
    log.info("─" * 50)
    log.info("Phase 2: Scanning supporting documents...")
    if args.skip_docs:
        log.info("  Skipped (--skip-docs)")
        sd_candidates = {}
    else:
        sd_candidates = process_supporting_docs(engine, existing)

    # Merge: agenda items take display name priority
    log.info("─" * 50)
    log.info("Merging results...")
    all_candidates = {**sd_candidates, **ai_candidates}
    for norm, entry in ai_candidates.items():
        if norm in sd_candidates:
            # Merge counts
            sd_entry = sd_candidates[norm]
            entry["total_occurrences"] += sd_entry["total_occurrences"]
            entry["source_count"] += sd_entry["source_count"]
            entry["patterns"] |= sd_entry["patterns"]
            for jid, cnt in sd_entry["jurisdictions"].items():
                entry["jurisdictions"][jid] += cnt

    log.info("  %d total unique candidates after merge", len(all_candidates))

    # Build output
    output = build_output(all_candidates, min_frequency=args.min_frequency)
    print_stats(engine, all_candidates, min_frequency=args.min_frequency)

    if args.dry_run:
        log.info("Dry run — no output written.")
        return

    # Write full output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    log.info("Wrote %d candidates to %s", len(output), output_path.resolve())

    # Write filtered subsets for LLM pipeline
    people_out = [c for c in output if c["guessed_type"] == "person"]
    orgs_out = [c for c in output if c["guessed_type"] == "organization"]
    uncertain_out = [c for c in output if c["guessed_type"] == "uncertain"]

    if args.people_output:
        ppath = Path(args.people_output)
        with open(ppath, "w") as f:
            json.dump(people_out, f, indent=2, default=str)
        log.info("Wrote %d person candidates to %s", len(people_out), ppath.resolve())

    if args.orgs_output:
        opath = Path(args.orgs_output)
        with open(opath, "w") as f:
            json.dump(orgs_out, f, indent=2, default=str)
        log.info("Wrote %d organization candidates to %s", len(orgs_out), opath.resolve())

    # Also write uncertain candidates to a separate file for potential later review
    if uncertain_out:
        upath = output_path.with_suffix(".uncertain.json")
        with open(upath, "w") as f:
            json.dump(uncertain_out, f, indent=2, default=str)
        log.info("Wrote %d uncertain candidates to %s", len(uncertain_out), upath.resolve())

    elapsed = time.time() - start
    log.info("Total time: %d seconds (%.1f min)", int(elapsed), elapsed / 60)


if __name__ == "__main__":
    main()
