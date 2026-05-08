"""Extract votes, conditions, and member rosters from P&Z minutes PDFs."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from scraper.utils import CASE_PATTERN


def parse_pz_minutes_pdf(filepath: str) -> dict:
    """Parse a P&Z Minutes PDF and extract structured vote/condition data.

    Returns:
      {
        "members_present": ["Linda Milhaven", "Jan Leighton", ...],
        "members_absent": ["Kevin Danzeisen"],
        "votes": [
          {
            "c_numbers": ["MCP250001", "Z250044"],  # consent: multiple cases
            "mover": "Commissioner Toma",
            "seconder": "Commissioner Leighton",
            "motion_result": "approved",
            "ayes": ["Finter", "Hernandez", ...],
            "nays": ["Rochwalik"],
            "vote_text": "Commissioner Toma adopted a motion...",
            "conditions": "a. Development...\nb. ...",
          },
          ...
        ],
        "member_vote_records": [
          {"name": "Linda Milhaven", "vote": "aye", "meeting_id": ""},
          ...
        ],
      }
    """
    if not filepath or not Path(filepath).exists():
        return {"members_present": [], "members_absent": [], "votes": []}

    text = _pdf_to_text(filepath)
    if not text:
        return {"members_present": [], "members_absent": [], "votes": []}

    lines = text.split("\n")

    members_present = _extract_members(lines, "MEMBERS PRESENT")
    members_absent = _extract_members(lines, "MEMBERS ABSENT")

    # Build a combined list of all unique commissioner last names for vote matching
    commissioner_last_names = _extract_last_names(members_present + members_absent)

    # Parse the document into sections with their C-numbers and vote blocks
    sections = _parse_sections(lines)

    votes = []
    section_cnumbers: list[str] = []
    current_section_type = ""

    for entry in sections:
        if entry["type"] == "section_header":
            current_section_type = entry.get("section_type", "")
            # Reset accumulated C-numbers for this section
            section_cnumbers = []
        elif entry["type"] == "c_number":
            cn = entry["c_number"]
            if cn and cn not in section_cnumbers:
                section_cnumbers.append(cn)
        elif entry["type"] == "vote":
            vote_data = _parse_commission_action(
                entry["text"],
                section_cnumbers,
                commissioner_last_names,
            )
            # Skip votes that have no C-numbers (e.g., minutes approval)
            if vote_data and vote_data.get("c_numbers"):
                votes.append(vote_data)
            # Reset so C-numbers before the NEXT vote block are fresh
            section_cnumbers = []

    return {
        "members_present": members_present,
        "members_absent": members_absent,
        "votes": votes,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _pdf_to_text(filepath: str) -> str:
    """Run pdftotext -layout and return the full text."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            txt_path = f.name
        subprocess.run(
            ["pdftotext", "-layout", filepath, txt_path],
            capture_output=True, timeout=30,
        )
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        Path(txt_path).unlink(missing_ok=True)
        return text
    except Exception:
        return ""


def _extract_members(lines: list[str], heading: str) -> list[str]:
    """Extract commissioner names from MEMBERS PRESENT / MEMBERS ABSENT sections.

    Format:
        MEMBERS PRESENT:     In-person
                             Ms. Linda Milhaven, Chair
                             Mr. Derrik Rochwalik
                             ...
    """
    members: list[str] = []
    in_section = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith(heading):
            in_section = True
            # The heading line itself may contain names:
            # "MEMBERS ABSENT:             Mr. Kevin Danzeisen"
            after_heading = stripped[len(heading):].strip().lstrip(":;,").strip()
            if after_heading:
                h_match = re.match(
                    r"^(Mr|Ms|Mrs)\.\s+([A-Za-z]+(?:\s+[A-Za-z]+)+)",
                    after_heading
                )
                if h_match:
                    full_name = h_match.group(2).strip()
                    if full_name not in members:
                        members.append(full_name)
            continue

        if in_section:
            # Stop at blank line or next section heading
            if not stripped:
                break
            if stripped.startswith("MEMBERS") and ":" in stripped:
                break
            if stripped.startswith("STAFF"):
                break

            # Skip "In-person", "GoToWebinar" labels
            if stripped.lower() in ("in-person", "gotowebinar"):
                continue

            # Extract name: "Ms. Linda Milhaven, Chair" or "Mr. Erik Hernandez, Vice Chair (left...)"
            # Match patterns like "Ms. First Last" or "Mr. First Last"
            name_match = re.match(
                r"^(Mr|Ms|Mrs)\.\s+([A-Za-z]+(?:\s+[A-Za-z]+)+)", stripped
            )
            if name_match:
                full_name = name_match.group(2).strip()
                if full_name not in members:
                    members.append(full_name)

    return members


