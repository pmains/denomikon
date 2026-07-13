#!/usr/bin/env python3
"""
Case/project number extraction — Phase 1.7.

Scans all agenda item titles for case number patterns across all jurisdictions,
creates entity records for each unique case, and links mentions to source items.

Usage:
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract_cases.py
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract_cases.py --dry-run
    PYTHONPATH=scripts .venv/bin/python scripts/entities/extract_cases.py --limit=10000
"""

from __future__ import annotations

import logging
import os
import re
import sys
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from db.core import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cases")

# Per-jurisdiction case number patterns
# Format: prefix-digits, various separators
CASE_PATTERNS: dict[str, list[re.Pattern]] = {
    "chandler": [
        re.compile(r"\b(PLH|PLN|CASE|SPR|PUD|SUP|ZON)[-\s]?(\d{2,})[-](\d{2,})\b", re.IGNORECASE),
    ],
    "mesa": [
        re.compile(r"\b(ZON|PLN|CU|SP)[-\s]?(\d{2,})[-](\d{4,})\b", re.IGNORECASE),
    ],
    "tempe": [
        re.compile(r"\b(ZON|PLN|CU|SPR|USE|SPL)[-\s]?(\d{2,})[-](\d{2,})\b", re.IGNORECASE),
    ],
    "scottsdale": [
        re.compile(r"\b(\d+)[-](GP|ZN|UP|PP|DR|TA|AB|BA|SP|SU|DT|FL|GP)[-](\d{4})\b"),
        re.compile(r"\b(UP|DR|ZN|GP|PP|TA|AB|BA|SP|SU)[-](\d{4})[#]\d+\b", re.IGNORECASE),
    ],
    "maricopa-county": [
        re.compile(r"\b(Z|CPA|MCP|SU|S|DMP|TA|PD|TU)[-\s]?(\d{6,})\b", re.IGNORECASE),
    ],
    "phoenix": [
        re.compile(r"\bZ[-](\d{2,})[-](\d{2,})\b", re.IGNORECASE),
    ],
    "glendale": [
        re.compile(r"\b([A-Z]+)[-]?(\d{4,})\b"),
    ],
    "surprise": [
        re.compile(r"\b(CASE|ZON|PLN|SUP)[-\s]?(\d{2,})[-](\d{2,})\b", re.IGNORECASE),
    ],
}

# Generic fallback — catches anything case-like
GENERIC_PATTERN = re.compile(
    r"\b(CASE|ZON|PLN|CPA|SUP|SPR|CU|MCP|DR|PUD|SPL|USE|PLH|PCD|SP|ZN|GP|PP|TA|AB|BA|RFQ|RFP|IFB|ORD)[-\s]?(\d{2,})[-](\d{2,})\b",
    re.IGNORECASE,
)


# Case number noise filter — skip these common non-case patterns
NOISE_PATTERNS = [
    re.compile(r"^FY\d{4}$", re.IGNORECASE),        # Fiscal year references
    re.compile(r"^\d+[-]LLC$", re.IGNORECASE),       # LLC matches
    re.compile(r"^\d+[-]INC$", re.IGNORECASE),       # Inc matches
    re.compile(r"^LLC$", re.IGNORECASE),
    re.compile(r"^INC$", re.IGNORECASE),
    re.compile(r"^P\.?L\.?C\.?$", re.IGNORECASE),
    re.compile(r"^P\.?A\.?$", re.IGNORECASE),
    re.compile(r"^\d{5}$"),                          # Just a 5-digit number (ZIP code)
    re.compile(r"^\d{7,}$"),                         # Long number without prefix
]


def _is_noise(case_str: str) -> bool:
    for pat in NOISE_PATTERNS:
        if pat.match(case_str):
            return True
    return False


def normalize_case(case_str: str) -> str:
    """Normalize a case number to a standard format: PREFIX-YY-NNNN."""
    return case_str.upper().replace(" ", "-").replace("_", "-")


