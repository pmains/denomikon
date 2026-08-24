"""Shared parsing utilities for Tempe document extraction.

Consolidates functions that were duplicated across tempe_summary.py,
tempe_drc_summary.py, and tempe_hpc_summary.py.
"""

from __future__ import annotations

import io
import logging
import re

log = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    """Collapse whitespace and fix hyphenated line breaks."""
    text = re.sub(r"(\w)-\n+(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF byte stream."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n".join(parts)
