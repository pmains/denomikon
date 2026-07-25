"""Case number extraction — canonical pattern registry and pipeline staging.

Two tiers:
  - Body-aware: jurisdiction-specific patterns (high recall, used by backfill / entity graph)
  - Body-agnostic: conservative fallback (high precision, used by article dedup)

Case number format conventions:
  - Maricopa County:  PREFIX+YYNNNNN  (e.g. Z250044, CPAZ250011)
  - Most cities:      PREFIX-YY-NNNN or PREFIX-YYYY-NNNNN
  - Scottsdale:       YYYY-PREFIX-NNNN  (year-first)
  - Court cases:      CV-YYYY-NNNNN  (e.g. CV-2024-12345)
"""

from __future__ import annotations

import re
from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# Jurisdiction-specific patterns
# ═══════════════════════════════════════════════════════════════════

# Key:
#   (?P<prefix>[A-Z]+)  captures the case type prefix
#   (?P<num>...)        captures the numeric portion
#   The full match is the raw case string (normalized later)

JURISDICTION_PATTERNS: dict[str, list[re.Pattern]] = {
    "maricopa-county": [
        # Z250044, CPAZ250011, SU250007, MCP250001, S250001, DMP250001, etc.
        re.compile(r"\b(?P<prefix>Z|CPA|MCP|SU|S|DMP|TA|PD|TU)[-\s]?(?P<num>\d{6})\b", re.IGNORECASE),
        # Match the prefix with year digits only (most common)
        re.compile(r"\b(?P<prefix>C)(?P<num>\d{6,})\b"),  # C-Numbers: C-XXXXX -> CXXXXX
    ],
    "chandler": [
        re.compile(r"\b(?P<prefix>PLH|PLN|CASE|SPR|PUD|SUP|ZON)[-\s]?(?P<num>\d{2,})[-](?P<ext>\d{2,})\b", re.IGNORECASE),
    ],
    "mesa": [
        re.compile(r"\b(?P<prefix>ZON|PLN|CU|SP)[-\s]?(?P<num>\d{2,})[-](?P<ext>\d{4,})\b", re.IGNORECASE),
    ],
    "tempe": [
        re.compile(r"\b(?P<prefix>ZON|PLN|CU|SPR|USE|SPL)[-\s]?(?P<num>\d{2,})[-](?P<ext>\d{2,})\b", re.IGNORECASE),
    ],
    "scottsdale": [
        # Scottsdale format: YYYY-PREFIX-NNNN  (year first)
        re.compile(r"\b(?P<num>\d{4})[-](?P<prefix>GP|ZN|UP|PP|DR|TA|AB|BA|SP|SU|DT|FL)[-](?P<ext>\d{4})\b"),
        # Alternative: PREFIX-YYYY-NNNN or PREFIX-YYYY#NNNN
        re.compile(r"\b(?P<prefix>UP|DR|ZN|GP|PP|TA|AB|BA|SP|SU)[-](?P<num>\d{4})[#]?\d+\b", re.IGNORECASE),
    ],
    "phoenix": [
        re.compile(r"\b(?P<prefix>Z)[-](?P<num>\d{2,})[-](?P<ext>\d{2,})\b", re.IGNORECASE),
    ],
    "glendale": [
        re.compile(r"\b(?P<prefix>[A-Z]{2,})[-](?P<num>\d{4,})\b"),
    ],
    "surprise": [
        re.compile(r"\b(?P<prefix>CASE|ZON|PLN|SUP)[-\s]?(?P<num>\d{2,})[-](?P<ext>\d{2,})\b", re.IGNORECASE),
    ],
}

# Generalized case pattern — matches:
#   Format A: PREFIXYYNNNNNN   (e.g. Z250044, WS85100032, CT25000001)
#   Format B: PREFIX-YY-NNNNN  (e.g. CT-25-000001, PL-18-0146, CV-2024-12345)
# Generalized case pattern — matches:
#   Format A: PREFIXYYNNNNNN   (e.g. Z250044, WS85100032, CT25000001)
#   Format B: PREFIX-YY-NNNNN  (e.g. CT-25-000001, PL-18-0146, CV-2024-12345)
GENERIC_PATTERN = re.compile(
    r"\b(?P<prefix>[A-Z]{1,5})"
    r"(?:"
    r"[-\s]?(?P<num1>\d{2,})[-](?P<ext>\d{4,})"
    r"|"
    r"[-\s]?(?P<num2>\d{5,})"
    r")\b",
    re.IGNORECASE,
)

