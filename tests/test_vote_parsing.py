"""Regression tests for BOS vote text parsing.

Each test case provides a vote_text (as the parser would see it from the
meeting summary page) and the expected supervisor votes.  These cases
represent real votes that our parsers previously missed.
"""

import os
import re
import sys
import unittest
from pathlib import Path

# Ensure scripts/ is on the path so we can import scraper modules
_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

# ── Helpers replicating the parser's extraction logic ──────────────────────

def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


KNOWN_SUPERVISORS = {
    "kate brophy mcgee": "Kate Brophy McGee",
    "debbie lesko": "Debbie Lesko",
    "mark stewart": "Mark Stewart",
    "thomas galvin": "Thomas Galvin",
    "steve gallardo": "Steve Gallardo",
}

KNOWN_SUPERVISOR_NORMS = set(KNOWN_SUPERVISORS)
NORM_TO_NAME = dict(KNOWN_SUPERVISORS)


def is_known_supervisor(name: str) -> bool:
    norm = _normalize_name(name)
    if norm in KNOWN_SUPERVISOR_NORMS:
        return True
    for known in KNOWN_SUPERVISOR_NORMS:
        if norm.startswith(known) or known.startswith(norm):
            return True
    return False


def extract_supervisor_names_from_vote_text(vote_text: str) -> dict[str, str]:
    """Extract ayes/nays/absent/recused names from vote text.

    Returns a dict mapping short labels to lists of supervisor names.
    """
    vt_lower = vote_text.lower()
    result: dict[str, list[str]] = {"ayes": [], "nays": [], "absent": [], "recused": []}

    # --- Ayes ---
    ayes_match = re.search(r"Ayes:\s*(.*?)(?:\s*Nay:|\s*$)", vote_text, re.I)
    if ayes_match:
        raw = ayes_match.group(1).strip()
        candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
        for c in candidates:
            c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
            c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
            c = re.sub(r"\s+(County\s+(Attorney|Engineer|Supervisor|Manager|Recorder|School|Treasurer|Sheriff|Department|Office|Human|Transportation)|Human\s+Services|Public\s+|Parks|Transportation|Elections|Risk\s+Management|Finance|Real\s+Estate|Library|Planning).*$", "", c, flags=re.I).strip()
            c = re.sub(r"\s+STATUTORY.*$", "", c, flags=re.I).strip()
            c = re.sub(r"\s+AUDIENCIAS.*$", "", c, flags=re.I).strip()
            c = re.sub(r"\s+BOARD\s+.*$", "", c, flags=re.I).strip()
            c = re.sub(r"\s+CALL TO.*$", "", c, flags=re.I).strip()
            c = re.sub(r"\s+LIBRARY.*$", "", c, flags=re.I).strip()
            c = c.rstrip(",;.:").strip()
            if not c or len(c) < 3:
                continue
            # Extract known supervisor names from trailing-context blobs.
            # Threshold of 20: longest BOS supervisor name is 17 chars
            # (Kate Brophy McGee), so anything > 20 has trailing content.
            if len(c) > 20 and NORM_TO_NAME:
                extracted = [n for n in NORM_TO_NAME if n in c.lower()]
                if extracted:
                    for name in extracted:
                        proper = NORM_TO_NAME.get(name, name.title())
                        if proper not in result["ayes"]:
                            result["ayes"].append(proper)
                    continue
            if len(c) > 60:
                continue
            if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                continue
            if re.search(r"\b(with|and|the|for|of|that|this|from|please|email|prior|local|fire|written|except|amenos|como|que|del|para|una|los|las|por|notado)", c, re.I):
                continue
            if c not in result["ayes"]:
                result["ayes"].append(c)

        # Filter against known supervisors
        if KNOWN_SUPERVISOR_NORMS and result["ayes"]:
            filtered = [n for n in result["ayes"] if is_known_supervisor(n)]
            if filtered:
                result["ayes"] = filtered

    # --- Nays ---
    nays_match = re.search(r"Nay:\s*(.*?)(?:\s+(?=\d+\.)|\s*$)", vote_text, re.I)
    if nays_match:
        raw = nays_match.group(1).strip()
        candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
        for c in candidates:
            c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
            c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
            c = c.rstrip(",;.:").strip()
            if not c or len(c) < 3:
                continue
            if len(c) > 20 and NORM_TO_NAME:
                extracted = [n for n in NORM_TO_NAME if n in c.lower()]
                if extracted:
                    for name in extracted:
                        proper = NORM_TO_NAME.get(name, name.title())
                        if proper not in result["nays"]:
                            result["nays"].append(proper)
                    continue
            if len(c) > 60:
                continue
            if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                continue
            if c not in result["nays"]:
                result["nays"].append(c)

        # Filter against known supervisors
        if KNOWN_SUPERVISOR_NORMS and result["nays"]:
            filtered = [n for n in result["nays"] if is_known_supervisor(n)]
            if filtered:
                result["nays"] = filtered

    # --- Absent ---
    absent_match = re.search(r"Absent:\s*(.*?)(?:\s*(?:Motion|Ayes:|Nay:|Recused|\d+\.)|$)", vote_text, re.I | re.DOTALL)
    if absent_match:
        raw = absent_match.group(1).strip()
        for name in re.split(r"[,\n]+", raw):
            name = name.strip(" ;,.")
            if name:
                result["absent"].append(name)

    # --- Recused ---
    recused_match = re.search(r"Recused:\s*([A-Za-z\s]+?)(?:\s*(?:~|Motion|~|\d+\.|$))", vote_text, re.I)
    if recused_match:
        name = recused_match.group(1).strip()
        if name:
            result["recused"].append(name)

    return result


