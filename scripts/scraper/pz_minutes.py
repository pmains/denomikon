"""PZ Minutes parser — extracts commissioner votes from minutes PDFs.

Minutes URLs follow the pattern:
    /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-XXXX

The PDF text contains commissioner attendance, agenda items by case number,
and per-item vote records in a standardised format:

    COMMISSION ACTION: Commissioner [name] adopted a motion recommending the
    Board of Supervisors [approve|deny] [case(s)] with conditions [...].
    Commissioner [name] second. Approved X-Y. Ayes: list. Nays: list.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# Regex to extract case numbers like SU250007, Z250044, MCP250001, CPAZ250011
CASE_RE = re.compile(r"[A-Z]+-?\d{3,}")

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_minutes_pdf(minutes_url: str) -> Optional[str]:
    """Download a minutes PDF to a temporary file.

    Returns path to the temp file, or None on failure.
    """
    try:
        req = urllib.request.Request(
            minutes_url, headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.write(data)
        tmp.close()
        return tmp.name
    except Exception as e:
        log.warning("Failed to download minutes PDF %s: %s", minutes_url, e)
        return None


def extract_minutes_text(pdf_path: str) -> Optional[str]:
    """Extract text from a minutes PDF.

    Uses pdftotext if available; falls back to pypdf.

    Returns the raw text, or None on failure.
    """
    # Try pdftotext first
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # Fallback: use pypdf
    try:
        # Add local pylib to path if pypdf is not yet importable
        import sys as _sys
        try:
            import pypdf
        except ImportError:
            _sys.path.insert(0, "/opt/poliscopic/pylib")
            import pypdf
        reader = pypdf.PdfReader(pdf_path)
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n".join(pages) if pages else None
    except Exception as e:
        log.warning("pypdf extraction failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Commissioner attendance parsing
# ---------------------------------------------------------------------------

_COMMISSIONER_TITLE_RE = re.compile(
    r"(Mr|Ms|Mrs|Dr)\.?\s+([A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)"
)

def parse_commissioners(text: str) -> dict:
    """Extract commissioner names from the MEMBERS PRESENT / ABSENT sections.

    Returns a dict with:
      present: list of {'name': str, 'title': str}
      absent: list of names
    """
    present: list[dict] = []
    absent: list[str] = []
    current_section = None

    # Strip page headers/footers that may interfere
    text = re.sub(r"(?m)^.*Page\s+\d+\s+of\s+\d+\s*$", "", text)
    text = re.sub(
        r"(?m)^Maricopa County Planning and Zoning Commission.*$", "", text,
    )

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        upper = stripped.upper()

        if "MEMBERS PRESENT" in upper:
            current_section = "present"
            continue
        if "MEMBERS ABSENT" in upper:
            current_section = "absent"
            continue
        if "STAFF PRESENT" in upper or "COUNTY AGENCIES" in upper:
            current_section = None
            continue

        if current_section == "present":
            # Skip "In-person" / "GoToWebinar" labels
            if stripped in ("In-person", "GoToWebinar"):
                continue
            m = _COMMISSIONER_TITLE_RE.match(stripped)
            if m:
                name = m.group(2).strip()
                present.append({"name": name, "title": m.group(0)})
        elif current_section == "absent":
            m = _COMMISSIONER_TITLE_RE.match(stripped)
            if m:
                absent.append(m.group(2).strip())

    return {"present": present, "absent": absent}


# ---------------------------------------------------------------------------
# Vote action parsing
# ---------------------------------------------------------------------------

def _normalize_vote_name(name: str) -> str:
    """Normalize a commissioner name for matching."""
    return name.strip().lower()


def _find_case_number_in_context(
    action_text: str,
    minutes_lines: list[str],
    action_line_start: int,
) -> Optional[str]:
    """Try to find the case number referenced in a COMMISSION ACTION.

    Checks:
    1. Direct mention in the action text itself
    2. The closest case number mentioned in lines *before* the action
       (looking backward through section headers and item titles)
    """
    # 1. Check the action text directly
    cases = CASE_RE.findall(action_text)
    if cases:
        return cases[0]

    # 2. Look backward through preceding lines for a case number
    for i in range(action_line_start - 1, max(0, action_line_start - 50), -1):
        line = minutes_lines[i]
        cases = CASE_RE.findall(line)
        if cases:
            # Only take case numbers that are standalone (not part of other text)
            return cases[0]

    return None


def _parse_action_text(action_text: str) -> dict:
    """Parse a single COMMISSION ACTION text block.

    Returns dict with keys:
      commissioner_motion (motion maker)
      commissioner_second (seconder)
      action (approve/deny/continue/withdraw)
      case_number
      motion_result (approved/denied)
      tally_yes, tally_no
      ayes (list of names)
      nays (list of names)
    """
    result: dict = {
        "commissioner_motion": None,
        "commissioner_second": None,
        "action": None,
        "case_number": None,
        "motion_result": None,
        "tally_yes": 0,
        "tally_no": 0,
        "ayes": [],
        "nays": [],
    }

    # Motion maker: "Commissioner [name] adopted a motion"
    m = re.search(
        r"Commissioner\s+(.+?)\s+adopted\s+a\s+motion", action_text, re.I,
    )
    if m:
        result["commissioner_motion"] = m.group(1).strip().rstrip(",")

    # Seconder: "Commissioner [name] second"
    m = re.search(r"Commissioner\s+(.+?)\s+second(?:ed)?", action_text, re.I)
    if m:
        result["commissioner_second"] = m.group(1).strip()

    # Action type and case number
    # "recommending the Board of Supervisors approve SU250007"
    # "recommending the Board of Supervisors deny SU240001"
    m = re.search(
        r"recommending\s+the\s+Board\s+of\s+Supervisors\s+(approve|deny|continue|withdraw)\b",
        action_text, re.I,
    )
    if m:
        result["action"] = m.group(1).lower()

    # Case number — may appear inline with the action or separately
    cases = CASE_RE.findall(action_text)
    if cases:
        result["case_number"] = cases[0]

    # Motion result: "Approved X-Y" or "Denied X-Y" or "Continued"
    m = re.search(r"\b(Approved|Denied|Continued|Withdrawn)\b", action_text, re.I)
    if m:
        result["motion_result"] = m.group(1).lower()

    # Tally: "X-Y" — handle hyphenated-across-lines "70" = "7-0"
    m = re.search(r"(Approved|Denied)\s+(\d+)\s*[-–]\s*(\d+)", action_text, re.I)
    if m:
        result["tally_yes"] = int(m.group(2))
        result["tally_no"] = int(m.group(3))
    else:
        # Handle concatenated tally "70" = "7-0" (hyphen lost across line break)
        m = re.search(r"(Approved|Denied)\s+(\d{2})\b", action_text, re.I)
        if m:
            tally_str = m.group(2)
            # Try splitting a 2-digit string into yes-no
            # Common tallies: 70, 80, 71, 81, 60, 50
            for split_at in range(1, len(tally_str)):
                yes_part = tally_str[:split_at]
                no_part = tally_str[split_at:]
                result["tally_yes"] = int(yes_part)
                result["tally_no"] = int(no_part)
                break  # Just use first split (most common: 70, 80)

        # Fallback: try to find any digit
        if result["tally_yes"] == 0 and result["tally_no"] == 0:
            m = re.search(r"(Approved|Denied)\s+(\d+)", action_text, re.I)
            if m:
                tally_str = m.group(2)
                if len(tally_str) == 2:
                    result["tally_yes"] = int(tally_str[0])
                    result["tally_no"] = int(tally_str[1])

    # Ayes list
    # Ayes list — ends at period followed by space, or at Nays, or at end of string
    m = re.search(
        r"Ayes\s*:\s*([A-Z][a-zA-Z]+(?:,\s*[A-Z][a-zA-Z]+(?:\.\s*)?)*)",
        action_text, re.I,
    )
    if m:
        names = [n.strip() for n in m.group(1).split(",")]
        result["ayes"] = [n for n in names if n]

    # Nays list (may be on its own)
    # Nays list
    m = re.search(r"Nays\s*:\s*([A-Z][a-zA-Z]+(?:,\s*[A-Z][a-zA-Z]+)*)", action_text, re.I)
    if m:
        names = [n.strip() for n in m.group(1).split(",")]
        result["nays"] = [n for n in names if n]

    return result


def aggregate_vote_text(action_text: str) -> str:
    """Collapse multi-line COMMISSION ACTION text into a single line."""
    return " ".join(
        line.strip() for line in action_text.splitlines()
    ).strip()


# ---------------------------------------------------------------------------
# Main minutes parser
# ---------------------------------------------------------------------------

def parse_minutes_votes(text: str) -> list[dict]:
    """Parse all COMMISSION ACTION vote records from minutes text.

    Returns a list of vote dicts, each with:
      commissioner_motion, commissioner_second, action, case_number,
      motion_result, tally_yes, tally_no, ayes (names), nays (names)
    """
    lines = text.splitlines()
    votes: list[dict] = []

    # Find COMMISSION ACTION start lines
    action_starts: list[int] = []
    for i, line in enumerate(lines):
        if line.strip().startswith("COMMISSION ACTION:"):
            action_starts.append(i)

    for start_idx in action_starts:
        # Collect the action text (spans multiple lines until next section)
        action_lines: list[str] = []
        j = start_idx
        while j < len(lines):
            stripped = lines[j].strip()
            if not stripped:
                j += 1
                continue
            # Stop at next COMMISSION ACTION or major section header
            if (
                j > start_idx
                and (
                    stripped.startswith("COMMISSION ACTION:")
                    or stripped.startswith("CONTINUANCE AGENDA")
                    or stripped.startswith("WITHDRAWN AGENDA")
                    or stripped.startswith("CONSENT AGENDA")
                    or stripped.startswith("REGULAR AGENDA")
                    or stripped.startswith("ADJOURNMENT")
                )
            ):
                break
            action_lines.append(stripped)
            j += 1

        full_text = " ".join(action_lines)
        result = _parse_action_text(full_text)

        # If no motion result found, skip (not a vote action, e.g. "minutes approved")
        if not result["motion_result"]:
            continue
        # Skip the "minutes approval" action
        if result["motion_result"] == "approved" and not result["action"]:
            continue

        # Try to find case number from surrounding context if not in action text
        if not result["case_number"]:
            cn = _find_case_number_in_context(
                full_text, lines, start_idx,
            )
            if cn:
                result["case_number"] = cn

        votes.append(result)

    return votes


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def extract_votes_from_minutes(minutes_url: str) -> Optional[list[dict]]:
    """Download minutes PDF, extract text, and parse vote records.

    Returns a list of vote dicts, or None on failure.
    """
    pdf_path = download_minutes_pdf(minutes_url)
    if not pdf_path:
        return None

    try:
        text = extract_minutes_text(pdf_path)
        if not text:
            return None

        commissioners = parse_commissioners(text)
        votes = parse_minutes_votes(text)

        return {
            "commissioners": commissioners,
            "votes": votes,
        }
    finally:
        Path(pdf_path).unlink(missing_ok=True)