# Conservative body-agnostic (article dedup tier) — same regex, used by extract_case_number()
CONSERVATIVE_PATTERN = re.compile(
    r"\b(?P<prefix>[A-Z]{1,5})"
    r"(?:"
    r"[-\s]?(?P<num1>\d{2,})[-](?P<ext>\d{4,})"
    r"|"
    r"[-\s]?(?P<num2>\d{5,})"
    r")\b",
    re.IGNORECASE,
)
CV_PATTERN = re.compile(
    r"\b(?P<prefix>CV)[-\s](?P<num>\d{4})[-](?P<ext>\d{4,})\b",
    re.IGNORECASE,
)

# Noise patterns — things that look like case numbers but aren't
NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^FY\d{4}$", re.IGNORECASE),       # Fiscal year
    re.compile(r"^\d{5}$"),                          # Just a ZIP code
    re.compile(r"^\d{7,}$"),                         # Long number without prefix
    re.compile(r"^[A-Z]+[-]?\d{1,3}$"),              # Prefix + too few digits (e.g. AB-12)
]


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _normalize(match: re.Match) -> str:
    """Build a normalized case string from regex match groups."""
    prefix = match.group("prefix").upper()
    
    # Try Format B groups (num1 + ext) — from GENERIC/CONSERVATIVE patterns
    try:
        num1 = match.group("num1")
        ext = match.group("ext")
        if num1 and ext:
            return f"{prefix}-{num1}-{ext}"
    except IndexError:
        pass
    
    # Try Format A single-num group (num2) — from GENERIC/CONSERVATIVE patterns
    try:
        num2 = match.group("num2")
        if num2:
            return f"{prefix}{num2}"
    except IndexError:
        pass
    
    # Try jurisdiction-specific pattern groups (num, ext)
    try:
        num = match.group("num")
        ext = match.group("ext")
        if num and ext:
            return f"{prefix}-{num}-{ext}"
        if num:
            return f"{prefix}{num}"
    except IndexError:
        pass
    
    return prefix
def _guess_jurisdiction(body: str | None) -> str | None:
    """Extract a jurisdiction key from a body code (e.g. 'tempe-cc' -> 'tempe')."""
    if not body:
        return None
    body_lower = body.lower().strip()
    parts = body_lower.split("-")
    if parts[0] in JURISDICTION_PATTERNS:
        return parts[0]
    # Try matching full body code
    if body_lower in JURISDICTION_PATTERNS:
        return body_lower
    return None


# ═══════════════════════════════════════════════════════════════════
# Extraction functions
# ═══════════════════════════════════════════════════════════════════

def extract_case_number(text: str) -> str | None:
    """Conservative, body-agnostic extraction — for article dedup.
    
    Returns the first plausible case number found, or None.
    Prefers labeled patterns (Case: X) over bare.
    """
    if not text:
        return None
    
    # Try CV pattern first (court cases are distinctive)
    m = CV_PATTERN.search(text)
    if m:
        norm = _normalize(m)
        if not _is_noise(norm):
            return norm
    
    # Try labeled pattern: "Case Z250044", "Case #: SU250007"
    labeled = re.search(r"(?:Case\s*[#:]?\s*)([A-Z]{2,5}[-\s]?\d{5,})", text, re.IGNORECASE)
    if labeled:
        case_str = labeled.group(1).upper().replace(" ", "-")
        if not _is_noise(case_str):
            return case_str
    
    # Fall back to conservative bare pattern
    m = CONSERVATIVE_PATTERN.search(text)
    if m:
        norm = _normalize(m)
        if not _is_noise(norm):
            return norm
    
    return None


def _is_noise(case_str: str) -> bool:
    """Check if a matched string is a known false positive."""
    for pat in NOISE_PATTERNS:
        if pat.match(case_str):
            return True
    return False

def extract_case_number_for_body(text: str, body: str | None = None) -> str | None:
    """Body-aware extraction — uses jurisdiction-specific patterns.
    
    Returns the first plausible case number found, or None.
    Falls back to generic and conservative patterns if no jurisdiction match.
    """
    if not text:
        return None
    
    all_cases = extract_all_case_numbers(text, body)
    return all_cases[0] if all_cases else None


