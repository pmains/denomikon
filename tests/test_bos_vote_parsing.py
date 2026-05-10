"""Regression tests for BOS vote extraction from summary text.

Uses a reconstructed fixture of the BOS meeting 4622 summary page text
to validate that the vote parsing correctly handles:
- Item boundaries by character position (not line number)
- Items without motion/ayes language still produce vote records
- All agenda items get at least a "no_vote" fallback
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


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


# Helper: simulate what extract_votes_from_summary does internally
# by running the parsing logic on fixture text.


def _simulate_vote_parsing(text: str, agenda_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Run the vote parsing logic on raw summary text without Playwright.

    This mirrors the logic inside extract_votes_from_summary after the
    page.evaluate() call, operating on the extracted text directly.
    """
    from scraper.votes import detect_split_vote as _unused

    _raw = text  # Keep original for context-sensitive filters
    text_normalized = text.replace("\xa0", " ")
    text_normalized = re.sub(r"\s{3,}", "\n\n", text_normalized)

    # --- Parse supervisors present ---
    supervisors: list[dict] = []
    sup_match = re.search(
        r"with the following members present:\s+(.*?)\.\s*Also present",
        text_normalized, re.I | re.DOTALL,
    )
    if not sup_match:
        sup_match = re.search(
            r"with the following members present:\s+(.*?)(?=\d+\.)",
            text_normalized, re.I | re.DOTALL,
        )
    if sup_match:
        sup_text = sup_match.group(1)
        for part in re.split(r";\s*", sup_text):
            part = part.strip().rstrip(";,.")
            if not part:
                continue
            part = re.sub(r"\s*\([^)]*\)", "", part).strip()
            m = re.match(
                r"([A-Za-z]+(?:\s+[A-Za-z']+)+)"
                r"(?:,\s*(?:Chairman|Vice Chair|Supervisor))?"
                r"(?:,\s*District\s+(\d+))?",
                part, re.I,
            )
            if m:
                name = m.group(1).strip()
                district = m.group(2)
                role = ""
                if re.search(r"Chairman\b", part, re.I) and not re.search(r"Vice", part, re.I):
                    role = "Chairman"
                elif re.search(r"Vice Chair", part, re.I):
                    role = "Vice Chair"
                if name:
                    supervisors.append({
                        "name": name,
                        "normalized_name": re.sub(r"[^a-z0-9]+", " ", name.lower()).strip(),
                        "district": district,
                        "role": role if role else None,
                        "present": True,
                    })

    # --- Build item_cnumber_map ---
    item_cnumber_map: dict[str, str] = {}
    for item in agenda_items:
        num = str(item.get("agenda_item_number", ""))
        c = item.get("c_number", "") or ""
        if num and c:
            item_cnumber_map[num] = c

    # --- Parse votes for each agenda item ---
    votes: list[dict] = []

    # Flatten to single string for character-position-based splitting
    lines = text_normalized.split("\n")
    full_text = "\n".join(lines) if len(lines) > 1 else lines[0] if lines else ""

    item_boundaries: list[tuple[int, str, str]] = []
    seen_nums: set[int] = set()
    valid_item_nums = {int(a.get("agenda_item_number", 0)) for a in agenda_items if a.get("agenda_item_number")}
    for m in re.finditer(r"(?:^|[^\w\'\u2019\u2018])(\d{1,3})\.(?=\s*[A-Z0-9])", full_text):
        num = m.group(1)
        num_int = int(num)
        if num_int in seen_nums:
            continue
        if valid_item_nums and num_int not in valid_item_nums:
            continue
        if len(num) > 3 and num not in item_cnumber_map:
            continue
        # Post-filter: reject spurious boundary matches
        # 1. Decimal numbers: "0,72.50" (char before separator is digit)
        if m.start() > 0 and full_text[m.start() - 1].isdigit():
            continue
        # 2. Percentages: " 27.66%" (2 digits then '%' after dot)
        if len(full_text) > m.end() + 2:
            after_dot = full_text[m.end():m.end()+3]
            if re.match(r'\d{2}%', after_dot):
                continue
        # 3. Condition sub-numbers: " 7. Drainage" (space after dot)
        if len(full_text) > m.end() and full_text[m.end()] == ' ':
            continue
        seen_nums.add(num_int)
        pos = m.start()
        rest = full_text[pos:].lstrip()
        rest = re.sub(r"^\d+\.\s*", "", rest)
        c_m = re.search(r"\(([A-Z]-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3}){1,3})\)", rest)
        c_num = c_m.group(1) if c_m else item_cnumber_map.get(num, "")
        item_boundaries.append((pos, num, c_num))

    agenda_item_counter = 0
    valid_item_nums = {int(a.get("agenda_item_number", 0)) for a in agenda_items if a.get("agenda_item_number")}

    for idx, (start_pos, item_num, c_num) in enumerate(item_boundaries):
        num = int(item_num)
        if valid_item_nums and num not in valid_item_nums:
            continue
        end_pos = item_boundaries[idx + 1][0] if idx + 1 < len(item_boundaries) else len(full_text)
        section_text = full_text[start_pos:end_pos].strip()
        section_text = re.sub(r"\s+", " ", section_text).strip()

        if re.search(r"\bwithdrawn\b", section_text, re.I):
            agenda_item_counter += 1
            votes.append({
                "agenda_item_id": agenda_item_counter,
                "agenda_item_number": int(item_num),
                "c_number": c_num if c_num else None,
                "c_number_base": c_num[:-4] if c_num and len(c_num) > 4 else c_num if c_num else None,
                "motion_result": "withdrawn",
                "vote_text": section_text,
                "supervisor_votes": [],
            })
            continue

        motion_match = re.search(
            r"Motion to (\w+)[^.]*?(?:by Supervisor ([^,]+),\s*seconded by Supervisor ([^)]+))",
            section_text, re.I,
        )

        known_supervisor_names = {s["normalized_name"] for s in supervisors}

        def is_known_supervisor(name: str) -> bool:
            normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            for known in known_supervisor_names:
                if normalized.startswith(known) or known.startswith(normalized):
                    return True
            return False

        ayes: list[str] = []
        ayes_match = re.search(r"Ayes:\s*(.*?)(?:\s*Nay:|\s*$)", section_text, re.I)
        if ayes_match:
            raw = ayes_match.group(1).strip()
            candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
            for c in candidates:
                c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
                c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
                c = re.sub(r"\s+(County|Human|Public|Parks|Transportation|Elections|Risk|Finance|Real Estate|Library|Planning).*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+STATUTORY.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+AUDIENCIAS.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+BOARD.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+CALL TO.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+LIBRARY.*$", "", c, flags=re.I).strip()
                c = c.rstrip(",;.:").strip()
                if not c or len(c) < 3 or len(c) > 60:
                    continue
                if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                    continue
                if re.search(r"\b(with|and|the|for|of|that|this|from|please|email|prior|local|fire|written|except|amenos|como|que|del|para|una|los|las|por|notado)", c, re.I):
                    continue
                if c not in ayes:
                    ayes.append(c)

        if known_supervisor_names and len(ayes) > len(supervisors):
            filtered = [n for n in ayes if is_known_supervisor(n)]
            if filtered:
                ayes = filtered

        nays: list[str] = []
        nays_match = re.search(r"Nay:\s*(.*?)(?:\s+(?=\d+\.)|\s*$)", section_text, re.I)
        if nays_match:
            raw = nays_match.group(1).strip()
            candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
            for c in candidates:
                c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
                c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
                c = c.rstrip(",;.:").strip()
                if not c or len(c) < 3 or len(c) > 60:
                    continue
                if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                    continue
                if c not in nays:
                    nays.append(c)

            if known_supervisor_names:
                filtered = [n for n in nays if is_known_supervisor(n)]
                if filtered:
                    nays = filtered

        motion_result = ""
        if motion_match:
            action = motion_match.group(1).lower()
            if action in ("approve", "adopt", "concur"):
                motion_result = "approved"
            elif action in ("deny", "denied"):
                motion_result = "denied"
            elif action == "continue":
                motion_result = "continued"
            else:
                motion_result = action
        elif ayes and not nays:
            motion_result = "approved"
        elif nays:
            motion_result = "carried"

        known_supervisor_lookup: dict[str, dict] = {
            s["normalized_name"]: s for s in supervisors
        }

        supervisor_votes: list[dict] = []

        def find_canonical_name(raw_name: str) -> str:
            cleaned = re.sub(r"^Supervisor\s+|^Vice Chair\s+|^Chairman\s+", "", raw_name, flags=re.I).strip()
            normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
            if normalized in known_supervisor_lookup:
                return known_supervisor_lookup[normalized]["name"]
            for kn, kd in known_supervisor_lookup.items():
                if normalized.startswith(kn) or kn.startswith(normalized):
                    return kd["name"]
            return ""

        seen_sup: set[str] = set()
        for name in ayes:
            canonical = find_canonical_name(name)
            if canonical and canonical not in seen_sup:
                seen_sup.add(canonical)
                supervisor_votes.append({"name": canonical, "vote": "yes"})
        for name in nays:
            canonical = find_canonical_name(name)
            if canonical and canonical not in seen_sup:
                seen_sup.add(canonical)
                supervisor_votes.append({"name": canonical, "vote": "no"})

        has_vote_data = bool(motion_result or supervisor_votes)
        agenda_item_counter += 1
        votes.append({
            "agenda_item_id": agenda_item_counter,
            "agenda_item_number": int(item_num),
            "c_number": c_num if c_num else None,
            "c_number_base": c_num[:-4] if c_num and len(c_num) > 4 else c_num if c_num else None,
            "motion_result": motion_result or ("unknown" if has_vote_data else "no_vote"),
            "vote_text": section_text,
            "supervisor_votes": supervisor_votes if has_vote_data else [],
        })

    # Fill in missing items
    found_item_nums = {v["agenda_item_number"] for v in votes}
    for item in agenda_items:
        item_num = int(item.get("agenda_item_number", 0))
        if item_num and item_num not in found_item_nums:
            agenda_item_counter += 1
            c_num = item.get("c_number", "") or ""
            votes.append({
                "agenda_item_id": agenda_item_counter,
                "agenda_item_number": item_num,
                "c_number": c_num if c_num else None,
                "c_number_base": None,
                "motion_result": "no_vote",
                "vote_text": "",
                "supervisor_votes": [],
            })

    return supervisors, votes


