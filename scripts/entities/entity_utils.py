"""
entity_utils.py — Shared helpers for entity creation and normalization.

Used across graph_builder, sweep_docs, and pattern_cascade to ensure
consistent name handling. The core principle:

    Titles carry signal, not noise.
    - entity.name:          raw text as-obtained ("Chairperson John Smith")
    - entity.normalized_name: clean parse for dedup ("john smith")
    - entity_mention.mention_text: full context at mention time ("Chairperson John Smith")

The three-layer model preserves disambiguation context (which "Smith"
was at which meeting) while giving the resolver clean keys to work with.
"""

from __future__ import annotations

import re

# ── Title/Honorific Prefixes ──────────────────────────────────────────
# Titles are stripped from normalized_name because they conflate different
# people at the same meeting ("Chairperson John Smith" vs "John Smith").
# The raw title remains in entity.name and entity_mention.mention_text.

TITLE_PREFIXES = [
    "chairperson", "chairman", "chairwoman", "vice chair", "vice-chair",
    "vice chairperson", "vice-chairperson",
    "councilmember", "council man", "council woman",
    "commissioner", "mayor", "vice mayor", "vice-mayor",
    "dr", "dr.", "honorable", "hon.", "the honorable",
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "mx", "mx.",
    "attorney", "esq", "esq.",
]

# ── Role Suffixes (after comma) ───────────────────────────────────────
# "John Smith, Chair" → normalize as "john smith" not "john smith, chair"

ROLE_SUFFIXES = [
    "chair", "vice chair", "vice-chair",
    "councilmember", "commissioner",
    "attorney", "atty", "esq", "planner", "agent",
    "representative", "rep", "manager", "director",
    "president", "vice president", "ceo", "cfo",
    "member", "chairman", "chairwoman", "chairperson",
    "interim director", "assistant director",
    "senior planner", "associate planner", "principal planner",
]


def clean_normalized_name(raw_name: str) -> str:
    """Derive a dedup-friendly normalized_name from raw entity text.

    Strips titles, honorifics, and role suffixes that would prevent
    cross-source matching. The original raw name is preserved in the
    caller's `name` and `mention_text` fields.

    Intended for use with entity_type='person'. For organizations,
    cases, and other types, use normalize_entity_name() instead.
    """
    name = raw_name.strip()
    if not name:
        return ""

    # Phase 1: Strip leading title/role prefix
    name_lower = name.lower()
    for title in TITLE_PREFIXES:
        # Match at start of string, followed by a space or end
        if name_lower.startswith(title + " ") or name_lower == title:
            name = name[len(title):].lstrip()
            name_lower = name.lower()
            break

    # Phase 2: Strip trailing role suffix after comma
    if "," in name:
        base, suffix = name.rsplit(",", 1)
        suffix_clean = suffix.strip().lower()
        for role in ROLE_SUFFIXES:
            if suffix_clean == role:
                name = base.strip()
                break

    # Phase 3: Standard normalization (lowercase, strip punctuation)
    name = re.sub(r"\s+", " ", name.strip())
    name = re.sub(r"[^\w\s'\-]", "", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^the\s+", "", name)

    return name


def normalize_entity_name(raw_name: str) -> str:
    """Standard normalization for non-person entities (orgs, cases, etc.).

    Strips common business suffixes, normalizes whitespace, removes
    punctuation (including hyphens for case-number-like patterns).
    Used by graph_builder for organizations and cases.
    """
    name = raw_name.strip()
    if not name:
        return ""

    name = re.sub(r"\s+", " ", name.strip())
    name = re.sub(
        r"\s+(P\.?L\.?C\.?|P\.?L\.?L\.?C\.?|P\.?C\.?|P\.?A\.?|"
        r"L\.?L\.?C\.?|I\.?N\.?C\.?|L\.?T\.?D\.?|C\.?O\.?R\.?P\.?|"
        r"L\.?L\.?P\.?|C\.?O\.?)\.?\s*$",
        "", name, flags=re.I,
    )
    name = name.replace("&", " and ")
    # Strip hyphens for case-number-like patterns ("Z-101-26" → "z10126")
    name = name.replace("-", "")
    name = re.sub(r"[^\w\s']", "", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^the\s+", "", name)

    return name


def is_firm_name(name: str) -> bool:
    """Check if a name looks like a firm/law office, not a person.

    Used by graph_builder's PZItemDetailsSource to split "Person, Firm"
    applicant fields (D5 fix). Returns True if the name contains indicators
    of being an organization rather than an individual person.
    """
    n = name.lower().strip()

    # Law firm/partnership patterns with "&" or "and" — these are
    # almost always firm names, not person names (e.g., "Tiffany & Bosco")
    if "&" in n or " and " in n:
        return True

    # Organizational keywords that indicate a firm, not an individual
    firm_keywords = ["law", "firm", "corporation", "company", "group",
                     "engineering", "consulting", "architecture", "planning",
                     "development", "properties", "management", "services",
                     "design", "solutions", "llc", "inc", "plc", "ltd",
                     "corp", "llp", "pllc", "pa ", "pc ", "pa.", "pc.",
                     "homes", "construction", "partnership", "association",
                     "incorporated", "power", "energy", "transmission",
                     "architecture studios", "landscape architecture",
                     "landscaping architecture"]
    for kw in firm_keywords:
        if kw in n:
            return True

    return False


def classify_entity_type(name: str) -> str:
    """Heuristic entity type classifier based on name patterns.

    Returns 'person' or 'organization'. Used by sweep_docs and
    pattern_cascade when the entity type isn't known from context.
    """
    n = name.lower().strip()

    # Explicit firm keywords
    firm_keywords = frozenset({
        "llc", "inc", "plc", "ltd", "corp", "corporation", "company",
        "group", "firm", "partnership", "consulting", "planning", "engineering",
        "law ", "office", "pa ", "pc ", "llp", "association", "incorporated",
        "development", "properties", "management", "services", "architecture",
        "construction", "design", "homes", "church", "assembly of god",
        "university", "hospital", "district", "department", "commission",
        "committee", "board of", "city of", "town of", "county of",
        "architect", "attorney", "engineer", "solutions",
    })

    if any(kw in n for kw in firm_keywords):
        return "organization"
    if "," in name:
        return "organization"
    words = name.split()
    if len(words) <= 1:
        return "organization"
    return "person"