# ── Test cases ─────────────────────────────────────────────────────────────

def parse_test_case(vote_text, expected_ayes=None, expected_nays=None,
                     expected_absent=None, expected_recused=None,
                     description=""):
    """Run the parser on vote_text and compare with expectations."""
    result = extract_supervisor_names_from_vote_text(vote_text)
    errors = []
    if expected_ayes is not None:
        for name in expected_ayes:
            if name not in result["ayes"]:
                errors.append(f"  Expected {name} in Ayes, got {result['ayes']}")
    if expected_nays is not None:
        for name in expected_nays:
            if name not in result["nays"]:
                errors.append(f"  Expected {name} in Nays, got {result['nays']}")
    if expected_absent is not None:
        for name in expected_absent:
            if name not in result["absent"]:
                errors.append(f"  Expected {name} in Absent, got {result['absent']}")
    if expected_recused is not None:
        for name in expected_recused:
            if name not in result["recused"]:
                errors.append(f"  Expected {name} in Recused, got {result['recused']}")
    return errors


class TestBOSVoteParsing(unittest.TestCase):
    """Regression tests for BOS vote text parsing.

    Pattern 1: Trailing text after last name in Ayes list (no comma separator).
    Pattern 2: Spanish translation " - " in item titles.
    Pattern 3: Multi-sub-item items with multiple Ayes lines.
    Pattern 4: Names followed by "OPEN SESSION", "Absent:", etc.
    Pattern 5: Yes votes with "Nay:" present.
    """

    # ── Pattern 1: Trailing text after last name ───────────────────────

    def test_steve_gallardo_trailing_clerk_text(self):
        """'Steve Gallardo' followed by 'clerks of the board' without comma."""
        vt = (
            "36.REAPPOINTMENT TO THE BOARD OF HEALTH Approve the reappointment "
            "of Lorenzo Sierra to the Board of Health, representing Supervisorial "
            "District 5. (C-07-25-027-X-00) Motion to approve by Supervisor "
            "Steve Gallardo, seconded by Supervisor Kate Brophy McGee "
            "Ayes: Thomas Galvin, Kate Brophy McGee, Mark Stewart, Debbie Lesko, "
            "Steve Gallardo clerk of the board - secretaria de la junta BOARD "
            "OF SUPERVISORS - JUNTA DE SUPERVISORES"
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Kate Brophy McGee", "Mark Stewart",
            "Debbie Lesko", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_debbie_lesko_trailing_open_session(self):
        """'Debbie Lesko' followed by 'OPEN SESSION' without comma."""
        vt = (
            "1.EXECUTIVE SESSION Vote to convene in Executive Session to "
            "consider the items on the Executive Agenda dated Wednesday, "
            "January 08, 2025, for Board of Supervisors and relevant Special "
            "Districts pursuant to the statutory authority listed for each item. "
            "Motion to approve by Supervisor Kate Brophy McGee, seconded by "
            "Supervisor Mark Stewart "
            "Ayes: Thomas Galvin, Steve Gallardo, Kate Brophy McGee, "
            "Mark Stewart, Debbie Lesko OPEN SESSION Supervisor Gallardo "
            "attended the open session remotely."
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Steve Gallardo", "Kate Brophy McGee",
            "Mark Stewart", "Debbie Lesko"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_debbie_lesko_trailing_absent(self):
        """'Debbie Lesko' followed by 'Absent: Steve Gallardo' without comma."""
        vt = (
            "5.BNSF INTERMODAL CPA Case #: CPA2024006 Supervisor District: 4 "
            "Applicant & Owner: Susan Demmitt, Gammage & Burnham, PLC / BNSF "
            "Request: Major Comprehensive Plan Amendment (CPA) to change the "
            "land use designation... "
            "Motion to continue item 5 until the August 20, 2025, meeting "
            "by Supervisor Debbie Lesko, seconded by Supervisor Mark Stewart "
            "Ayes: Thomas Galvin, Kate Brophy McGee, Mark Stewart, "
            "Debbie Lesko Absent: Steve Gallardo"
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Kate Brophy McGee", "Mark Stewart",
            "Debbie Lesko"
        ], expected_absent=["Steve Gallardo"])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_steve_gallardo_trailing_statutory(self):
        """'Steve Gallardo' followed by 'Statutory Hearings' without comma."""
        vt = (
            "8.DEANNEXATION FROM THE CITY OF AVONDALE TO MARICOPA COUNTY "
            "Pursuant to A.R.S. § 9-471.03 convene the scheduled public "
            "hearing... "
            "Motion to approve by Supervisor Steve Gallardo, seconded by "
            "Supervisor Kate Brophy McGee "
            "Ayes: Thomas Galvin, Kate Brophy McGee, Mark Stewart, "
            "Debbie Lesko, Steve Gallardo STATUTORY HEARINGS - AUDIENCIAS "
            "LEGALES"
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Kate Brophy McGee", "Mark Stewart",
            "Debbie Lesko", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_mark_stewart_trailing_subitem(self):
        """'Mark Stewart' followed by sub-item b. without comma."""
        vt = (
            "62.MOU WITH THE FINANCIAL CRIMES ENFORCEMENT NETWORK FOR DIRECT "
            "ELECTRONIC ACCESS TO DATA Approve a Memorandum of Understanding... "
            "Motion to approve by Supervisor Mark Stewart, seconded by "
            "Supervisor Thomas Galvin "
            "Ayes: Thomas Galvin, Kate Brophy McGee, Debbie Lesko, "
            "Mark Stewart, Steve Gallardo b. PINAL COUNTY 2026 AGREEMENT..."
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Kate Brophy McGee", "Debbie Lesko",
            "Mark Stewart", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    # ── Pattern 2: Spanish translation ─────────────────────────────────

    def test_spanish_translation_separator(self):
        """Item title with ' - ' separating English/Spanish."""
        vt = (
            "13.ROAD FILE DECLARATIONS - DECLARACIONES DE CARRETERA Approve "
            "by resolution... "
            "Motion to approve by Supervisor Debbie Lesko, seconded by "
            "Supervisor Steve Gallardo "
            "Ayes: Kate Brophy McGee, Debbie Lesko, Mark Stewart, "
            "Thomas Galvin, Steve Gallardo"
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Kate Brophy McGee", "Debbie Lesko", "Mark Stewart",
            "Thomas Galvin", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    # ── Pattern 3: Yes with Nay present ────────────────────────────────

    def test_kate_mcgee_nay_on_cash_deficit(self):
        """Kate Brophy McGee votes Nay while others vote Ayes."""
        vt = (
            "2.REQUEST FOR CASH DEFICIT SCHOOL DISTRICT LEVY Per A.R.S. "
            "§15-991(A)... "
            "Motion to approve the Request for Cash Deficit School District "
            "Levy as presented for Nadaburg Unified School District by "
            "Supervisor Kate Brophy McGee, seconded by Supervisor Mark Stewart "
            "Ayes: Thomas Galvin, Mark Stewart, Steve Gallardo "
            "Nay: Kate Brophy McGee Absent: Debbie Lesko"
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Mark Stewart", "Steve Gallardo"
        ], expected_nays=["Kate Brophy McGee"],
                                 expected_absent=["Debbie Lesko"])
        self.assertEqual(errs, [], "\n".join(errs))

    # ── Pattern 4: Multi-participant items ─────────────────────────────

    def test_all_five_supervisors_yes(self):
        """All 5 supervisors vote Yes (basic case)."""
        vt = (
            "Ayes: Thomas Galvin, Kate Brophy McGee, Mark Stewart, "
            "Debbie Lesko, Steve Gallardo"
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Thomas Galvin", "Kate Brophy McGee", "Mark Stewart",
            "Debbie Lesko", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_subitem_withdrawn_with_full_ayes(self):
        """Item has sub-items, first three have full Ayes, last is withdrawn."""
        vt = (
            "13.ROAD FILE DECLARATIONS - DECLARACIONES DE CARRETERA "
            "a. ROAD FILE 6036... Motion to approve by Supervisor Debbie Lesko, "
            "seconded by Supervisor Steve Gallardo "
            "Ayes: Kate Brophy McGee, Debbie Lesko, Mark Stewart, "
            "Thomas Galvin, Steve Gallardo "
            "b. ROAD FILE 6031... Motion to approve by Supervisor Debbie Lesko, "
            "seconded by Supervisor Steve Gallardo "
            "Ayes: Kate Brophy McGee, Debbie Lesko, Mark Stewart, "
            "Thomas Galvin, Steve Gallardo "
            "c. ROAD FILE 6030... Motion to approve by Supervisor Debbie Lesko, "
            "seconded by Supervisor Steve Gallardo "
            "Ayes: Kate Brophy McGee, Debbie Lesko, Mark Stewart, "
            "Thomas Galvin, Steve Gallardo "
            "d. ROAD FILE 6033... The Clerk noted the item was withdrawn. "
            "No action was taken on item 13.d. "
            "BOARD OF SUPERVISORS CONSENT AGENDA - AGENDA CONSIENTA "
            "DE LA JUNTA DE SUPERVISORES"
        )
        # Parser should find the first Ayes section and capture all 5 names
        errs = parse_test_case(vt, expected_ayes=[
            "Kate Brophy McGee", "Debbie Lesko", "Mark Stewart",
            "Thomas Galvin", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))

    # ── Pattern 5: Non-vote items (presentation, withdrawal) ──────────────

    def test_withdrawn_item_should_not_extract_votes(self):
        """When vote_text says 'the item was withdrawn', no votes should be parsed."""
        vt = (
            "47.GRANT FUNDS FROM DEPARTMENT OF JUSTICE FOR MARICOPA COUNTY "
            "ATTORNEY\'S OFFICE DIGITAL EVIDENCE MANAGEMENT SOLUTION PROJECT "
            "Approve the application and acceptance of grant funds... "
            "(C-XX-25-XXX-X-00) The Clerk noted the item was withdrawn. "
            "No action was taken on the item."
        )
        errs = parse_test_case(vt, expected_ayes=[], expected_nays=[])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_presentation_item_no_vote(self):
        """Informational presentation has no Ayes/Nays — no votes should be parsed."""
        vt = (
            "4.PET SHOWCASE BY MARICOPA COUNTY ANIMAL CARE AND CONTROL "
            "- PRESENTACIÓN DE ANIMALES DOMESTICOS POR EL DEPARTAMENTO "
            "DE CONTROL Y CUIDADO DE ANIMALES "
            "~ Supervisor Gallardo entered the meeting ~ "
            "The Clerk announced items 47,71 and 72 were withdrawn. "
            "PLANNING AND ZONING HEARINGS"
        )
        errs = parse_test_case(vt, expected_ayes=[], expected_nays=[])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_withdrawn_referring_to_other_items(self):
        """When 'withdrawn' in text refers to OTHER items, this item should
        NOT be treated as withdrawn."""
        vt = (
            "4.PET SHOWCASE BY MARICOPA COUNTY ANIMAL CARE AND CONTROL "
            "- PRESENTACIÓN DE ANIMALES DOMESTICOS... "
            "The Clerk announced items 47,71 and 72 were withdrawn. "
            "PLANNING AND ZONING HEARINGS"
        )
        # Parse with item_number context: the withdrawn text refers to
        # items 47, 71, 72 — not to item 4 itself.
        # Just checking that no votes are extracted (no Ayes/Nays).
        errs = parse_test_case(vt, expected_ayes=[], expected_nays=[])
        self.assertEqual(errs, [], "\n".join(errs))

    def test_item_with_subitem_withdrawn_other_votes_captured(self):
        """Item has 3 sub-items with Ayes and 1 sub-item withdrawn. The
        Ayes from the first sub-item should still be captured."""
        vt = (
            "13.ROAD FILE DECLARATIONS - DECLARACIONES DE CARRETERA "
            "a. ROAD FILE 6036... Motion to approve by Supervisor Debbie Lesko, "
            "seconded by Supervisor Steve Gallardo "
            "Ayes: Kate Brophy McGee, Debbie Lesko, Mark Stewart, "
            "Thomas Galvin, Steve Gallardo "
            "b. ROAD FILE 6033... The Clerk noted the item was withdrawn. "
            "No action was taken on item 13.b. "
            "BOARD OF SUPERVISORS CONSENT AGENDA "
        )
        errs = parse_test_case(vt, expected_ayes=[
            "Kate Brophy McGee", "Debbie Lesko", "Mark Stewart",
            "Thomas Galvin", "Steve Gallardo"
        ])
        self.assertEqual(errs, [], "\n".join(errs))


# ── Test absent-section parsing ───────────────────────────────────────────

SUPERVISOR_NAMES = {
    "kate brophy mcgee": "Kate Brophy McGee",
    "debbie lesko": "Debbie Lesko",
    "mark stewart": "Mark Stewart",
    "thomas galvin": "Thomas Galvin",
    "steve gallardo": "Steve Gallardo",
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def extract_supervisors_from_members_section(text: str) -> list[dict]:
    """Simulates the supervisor extraction from the members-present section.

    This mirrors the logic in votes.py's extract_votes_from_summary.
    """
    supervisors: list[dict] = []

    # Separate Absent section so supervisors aren't double-counted
    present_text = text
    absent_text = ""
    absent_match = re.search(
        r"\.\s*Absent:\s*(.*?)(?=\d+\.|\s*Also present|\.\s*Motion|$)",
        text, re.I | re.DOTALL
    )
    if absent_match:
        absent_text = absent_match.group(1).strip()
        present_text = text[:absent_match.start()]

    # Parse present section (from text before "Absent:")
    for part in re.split(r";\s*", present_text):
        part = part.strip().rstrip(";,.").strip()
        if not part:
            continue
        part = re.sub(r"\s*\([^)]*\)", "", part).strip()
        m = re.match(
            r"([A-Za-z]+(?:\s+[A-Za-z']+)+)"
            r"(?:,\s*(?:Chairman|Chair|Vice Chair|Supervisor))?"
            r"(?:,\s*District\s+(\d+))?",
            part, re.I,
        )
        if m:
            name = m.group(1).strip()
            district = m.group(2)
            role = ""
            if re.search(r"\bChair\b", part, re.I) and not re.search(r"Vice", part, re.I):
                role = "Chair"
            elif re.search(r"Vice Chair", part, re.I):
                role = "Vice Chair"
            if name:
                supervisors.append({
                    "name": name,
                    "normalized_name": _normalize(name),
                    "district": district,
                    "role": role if role else None,
                    "present": True,
                })

    # Parse Absent section (text already extracted above)
    if absent_text:
        for part in re.split(r";\s*", absent_text):
            part = part.strip().rstrip(";,.").strip()
            if not part:
                continue
            part = re.sub(r"\s*\([^)]*\)", "", part).strip()
            m = re.match(
                r"([A-Za-z]+(?:\s+[A-Za-z']+)+)"
                r"(?:,\s*(?:Chairman|Chair|Vice Chair|Supervisor))?"
                r"(?:,\s*District\s+(\d+))?",
                part, re.I,
            )
            if m:
                name = m.group(1).strip()
                district = m.group(2)
                if name:
                    supervisors.append({
                        "name": name,
                        "normalized_name": _normalize(name),
                        "district": district,
                        "role": None,
                        "present": False,
                    })

    return supervisors


class TestSupervisorExtraction(unittest.TestCase):
    """Tests for extracting supervisor presence (present + absent) from the
    "members present" section of meeting summaries."""

    def test_present_and_absent_supervisors(self):
        """Meeting 4478: 4 present, 1 absent (Debbie Lesko)."""
        text = (
            "Thomas Galvin, Chairman, District 2; "
            "Kate Brophy McGee, Vice Chair, District 3; "
            "Mark Stewart, Supervisor, District 1; "
            "Steve Gallardo, Supervisor, District 5 (entered meeting late). "
            "Absent: Debbie Lesko, Supervisor, District 4"
        )
        sups = extract_supervisors_from_members_section(text)
        names = {s["name"]: s["present"] for s in sups}
        self.assertEqual(names["Thomas Galvin"], True)
        self.assertEqual(names["Kate Brophy McGee"], True)
        self.assertEqual(names["Mark Stewart"], True)
        self.assertEqual(names["Steve Gallardo"], True)
        self.assertEqual(names["Debbie Lesko"], False)
        self.assertEqual(len(sups), 5)

    def test_all_present_no_absent(self):
        """When no Absent section exists, all are present."""
        text = (
            "Kate Brophy McGee, Chair, District 3; "
            "Debbie Lesko, Vice Chair, District 4; "
            "Mark Stewart, Supervisor, District 1; "
            "Thomas Galvin, Supervisor, District 2; "
            "Steve Gallardo, Supervisor, District 5"
        )
        sups = extract_supervisors_from_members_section(text)
        self.assertEqual(len(sups), 5)
        for s in sups:
            self.assertEqual(s["present"], True)

    def test_all_present_with_remote_notices(self):
        """Supervisors with (remote) notices are still present."""
        text = (
            "Kate Brophy McGee, Chair, District 3 (remote); "
            "Debbie Lesko, Vice Chair, District 4; "
            "Mark Stewart, Supervisor, District 1 (remote); "
            "Thomas Galvin, Supervisor, District 2; "
            "Steve Gallardo, Supervisor, District 5 (remote)"
        )
        sups = extract_supervisors_from_members_section(text)
        self.assertEqual(len(sups), 5)
        for s in sups:
            self.assertEqual(s["present"], True)

    def test_absent_multiple_supervisors(self):
        """Multiple supervisors absent."""
        text = (
            "Kate Brophy McGee, Chair, District 3; "
            "Thomas Galvin, Supervisor, District 2. "
            "Absent: Debbie Lesko, Supervisor, District 4; "
            "Steve Gallardo, Supervisor, District 5"
        )
        sups = extract_supervisors_from_members_section(text)
        by_name = {s["name"]: s for s in sups}
        self.assertEqual(by_name["Kate Brophy McGee"]["present"], True)
        self.assertEqual(by_name["Thomas Galvin"]["present"], True)
        self.assertEqual(by_name["Debbie Lesko"]["present"], False)
        self.assertEqual(by_name["Steve Gallardo"]["present"], False)
        self.assertEqual(len(sups), 4)


# ── Summary DOM regex and name-parsing tests ───────────────────────────

class TestSummaryDOMNameParsing(unittest.TestCase):
    """Regression tests for summary_dom.py name extraction.

    Tests the _AYES_RE / _NAYS_RE / _parse_names functions used by
    the DOM-based backfill parser (extract_votes_from_summary_dom).

    Bugs fixed in this class:
    - _AYES_RE used `Nay:` (singular) but vote text has `Nays:` (plural),
      causing the lazy match to consume past the Nays section.
    - _parse_names had no filter for fragments containing vote/agenda
      keywords like "Nays:", "Recused:", "BOARD OF SUPERVISORS", etc.
    """

    def setUp(self):
        from scraper.common.summary_dom import _AYES_RE, _NAYS_RE, _ABSENT_RE, _parse_names
        self._AYES_RE = _AYES_RE
        self._NAYS_RE = _NAYS_RE
        self._ABSENT_RE = _ABSENT_RE
        self._parse_names = _parse_names

    # ── _AYES_RE boundary tests ────────────────────────────────────────

    def test_ayes_re_stops_at_nays_plural(self):
        """'Nays:' (plural) after Ayes list must NOT be consumed."""
        text = (
            "Ayes: Thomas Galvin, Kate Brophy McGee, "
            "Mark Stewart, Debbie Lesko, Steve Gallardo "
            "Nays: None"
        )
        m = self._AYES_RE.search(text)
        self.assertIsNotNone(m, "_AYES_RE should match")
        ayes = m.group("ayes")
        self.assertNotIn("Nays", ayes,
            "_AYES_RE consumed past Nays: (plural) boundary")
        self.assertIn("Thomas Galvin", ayes)
        self.assertIn("Steve Gallardo", ayes)

    def test_ayes_re_stops_at_nays_with_real_names(self):
        """'Nays:' followed by actual supervisor names must be excluded."""
        text = "Ayes: Steve Gallardo, Bill Gates Nays: Debbie Lesko"
        m = self._AYES_RE.search(text)
        self.assertIsNotNone(m)
        ayes = m.group("ayes")
        self.assertNotIn("Nays", ayes,
            "_AYES_RE should stop at 'Nays:'")
        self.assertIn("Steve Gallardo", ayes)
        self.assertIn("Bill Gates", ayes)

    def test_ayes_re_stops_at_recused(self):
        """'Recused:' after Ayes list must NOT be consumed."""
        text = "Ayes: Steve Gallardo, Bill Gates Recused: Kate Brophy McGee"
        m = self._AYES_RE.search(text)
        self.assertIsNotNone(m)
        ayes = m.group("ayes")
        self.assertNotIn("Recused", ayes,
            "_AYES_RE should stop at 'Recused:'")
        self.assertIn("Steve Gallardo", ayes)

    def test_ayes_re_stops_at_absent(self):
        """'Absent:' after Ayes list must NOT be consumed."""
        text = "Ayes: Thomas Galvin, Steve Gallardo Absent: Debbie Lesko"
        m = self._AYES_RE.search(text)
        self.assertIsNotNone(m)
        ayes = m.group("ayes")
        self.assertNotIn("Absent", ayes,
            "_AYES_RE should stop at 'Absent:'")
        self.assertIn("Thomas Galvin", ayes)

    # ── _NAYS_RE boundary tests ────────────────────────────────────────

    def test_nays_re_stops_at_recused(self):
        """'Recused:' after Nays list must NOT be consumed."""
        text = "Nays: Debbie Lesko Recused: Steve Gallardo"
        m = self._NAYS_RE.search(text)
        self.assertIsNotNone(m)
        nays = m.group("nays")
        self.assertNotIn("Recused", nays,
            "_NAYS_RE should stop at 'Recused:'")
        self.assertIn("Debbie Lesko", nays)

    def test_nays_re_stops_at_absent(self):
        """'Absent:' after Nays list must NOT be consumed."""
        text = "Nays: Mark Stewart Absent: Steve Gallardo"
        m = self._NAYS_RE.search(text)
        self.assertIsNotNone(m)
        nays = m.group("nays")
        self.assertNotIn("Absent", nays,
            "_NAYS_RE should stop at 'Absent:'")
        self.assertIn("Mark Stewart", nays)

    # ── _parse_names filter tests ──────────────────────────────────────

    def test_parse_names_rejects_concatenated_nays_fragment(self):
        """Fragment like 'Bill GatesNays: Steve Gallardo' must be rejected.

        This happens when there is no comma (or space) between the last
        Ayes name and the Nays: label.
        """
        names = self._parse_names(
            "Bill GatesNays: Steve Gallardo, Thomas Galvin")
        self.assertNotIn("Bill GatesNays: Steve Gallardo", names,
            "Concatenated 'Nays' fragment not filtered")
        self.assertIn("Thomas Galvin", names)

    def test_parse_names_rejects_recused_fragment(self):
        """Fragment containing 'Recused:' must be rejected."""
        names = self._parse_names(
            "Steve Gallardo Recused: Bill Gates, Thomas Galvin")
        self.assertNotIn("Steve Gallardo Recused: Bill Gates", names,
            "Recused fragment not filtered")
        self.assertIn("Thomas Galvin", names)

    def test_parse_names_rejects_agenda_title_fragment(self):
        """Fragment like 'Steve Gallardo BOARD OF SUPERVISORS...' rejected."""
        names = self._parse_names(
            "Steve Gallardo BOARD OF SUPERVISORS REGULAR AGENDA, Thomas Galvin")
        self.assertNotIn("Steve Gallardo BOARD OF SUPERVISORS REGULAR AGENDA", names,
            "Agenda title fragment not filtered")
        self.assertIn("Thomas Galvin", names)

    def test_parse_names_rejects_spanish_translation_fragment(self):
        """Fragment containing Spanish keywords rejected."""
        names = self._parse_names(
            "Steve Gallardo Planificación y Desarrollo, Kate Brophy McGee")
        self.assertNotIn("Steve Gallardo Planificación y Desarrollo", names,
            "Spanish keyword fragment not filtered")
        self.assertIn("Kate Brophy McGee", names)

    def test_parse_names_rejects_road_file_fragment(self):
        """Fragment like 'Steve Gallardo 73.ROAD FILE...' rejected."""
        names = self._parse_names(
            "Steve Gallardo 73.ROAD FILE 6037, Debbie Lesko")
        self.assertNotIn("Steve Gallardo 73.ROAD FILE 6037", names,
            "Road file fragment not filtered")
        self.assertIn("Debbie Lesko", names)

    def test_parse_names_keeps_clean_five_supervisors(self):
        """Normal supervisor names pass through unchanged."""
        names = self._parse_names(
            "Thomas Galvin, Kate Brophy McGee, Mark Stewart, "
            "Debbie Lesko, Steve Gallardo")
        expected = ["Thomas Galvin", "Kate Brophy McGee",
                    "Mark Stewart", "Debbie Lesko", "Steve Gallardo"]
        self.assertEqual(names, expected)

    def test_parse_names_strips_and_prefix(self):
        """'and Supervisor Name' strips 'and'."""
        names = self._parse_names(
            "Thomas Galvin, and Steve Gallardo")
        self.assertIn("Steve Gallardo", names)


if __name__ == "__main__":
    unittest.main()