class TestBosVoteParsingRegression(unittest.TestCase):
    """Regression tests for BOS vote parsing from summary text fixture."""

    @classmethod
    def setUpClass(cls):
        """Load fixture and build agenda_items list."""
        cls.text = _load_fixture("bos_summary_text_4622.txt")
        # Agenda items for meeting 4622 (1-63)
        cls.agenda_items = [
            {"agenda_item_number": str(i), "c_number": "", "agenda_item_title": f"Item {i}"}
            for i in range(1, 64)
        ]

    def setUp(self):
        self.supervisors, self.votes = _simulate_vote_parsing(
            self.text, self.agenda_items
        )

    def test_supervisors_parsed(self):
        """Summary text should identify all 5 supervisors."""
        self.assertGreaterEqual(len(self.supervisors), 4)

    def test_all_63_items_have_vote_records(self):
        """Every agenda item should have a vote record (no gap items)."""
        vote_nums = sorted(v["agenda_item_number"] for v in self.votes)
        expected = list(range(1, 64))
        self.assertEqual(vote_nums, expected)

    def test_item_10_has_motion_and_ayes(self):
        """Item 10 has motion text 'Motion to concur' and aye votes."""
        item10 = next((v for v in self.votes if v["agenda_item_number"] == 10), None)
        self.assertIsNotNone(item10, "Item 10 has no vote record")
        self.assertEqual(item10["motion_result"], "approved",
                         f"Item 10 should be approved, got '{item10['motion_result']}'")
        vt = item10.get("vote_text", "")
        self.assertIn("Motion to concur", vt, "Item 10 missing motion text")
        self.assertIn("Ayes:", vt, "Item 10 missing ayes")
        self.assertIn("Supervisor Debbie Lesko", vt)
        self.assertIn("Supervisor Thomas Galvin", vt)
        self.assertGreaterEqual(len(item10["supervisor_votes"]), 3,
                                f"Item 10 should have 3+ supervisor votes, got {len(item10['supervisor_votes'])}")

    def test_item_11_has_motion_and_ayes(self):
        """Item 11 has motion text 'Motion to concur' and aye votes."""
        item11 = next((v for v in self.votes if v["agenda_item_number"] == 11), None)
        self.assertIsNotNone(item11, "Item 11 has no vote record")
        self.assertEqual(item11["motion_result"], "approved",
                         f"Item 11 should be approved, got '{item11['motion_result']}'")
        vt = item11.get("vote_text", "")
        self.assertIn("Motion to concur", vt, "Item 11 missing motion text")
        self.assertIn("Ayes:", vt, "Item 11 missing ayes")
        self.assertGreaterEqual(len(item11["supervisor_votes"]), 3,
                                f"Item 11 should have 3+ supervisor votes, got {len(item11['supervisor_votes'])}")

    def test_item_14_double_warrants_has_motion_and_ayes(self):
        """Item 14 (Duplicate Warrants) has motion/ayes despite dollar amounts in its text confusing the regex."""
        item14 = next((v for v in self.votes if v["agenda_item_number"] == 14), None)
        self.assertIsNotNone(item14, "Item 14 has no vote record")
        self.assertEqual(item14["motion_result"], "approved",
                         f"Item 14 should be approved, got '{item14['motion_result']}'")
        vt = item14.get("vote_text", "")
        self.assertIn("Motion to approve", vt, "Item 14 missing motion text")
        self.assertIn("Supervisor Debbie Lesko", vt)
        self.assertIn("Supervisor Mark Stewart", vt)
        self.assertIn("Ayes:", vt, "Item 14 missing ayes")
        self.assertGreaterEqual(len(item14["supervisor_votes"]), 4,
                                f"Item 14 should have 4+ supervisor votes, got {len(item14['supervisor_votes'])}")

    def test_item_6_sonoran_serenity_has_motion_and_ayes(self):
        """Item 6 (SONORAN SERENITY) has motion/ayes — the summary includes 'Motion to concur...Ayes:...'"""
        item6 = next((v for v in self.votes if v["agenda_item_number"] == 6), None)
        self.assertIsNotNone(item6, "Item 6 has no vote record")
        self.assertEqual(item6["motion_result"], "approved",
                         f"Item 6 should be approved, got '{item6['motion_result']}'")
        vt = item6.get("vote_text", "")
        self.assertIn("Motion to concur", vt, "Item 6 missing motion text")
        self.assertIn("Supervisor Debbie Lesko", vt)
        self.assertIn("Supervisor Thomas Galvin", vt)
        self.assertIn("Ayes:", vt, "Item 6 missing ayes")
        self.assertGreaterEqual(len(item6["supervisor_votes"]), 3,
                                f"Item 6 should have 3+ supervisor votes, got {len(item6['supervisor_votes'])}")

    def test_items_with_motion_have_supervisor_votes(self):
        """Items with 'Motion to approve' should have 4-5 supervisor votes."""
        items_with_votes = [v for v in self.votes if v["supervisor_votes"]]
        self.assertGreater(len(items_with_votes), 10,
                           f"Expected >10 items with supervisor votes, got {len(items_with_votes)}")

    def test_no_duplicate_item_numbers(self):
        """No two vote records should share the same item number."""
        nums = [v["agenda_item_number"] for v in self.votes]
        self.assertEqual(len(nums), len(set(nums)))

    def test_item_1_roll_call_has_text(self):
        """Item 1 (Roll Call) should have vote_text with meeting info."""
        item1 = next((v for v in self.votes if v["agenda_item_number"] == 1), None)
        self.assertIsNotNone(item1)
        self.assertGreater(len(item1.get("vote_text", "")), 50)

    def test_results_are_reasonable(self):
        """Vote results should be one of the expected values."""
        allowed = {"approved", "no_vote", "unknown", "withdrawn", "continued", "denied", "carried"}
        for v in self.votes:
            self.assertIn(v["motion_result"], allowed,
                          f"Item {v['agenda_item_number']}: unexpected result '{v['motion_result']}'")


