"""
lifecycle.py — Extract lifecycle signals from agenda item text.

Detects whether an agenda item was held, continued, pulled, tabled,
approved, denied, approved as amended, or withdrawn — based on text
patterns found in meeting minutes and agenda item descriptions.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Priority-ordered: first match wins (most specific patterns first)
STATUSES = [
    ("pulled", [
        re.compile(r"pull(?:ed|ing|s)?\s+(?:item|from|the\s+agenda)", re.I),
        re.compile(r"(?:item|request|was|has\s+been)\s+(?:has\s+been\s+)?pull(?:ed|ing|s)\b", re.I),
        re.compile(r"removed\s+(?:from|by)\s+(?:the\s+)?(?:agenda|consent)", re.I),
    ]),
    ("tabled", [
        re.compile(r"\btabl(?:e|ed|ing)\b", re.I),
        re.compile(r"laid\s+(?:over|on\s+the\s+table)", re.I),
    ]),
    ("continued", [
        re.compile(r"continu(?:e|ed|ation|ing)\s+(?:to|until|the\s+item|consideration)", re.I),
        re.compile(r"continued\s+(?:from|public\s+hearing)", re.I),
        re.compile(r"(?:item|hearing|meeting)\s+(?:was\s+)?continued\s+(?:to|until)", re.I),
        re.compile(r"continue\s+the\s+(?:item|hearing|matter|public\s+hearing)", re.I),
    ]),
    ("held", [
        re.compile(r"\b(?:held|hold)\s+(?:over|in\s+abeyance|for\s+?(?:further|additional))", re.I),
        re.compile(r"(?:held|hold)\s+(?:(?:item|case|matter|request|application)\s+)?(?:\S+\s+)?in\s+abeyance", re.I),
        re.compile(r"(?:item|matter|consideration|request|application)\s+(?:was\s+)?(?:held|hold)\s+(?:over|in)", re.I),
        re.compile(r"\bheld\b(?!\s+(?:in|by|at|on\s+(?:behalf|file|hold)|open|accountable))", re.I),
    ]),
    ("withdrawn", [
        re.compile(r"withdraw(?:n|al)?\s+(?:by|from|the\s+(?:item|request))", re.I),
        re.compile(r"(?:item|request|application)\s+(?:was\s+)?withdraw(?:n|al)", re.I),
    ]),
    ("approved_as_amended", [
        re.compile(r"approved\s+(?:as\s+)?(?:amended|modified|revised)", re.I),
        re.compile(r"approve\s+(?:as\s+)?(?:amended|modified)", re.I),
        re.compile(r"adopt(?:ed)?\s+(?:as\s+)?(?:amended|modified|revised)", re.I),
    ]),
    ("denied", [
        re.compile(r"\b(?:denied?|denial)\b(?!\s+(?:the\s+opportunity|access|entry))", re.I),
        re.compile(r"\bdenial\s+of\b", re.I),
        re.compile(r"motion\s+(?:to\s+)?(?:deny|denied)\b", re.I),
        re.compile(r"recommend(?:ation)?\s+(?:of\s+)?denial", re.I),
    ]),
    ("approved", [
        re.compile(r"\b(?:approve(?:d|s)?|approval)\b(?!\s+(?:as\s+amended|as\s+modified))", re.I),
        re.compile(r"adopt(?:ed)?\b", re.I),
        re.compile(r"motion\s+(?:to\s+)?(?:approve|adopt|carry)", re.I),
        re.compile(r"\bcarried\b", re.I),
        re.compile(r"recommend(?:ation)?\s+(?:of\s+)?(?:\w+\s+)?approval", re.I),
    ]),
]

INFORMATIONAL_PATTERNS = [
    re.compile(r"received\s+(?:and\s+)?(?:filed|placed\s+on\s+file)", re.I),
    re.compile(r"information\s+(?:only|item|report)", re.I),
    re.compile(r"presentation\s+only", re.I),
    re.compile(r"not\s+for\s+(?:committee|board)\s+discussion", re.I),
    re.compile(r"no\s+action\s+(?:taken|required|necessary)", re.I),
    re.compile(r"received\s+as\s+information", re.I),
]

# ── Core Logic ──────────────────────────────────────────────────────────────

def classify_lifecycle(text: str) -> str:
    """Classify a single agenda item's lifecycle status from its text.

    Returns one of: held, continued, pulled, tabled, withdrawn,
    approved_as_amended, denied, approved, informational, unknown
    """
    if not text or not text.strip():
        return "unknown"

    for pattern in INFORMATIONAL_PATTERNS:
        if pattern.search(text):
            return "informational"

    for status, patterns in STATUSES:
        for pattern in patterns:
            if pattern.search(text):
                return status

    return "unknown"


def extract_lifecycle_from_items(items: list[dict]) -> list[dict]:
    """Enrich a list of item dicts with lifecycle_status."""
    for item in items:
        text = item.get("agenda_item_text", "") or ""
        item["lifecycle_status"] = classify_lifecycle(text)
    return items


def classify_with_context(text: str, title: str = "", vote_or_action: str = "") -> str:
    """Classify lifecycle with more context.

    Uses vote_or_action field (from structured minutes extraction) if available,
    falling back to agenda_item_text otherwise.
    """
    if vote_or_action and vote_or_action.strip():
        for status, patterns in STATUSES:
            for pattern in patterns:
                if pattern.search(vote_or_action):
                    return status

    return classify_lifecycle(text)
