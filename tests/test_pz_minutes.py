"""Tests for P&Z minutes PDF vote and condition extraction."""

import unittest
from pathlib import Path


class TestPZMinutesParsing(unittest.TestCase):
    """Tests for parse_pz_minutes_pdf — vote extraction from minutes PDFs."""

    def _download_minutes(self, meeting_id: str, date_str: str) -> str:
        """Download a minutes PDF for a given meeting. Returns the file path."""
        import urllib.request
        url = f"https://www.maricopa.gov/AgendaCenter/ViewFile/Minutes/_{date_str}-{meeting_id}"
        pdf_path = Path(f"/tmp/pz_minutes_{meeting_id}_test.pdf")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                pdf_path.write_bytes(resp.read())
            return str(pdf_path)
        except Exception:
            self.skipTest(f"Could not download minutes PDF for meeting {meeting_id}")

    def test_votes_extracted_from_april_9_2026(self):
        """Meeting 3711 (April 9, 2026) should have votes for consent + regular items."""
        from scraper.pz_minutes import parse_pz_minutes_pdf

        pdf_path = self._download_minutes("3711", "04092026")
        result = parse_pz_minutes_pdf(pdf_path)

        # Member roster
        self.assertGreaterEqual(len(result["members_present"]), 6,
            "Should have at least 6 commissioners present")
        self.assertIn("Linda Milhaven", result["members_present"])

        # Should have 4 votes (not including minutes approval which is filtered)
        self.assertGreaterEqual(len(result["votes"]), 3,
            f"Should have at least 3 votes, got {len(result['votes'])}")

        # Find the consent agenda vote
        consent_vote = None
        for v in result["votes"]:
            if "MCP250001" in v["c_numbers"] and "Z250044" in v["c_numbers"]:
                consent_vote = v
                break
        self.assertIsNotNone(consent_vote, "Consent agenda vote not found")
        self.assertEqual(consent_vote["motion_result"], "approved")
        self.assertIsNotNone(consent_vote["conditions"],
            "Consent vote should have conditions")
        self.assertGreater(len(consent_vote["conditions"]), 100,
            "Conditions should be substantial text")

        # Find SU250007 vote
        su250007_vote = None
        for v in result["votes"]:
            if "SU250007" in v["c_numbers"]:
                su250007_vote = v
                break
        self.assertIsNotNone(su250007_vote, "SU250007 vote not found")
        self.assertEqual(su250007_vote["motion_result"], "approved")
        self.assertEqual(su250007_vote["mover"], "Commissioner Toma")
        self.assertEqual(su250007_vote["seconder"], "Commissioner Leighton")

    def test_members_extracted(self):
        """Member roster should be extracted from the header section."""
        from scraper.pz_minutes import parse_pz_minutes_pdf

        pdf_path = self._download_minutes("3711", "04092026")
        result = parse_pz_minutes_pdf(pdf_path)

        present = result["members_present"]
        # All 8 commissioners should be present
        expected = ["Linda Milhaven", "Jan Leighton", "Derrik Rochwalik",
                     "Mihai Toma", "Warren Whitney", "Erik Hernandez",
                     "Alex Finter", "Jimmy Lindblom"]
        for name in expected:
            self.assertIn(name, present, f"{name} should be in members_present")

        absent = result["members_absent"]
        self.assertIn("Spike Lawrence", absent,
                       "Spike Lawrence should be in members_absent")
        self.assertIn("Kevin Danzeisen", absent,
                       "Kevin Danzeisen should be in members_absent")

    def test_minutes_not_yet_available_handles_gracefully(self):
        """When the minutes PDF returns 404, the parser should not crash."""
        from pathlib import Path

        # Non-existent file
        from scraper.pz_minutes import parse_pz_minutes_pdf
        result = parse_pz_minutes_pdf("/tmp/nonexistent_file.pdf")

        self.assertEqual(result["members_present"], [])
        self.assertEqual(result["members_absent"], [])
        self.assertEqual(result["votes"], [])