def _extract_last_names(members: list[str]) -> list[str]:
    """Extract last names from full names for vote matching.

    "Linda Milhaven" -> "Milhaven"
    "Erik Hernandez" -> "Hernandez"
    """
    last_names = []
    for full_name in members:
        parts = full_name.split()
        if parts:
            last_names.append(parts[-1])
    return last_names


def _parse_sections(lines: list[str]) -> list[dict]:
    """Parse the document into an ordered list of entries.

    Each entry is one of:
        {"type": "section_header", "section_type": "consent", "line": N}
        {"type": "c_number", "c_number": "Z250032", "line": N}
        {"type": "vote", "text": "...", "line": N, "end_line": N}
    """
    entries: list[dict] = []

    SECTION_PATTERN = re.compile(
        r"^\s*(CONTINUANCE|WITHDRAWN|CONSENT|REGULAR)\s+AGENDA\s*$", re.I
    )

    SECTION_TYPE_MAP = {
        "continuance": "continuance",
        "withdrawn": "withdrawn",
        "consent": "consent",
        "regular": "regular",
    }

    in_vote = False
    vote_lines: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if in_vote:
                vote_lines.append(stripped)
            continue

        # Detect COMMISSION ACTION marker — this starts a vote block
        if "COMMISSION ACTION" in stripped:
            if vote_lines:
                entries.append({
                    "type": "vote",
                    "text": "\n".join(vote_lines),
                })
                vote_lines = []
            in_vote = True
            vote_lines.append(stripped)
            continue

        # Inside a vote block, accumulate everything until next section break
        if in_vote:
            # Check for section header (next section = vote block ends)
            sm = SECTION_PATTERN.match(stripped)
            if sm:
                entries.append({
                    "type": "vote",
                    "text": "\n".join(vote_lines),
                })
                vote_lines = []
                in_vote = False

                section_type = SECTION_TYPE_MAP.get(sm.group(1).lower(), sm.group(1).lower())
                entries.append({
                    "type": "section_header",
                    "section_type": section_type,
                    "line": i,
                })
                continue

            # Check for Other Matters / Adjournment (vote block ends)
            if re.match(r"^Other\s+Matters$|^Adjournment$", stripped, re.I):
                entries.append({
                    "type": "vote",
                    "text": "\n".join(vote_lines),
                })
                vote_lines = []
                in_vote = False
                continue

            vote_lines.append(stripped)
            continue

        # Outside vote — check for section header
        sm = SECTION_PATTERN.match(stripped)
        if sm:
            section_type = SECTION_TYPE_MAP.get(sm.group(1).lower(), sm.group(1).lower())
            entries.append({
                "type": "section_header",
                "section_type": section_type,
                "line": i,
            })
            continue

        # Outside vote — check for C-number
        cn = _extract_single_c_number(stripped)
        if cn:
            entries.append({"type": "c_number", "c_number": cn, "line": i})
            continue

    # Flush final vote
    if vote_lines:
        entries.append({
            "type": "vote",
            "text": "\n".join(vote_lines),
        })

    return entries