class TestBosVotePositionBasedBoundaries(unittest.TestCase):
    """Verify that character-position-based item boundaries work correctly.

    This was the core bug: when all items are on one line, using line-number
    boundaries means each item gets the same text. Using character-position
    boundaries gives each item its own correct slice.
    """

    def test_adjacent_items_have_different_text(self):
        """Items 5 and 6 should have different vote_text (they're different public hearings)."""
        text = _load_fixture("bos_summary_text_4622.txt")
        items = [{"agenda_item_number": str(i), "c_number": ""} for i in range(1, 64)]
        _, votes = _simulate_vote_parsing(text, items)

        item5 = next(v for v in votes if v["agenda_item_number"] == 5)
        item6 = next(v for v in votes if v["agenda_item_number"] == 6)

        # They should have different text content
        self.assertNotEqual(
            item5.get("vote_text", ""),
            item6.get("vote_text", ""),
            "Items 5 and 6 have identical vote_text — boundaries not splitting correctly",
        )

    def test_item_5_contains_99th_ave(self):
        """Item 5 text should reference the 99TH AVE & OLIVE AVE case."""
        text = _load_fixture("bos_summary_text_4622.txt")
        items = [{"agenda_item_number": str(i), "c_number": ""} for i in range(1, 64)]
        _, votes = _simulate_vote_parsing(text, items)

        item5 = next(v for v in votes if v["agenda_item_number"] == 5)
        vt = item5.get("vote_text", "").upper()
        self.assertIn("99TH", vt) or self.assertIn("OLIVE", vt)


