"""Regression tests for BOS vote text parsing.

Each test case provides a vote_text (as the parser would see it from the
meeting summary page) and the expected supervisor votes.  These cases
represent real votes that our parsers previously missed.
"""

import re
import unittest


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


if __name__ == "__main__":
    unittest.main()