def extract_all_case_numbers(text: str, body: str | None = None) -> list[str]:
    """Extract all case numbers from text, optionally scoped to a jurisdiction.
    
    Returns list of normalized case number strings, in order of appearance.
    Uses jurisdiction-specific patterns first, then generic, then conservative.
    """
    if not text:
        return []
    
    seen: set[str] = set()
    results: list[str] = []
    
    def _add(case_str: str) -> None:
        norm = case_str.upper().replace(" ", "-")
        if norm not in seen and not _is_noise(norm):
            seen.add(norm)
            results.append(norm)
    
    # 1. Jurisdiction-specific patterns
    jur = _guess_jurisdiction(body)
    if jur and jur in JURISDICTION_PATTERNS:
        for pat in JURISDICTION_PATTERNS[jur]:
            for m in pat.finditer(text):
                _add(_normalize(m))
    
    # 2. Court case patterns (CV) — cross-jurisdiction
    for m in CV_PATTERN.finditer(text):
        _add(_normalize(m))
    
    # 3. Generic fallback
    for m in GENERIC_PATTERN.finditer(text):
        _add(_normalize(m))
    
    # 4. Conservative body-agnostic (catches anything the others missed)
    if not results:
        for m in CONSERVATIVE_PATTERN.finditer(text):
            _add(_normalize(m))
    
    return results


def find_pipeline_for_case(session, case_number: str):
    """Find all appearances of a case across bodies.
    
    Returns list of event dicts sorted by date.
    """
    from db.models import AgendaItem, Meeting, PublicBody
    
    items = session.query(AgendaItem).filter(
        (AgendaItem.agenda_item_text.ilike(f'%{case_number}%')) |
        (AgendaItem.agenda_item_title.ilike(f'%{case_number}%'))
    ).all()
    
    results = []
    for item in items:
        meeting = session.query(Meeting).filter(Meeting.id == item.meeting_db_id).first()
        if meeting:
            body = session.query(PublicBody).filter(PublicBody.id == meeting.public_body_id).first()
            body_name = body.name if body else "Unknown"
            results.append({
                'meeting_id': meeting.id,
                'body': body_name,
                'date': meeting.meeting_date,
                'type': meeting.meeting_type,
                'item_number': item.agenda_item_number,
                'item_title': item.agenda_item_title,
            })
    
    results.sort(key=lambda r: r['date'])
    return results


def get_article_for_case(session, case_number: str):
    """Check if an article has already been written about a case."""
    from db.newsroom import Article, ArticleSource
    
    src = session.query(ArticleSource).filter(
        ArticleSource.item_title.ilike(f'%{case_number}%')
    ).first()
    if src:
        return session.query(Article).filter(Article.id == src.article_id).first()
    
    article = session.query(Article).filter(
        Article.body.ilike(f'%{case_number}%')
    ).first()
    return article


def stage_in_pipeline(body_name: str, meeting_type: str) -> int:
    """Determine how far along a case is in the pipeline."""
    pipeline_order = [
        'planning & zoning commission',
        'planning & zoning',
        'board of adjustment',
        'board of supervisors',
        'city council',
    ]
    key = (body_name or '').lower().strip()
    for i, stage in enumerate(pipeline_order):
        if stage in key:
            return i
    return -1


def should_skip(session, case_number: str, body_name: str, mtg_type: str) -> tuple[bool, str]:
    """Check if a case should be skipped due to dedup logic."""
    from db.newsroom import Article
    
    existing = get_article_for_case(session, case_number)
    if existing:
        return True, f"Already covered in article #{existing.id} '{existing.title}'"
    
    appearances = find_pipeline_for_case(session, case_number)
    if len(appearances) > 1:
        current_stage = stage_in_pipeline(body_name, mtg_type)
        for a in appearances:
            other_stage = stage_in_pipeline(a['body'], a['type'])
            if other_stage > current_stage:
                return True, (
                    f"This case also appears at a later stage "
                    f"({a['body']} on {a['date']}). "
                    f"Prefer the later hearing unless it's an action-less continuation."
                )
    
    return False, ""