def extract_cases_from_title(title: str, body: str | None = None) -> list[str]:
    """Extract case number strings from an agenda item title."""
    if not title:
        return []

    found: list[str] = []
    seen = set()

    # Try jurisdiction-specific patterns
    if body:
        # Use the body code prefix to guess jurisdiction
        jur = body.split("-")[0] if "-" in body else body
        patterns = CASE_PATTERNS.get(jur, []) + CASE_PATTERNS.get("__fallback__", [])

        for pat in patterns:
            for m in pat.finditer(title):
                case_str = m.group(0)
                norm = normalize_case(case_str)
                if norm not in seen and not _is_noise(norm):
                    seen.add(norm)
                    found.append(norm)
    elif GENERIC_PATTERN.search(title):
        for m in GENERIC_PATTERN.finditer(title):
            case_str = m.group(0)
            norm = normalize_case(case_str)
            if norm not in seen and not _is_noise(norm):
                seen.add(norm)
                found.append(norm)

    return found


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Case number extraction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    engine = get_engine()

    # Scan all agenda items
    with engine.connect() as c:
        items = c.execute(
            text("""
                SELECT ai.id, ai.agenda_item_title, ai.meeting_db_id
                FROM agenda_items ai
                ORDER BY ai.id
            """),
        ).fetchall()

    # Get body info for each meeting
    with engine.connect() as c:
        meeting_bodies = dict(c.execute(
            text("SELECT id, body FROM meetings")
        ).fetchall())

    log.info("Scanning %d agenda items for case numbers...", len(items))

    case_counts: dict[str, int] = {}
    total_matched = 0
    limit = args.limit or len(items)

    for idx, item in enumerate(items):
        if idx >= limit:
            break
        item_id, title = item[0], item[1] or ""
        meeting_id = item[2]
        body = meeting_bodies.get(meeting_id, "")

        cases = extract_cases_from_title(title, body)
        if cases:
            total_matched += 1
            for c in cases:
                case_counts[c] = case_counts.get(c, 0) + 1

        if idx > 0 and idx % 10000 == 0:
            log.info("  scanned %d / %d items (%d matched, %d unique cases)",
                     idx, limit, total_matched, len(case_counts))

    log.info("Scan complete: %d items matched, %d unique case numbers found",
             total_matched, len(case_counts))

    # Top 20 most frequent
    sorted_cases = sorted(case_counts.items(), key=lambda x: -x[1])
    log.info("── Top 20 most frequent case numbers ──")
    for case, count in sorted_cases[:20]:
        log.info("  %-25s  %d mentions", case, count)

    # Get existing case count before persisting
    with engine.connect() as c:
        existing = c.execute(
            text("SELECT COUNT(*) FROM entities WHERE entity_type = 'case'")
        ).scalar()
    log.info("Current case entities: %d. New cases to discover: %d",
             existing, len(case_counts) - existing)

    # ── Persist to entity tables (unless dry-run) ──
    if not args.dry_run and case_counts:
        log.info("Persisting unique case numbers as entities...")
        persisted = 0
        for case, count in case_counts.items():
            with engine.begin() as c:
                try:
                    c.execute(
                        text("""
                            INSERT INTO entities
                                (entity_type, name, normalized_name, is_government,
                                 first_seen_at, last_seen_at, mention_count,
                                 created_at, updated_at)
                            VALUES ('case', :name, :norm, false,
                                    NOW(), NOW(), :count, NOW(), NOW())
                            ON CONFLICT (normalized_name) DO UPDATE
                                SET mention_count = :count2,
                                    last_seen_at = NOW()
                        """),
                        {"name": case, "norm": case.upper(),
                         "count": count, "count2": count},
                    )
                    persisted += 1
                except Exception as e:
                    log.warning("  Failed to persist %s: %s", case, e)

        log.info("Persisted %d case entities", persisted)

    # Final count
    with engine.connect() as c:
        final_total = c.execute(
            text("SELECT COUNT(*) FROM entities WHERE entity_type = 'case'")
        ).scalar()
    log.info("Final case entity count: %d", final_total)

    # Count by jurisdiction prefix (letter prefix, not numeric)
    prefix_counts: dict[str, int] = {}
    for case in case_counts:
        parts = case.split("-")
        # Find the first non-numeric part as the prefix
        prefix = "OTHER"
        for p in parts:
            if not p.isdigit():
                prefix = p.upper()
                break
        if prefix == "OTHER" and parts:
            prefix = parts[0]
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1

    log.info("── Case numbers by prefix (top 20) ──")
    for prefix, count in sorted(prefix_counts.items(), key=lambda x: -x[1])[:20]:
        log.info("  %-10s  %d cases", prefix, count)


if __name__ == "__main__":
    main()