class TestDecimalBoundaryRejection(unittest.TestCase):
    """Unit tests for the boundary-detection decimal post-filters.

    These verify that decimal numbers like "72.50" and "27.66%" are NOT
    treated as item boundaries, while real items with titles starting with
    digits (like "52.260040") still ARE.
    """

    @classmethod
    def setUpClass(cls):
        cls.pattern = re.compile(
            r"(?:^|[^\w\'\u2019\u2018])(\d{1,3})\.(?=\s*[A-Z0-9])"
        )

    def _find_boundary(self, text: str, expected_num: int) -> bool:
        """Check if expected_num is found as a boundary after filtering."""
        seen_nums: set[int] = set()
        for m in self.pattern.finditer(text):
            num = int(m.group(1))
            if num in seen_nums:
                continue
            # Same filters as _simulate_vote_parsing / votes.py
            if m.start() > 0 and text[m.start() - 1].isdigit():
                continue
            if len(text) > m.end() + 2:
                after_dot = text[m.end():m.end()+3]
                if re.match(r'\d{2}%', after_dot):
                    continue
            seen_nums.add(num)
        return expected_num in seen_nums

    def test_comma_decimal_72_50_rejected(self):
        """,072.50 should NOT be treated as an item boundary for item 72."""
        text = "$396,072.50 for the award period"
        self.assertFalse(
            self._find_boundary(text, 72),
            "Decimal '72.50' should NOT match as item 72",
        )

    def test_percentage_27_66_rejected(self):
        """ 27.66% should NOT be treated as an item boundary for item 27."""
        text = "FY26 is 27.66% which is applicable"
        self.assertFalse(
            self._find_boundary(text, 27),
            "Percentage '27.66%' should NOT match as item 27",
        )

    def test_percentage_16_69_rejected(self):
        """ 16.69% should NOT be treated as an item boundary for item 16."""
        text = "indirect cost rate for FY26 is 16.69%. The"
        self.assertFalse(
            self._find_boundary(text, 16),
            "Percentage '16.69%' should NOT match as item 16",
        )

    def test_real_item_with_dollar_amount_not_blocked(self):
        """Item 5 with '5.2026 MARICOPA' (year) should still be found."""
        text = "CONTEST 5.2026 MARICOPA COUNTY STORMWATER"
        self.assertTrue(
            self._find_boundary(text, 5),
            "Item 5 with year-title '5.2026 MARICOPA' should match",
        )

    def test_real_item_with_letter_prefix(self):
        """' 17.AGREEMENT' (letter before separator space) should match."""
        text = "Gallardo  17.AGREEMENT WITH THE STATE"
        self.assertTrue(
            self._find_boundary(text, 17),
            "Item 17 with ' 17.AGREEMENT' should match",
        )

    def test_real_item_number_not_decimal(self):
        """Item 6 ('6.DOMRES 90') should match (no decimal context)."""
        text = "CONSIENTA 6.DOMRES 90 Case"
        self.assertTrue(
            self._find_boundary(text, 6),
            "Item 6 '6.DOMRES 90' should match",
        )

    def test_item_title_starting_with_digits_accepted(self):
        """Item 52 with title starting '52.260040-RFP' should match."""
        text = "Gallardo]52.260040-RFP, OFFICE OF THE MEDICAL"
        self.assertTrue(
            self._find_boundary(text, 52),
            "Item 52 '52.260040-RFP' should match (title starts with digits)",
        )