def _extract_single_c_number(text: str) -> Optional[str]:
    """Extract a C-number from a line that represents a case item.

    Matches:
      "Z250032"
      "Zoning - Z250032"
      "Military Compatibility Permit - MCP250001"
      "Z250045"
      "CPA2023007"

    Does NOT match conditions headers like "MCP250001 conditions;"
    """
    # Exclude conditions headers: "CASE conditions;" or "CASE conditions:"
    if re.search(r"[A-Z]+-?\d{3,}\s+conditions\s*[;:]", text, re.I):
        return None

    # Primary: match at start or after " - " separator
    m = re.search(r"(?:^|\s*[-–—]\s*)(" + CASE_PATTERN.pattern + r")", text)
    if m:
        return m.group(1).strip()

    # Fallback: any C-number in the text
    m2 = CASE_PATTERN.search(text)
    if m2:
        return m2.group(1).strip()

    return None


def _parse_commission_action(
    text: str,
    section_cnumbers: list[str],
    commissioner_last_names: list[str],
) -> Optional[dict]:
    """Parse a COMMISSION ACTION: block.

    Example text:
      "COMMISSION ACTION: Commissioner Toma adopted a motion recommending the Board of
       Supervisors approve the consent agenda. MCP250001 with conditions 'a'-'g' and
       Z250044 with conditions 'a'-'i'. Commissioner Rochwalik second. Approved 8-0.
       Ayes: Finter, Hernandez, Leighton, Lindblom, Milhaven, Rochwalik, Toma, Whitney."

    Returns dict or None on failure.
    """
    # Remove the COMMISSION ACTION: prefix
    text = re.sub(r"^COMMISSION ACTION:\s*", "", text).strip()
    if not text:
        return None

    # Determine motion_result
    motion_result = "unknown"
    if re.search(r"\bapproved\b", text, re.I):
        motion_result = "approved"
    elif re.search(r"\bdenied\b", text, re.I):
        motion_result = "denied"
    elif re.search(r"\bcontinued?\b", text, re.I):
        motion_result = "continued"
    elif re.search(r"\bwithdrawn\b|was withdrawn", text, re.I):
        motion_result = "withdrawn"

    # Skip "No action required" actions
    if "no action required" in text.lower():
        return None

    # Skip minutes-approval actions (no meaningful motion)
    if re.search(r"approved the .* minutes as written", text, re.I):
        return None

    # Extract mover: "Commissioner Toma adopted a motion" or "Chair Milhaven approved"
    mover = ""
    m_match = re.search(r"(?:Commissioner|Chair)\s+([A-Za-z]+)\s+(?:adopted\s+a\s+motion|approved)", text, re.I)
    if m_match:
        mover = f"Commissioner {m_match.group(1).strip()}"

    # Extract seconder: "Commissioner Rochwalik second"
    seconder = ""
    s_match = re.search(r"(?:Commissioner|Chair)\s+([A-Za-z]+)\s+second", text, re.I)
    if s_match:
        seconder = f"Commissioner {s_match.group(1).strip()}"

    # Extract result: "Approved 8-0" or "Approved 7-1"
    result_match = re.search(r"Approved\s+(\d+)[–—-](\d+)", text, re.I)
    result_text = result_match.group(0) if result_match else ""

    # Only look at first ~1500 chars for Ayes/Nays — the actual
    # motion text and vote results appear before conditions begin
    vote_head = text[:1500]
    # Grab the first ~500 chars (where Ayes/Nays typically appear)
    vote_tail = vote_head[:500]

    # Extract Ayes: find "Ayes:" in vote_tail, collect names until
    # we hit "Nays:" or conditions text (lowercase-starting lines)
    ayes: list[str] = []
    nays: list[str] = []

    # Flatten first 800 chars — Ayes/Nays appear in the first few lines
    flat_head = vote_head[:800].replace("\n", " ")

    # Ayes: extract comma-separated names until "Nays:" or conditions text
    ayes_raw = ""
    a_m = re.search(r"Ayes?:\s*([A-Z][a-z]+(?:,\s*[A-Z][a-z]+)*)", flat_head)
    if a_m:
        ayes_raw = a_m.group(1)
    ayes = [n.strip() for n in ayes_raw.split(",") if n.strip()]

    # Nays: extract comma-separated names similarly
    nays_raw = ""
    n_m = re.search(r"Nays?:\s*([A-Z][a-z]+(?:,\s*[A-Z][a-z]+)*)", flat_head)
    if n_m:
        nays_raw = n_m.group(1)
    nays = [n.strip() for n in nays_raw.split(",") if n.strip()]

    # Determine which C-numbers this vote applies to
    # Use C-numbers found in the vote text, plus any from the section
    vote_cnumbers = list(section_cnumbers)
    for cn_match in CASE_PATTERN.finditer(text):
        cn = cn_match.group(1).strip()
        if cn and cn not in vote_cnumbers:
            vote_cnumbers.append(cn)

    # Extract conditions from the vote text
    conditions = _extract_conditions_from_text(text)

    return {
        "c_numbers": vote_cnumbers,
        "mover": mover,
        "seconder": seconder,
        "motion_result": motion_result,
        "result_text": result_text,
        "ayes": ayes,
        "nays": nays,
        "vote_text": text[:10000],
        "conditions": conditions,
    }