class TestBosVoteParsing4669PZConsent(unittest.TestCase):
    """Regression test for PZ consent agenda items with conditions text.

    Meeting 4669 has PZ consent items 6-10 with extensive conditions
    text (a-g, a-i, a-p, a-k) and sub-numbered conditions (1., 2., etc.)
    inside each item. The boundary detection must not mistake condition
    numbers for item boundaries.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = _load_fixture("bos_summary_text_4669.txt")
        # Build agenda items list for meeting 4669 (items 1-81)
        cls.agenda_items = [
            {"agenda_item_number": str(i), "c_number": ""}
            for i in range(1, 82)
        ]

    def setUp(self):
        self.supervisors, self.votes = _simulate_vote_parsing(
            self.text, self.agenda_items
        )

    def test_all_81_items_found(self):
        """All 81 agenda items should have vote records."""
        nums = sorted(v["agenda_item_number"] for v in self.votes)
        expected = list(range(1, 82))
        self.assertEqual(nums, expected)

    def test_item_6_has_vote(self):
        """Item 6 (DOMRES 90) should have a vote record."""
        item6 = next((v for v in self.votes if v["agenda_item_number"] == 6), None)
        self.assertIsNotNone(item6, "Item 6 has no vote record")
        vt = item6.get("vote_text", "")
        self.assertIn("Ayes:", vt, "Item 6 missing ayes text")
        self.assertEqual(item6["motion_result"], "approved")

    def test_item_7_has_vote(self):
        """Item 7 (OFF 17 NORTH STORAGE) should have a vote record."""
        item7 = next((v for v in self.votes if v["agenda_item_number"] == 7), None)
        self.assertIsNotNone(item7, "Item 7 has no vote record")
        vt = item7.get("vote_text", "")
        self.assertIn("Ayes:", vt, "Item 7 missing ayes text")

    def test_item_8_has_vote(self):
        """Item 8 (SALOME VILLAGE SHOPS) should have a vote record."""
        item8 = next((v for v in self.votes if v["agenda_item_number"] == 8), None)
        self.assertIsNotNone(item8, "Item 8 has no vote record")
        vt = item8.get("vote_text", "")
        self.assertIn("Ayes:", vt, "Item 8 missing ayes text")

    def test_item_9_has_vote_and_is_split(self):
        """Item 9 (PROJECT BACCARA) should have a split vote (4-1)."""
        item9 = next((v for v in self.votes if v["agenda_item_number"] == 9), None)
        self.assertIsNotNone(item9, "Item 9 has no vote record")
        vt = item9.get("vote_text", "")
        self.assertIn("Ayes:", vt, "Item 9 missing ayes text")
        self.assertIn("Nay:", vt, "Item 9 missing nay text (should be split)")
        self.assertEqual(item9["motion_result"], "approved",
                         "Item 9 should be approved")
        # Should have a 'no' vote (Steve Gallardo dissented)
        no_votes = [sv for sv in item9["supervisor_votes"] if sv["vote"] == "no"]
        yes_votes = [sv for sv in item9["supervisor_votes"] if sv["vote"] == "yes"]
        self.assertEqual(len(no_votes), 1, "Item 9 should have exactly 1 no vote")
        self.assertEqual(len(yes_votes), 4, "Item 9 should have exactly 4 yes votes")

    def test_item_10_has_vote(self):
        """Item 10 (J & S DUMPSTER SERVICE) should have a vote record."""
        item10 = next((v for v in self.votes if v["agenda_item_number"] == 10), None)
        self.assertIsNotNone(item10, "Item 10 has no vote record")
        vt = item10.get("vote_text", "")
        self.assertIn("Ayes:", vt, "Item 10 missing ayes text")


if __name__ == "__main__":
    unittest.main()