def _clean_commissioner_name(name: str) -> str:
    """Clean a commissioner name extracted from vote text.

    Strips trailing periods, leading/trailing whitespace, and filters
    out single-letter entries (from conditions lists like 'a'-'p').
    """
    name = name.strip().rstrip(".")
    # Skip single letters (from conditions lists like "a.", "b." etc.)
    if len(name) <= 1:
        return ""
    return name


def _extract_conditions_from_text(vote_text: str) -> Optional[str]:
    """Extract conditions text from a full COMMISSION ACTION block.

    Conditions start with a header like "Z250044 conditions;" and continue
    until the end of the vote block.  The header itself is excluded from
    the returned conditions text.
    """
    # Find the conditions header pattern
    cond_match = re.search(
        r"[A-Z]+-?\d{3,}\s+conditions\s*[;:]\s*", vote_text, re.I
    )
    if not cond_match:
        return None

    # Return everything after the conditions header
    cond_start = cond_match.end()
    conds = vote_text[cond_start:].strip()
    if not conds:
        return None

    return conds


def _extract_conditions(lines: list[str], vote_start_line: int) -> Optional[str]:
    """Extract conditions text following a vote block.

    Conditions begin with a line like "MCP250001 conditions;" or "Z250044 conditions;"
    and continue until the next section break, COMMISSION ACTION, or page footer.

    Returns the full conditions text, or None if no conditions found.
    """
    cond_start = -1
    cond_end = -1
    in_conditions = False
    conditions_lines: list[str] = []

    for i in range(vote_start_line, len(lines)):
        stripped = lines[i].strip()

        if not stripped:
            if in_conditions:
                conditions_lines.append(stripped)
            continue

        # Check for conditions header: "CASE conditions;"
        if re.search(r"[A-Z]+-?\d{3,}\s+conditions[;:]", stripped, re.I):
            in_conditions = True
            if cond_start < 0:
                cond_start = i
            conditions_lines.append(stripped)
            continue

        # Stop at next section, vote, or page footer
        if in_conditions:
            if re.search(r"COMMISSION ACTION", stripped, re.I):
                cond_end = i
                break
            if re.search(
                r"^(CONTINUANCE|WITHDRAWN|CONSENT|REGULAR)\s+AGENDA\s*$",
                stripped, re.I,
            ):
                cond_end = i
                break
            if re.search(r"^Page\s+\d+\s+of\s+\d+$|^\w+\s+\d+,?\s+\d{4}\s+(?:Planning|ZIPPOR)", stripped, re.I):
                # Page footer — skip it
                continue
            if re.search(r"^Other\s+Matters$|^Adjournment$", stripped, re.I):
                cond_end = i
                break

            conditions_lines.append(stripped)

    if not conditions_lines:
        return None

    return "\n".join(conditions_lines).strip()
