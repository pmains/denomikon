"""Tests for P&Z minutes PDF vote and condition extraction."""

import unittest
import os
from pathlib import Path


class TestPZMinutesParsing(unittest.TestCase):
    """Tests for the pz_minutes module: text extraction, commissioners, votes."""

    def _pdf_path(self, meeting_id: str, date_str: str) -> str:
        """Download a minutes PDF and return the local file path."""
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

    def _extract(self, meeting_id: str, date_str: str) -> tuple:
        """Extract text, commissioners, and votes from a minutes PDF."""
        pdf_path = self._pdf_path(meeting_id, date_str)
        from scraper.common.pz_minutes import (
            extract_minutes_text, parse_commissioners, parse_minutes_votes,
        )
        text = extract_minutes_text(pdf_path)
        self.assertIsNotNone(text, "Failed to extract text from minutes PDF")
        members = parse_commissioners(text)
        votes = parse_minutes_votes(text)
        return members, votes

    def test_votes_extracted_from_april_9_2026(self):
        """Meeting 3711 — votes for consent + regular items."""
        members, votes = self._extract("3711", "04092026")

        present = [m["name"] for m in members.get("present", [])]
        self.assertGreaterEqual(len(present), 6)
        self.assertIn("Linda Milhaven", present)

        self.assertGreaterEqual(len(votes), 3,
            f"Should have at least 3 votes, got {len(votes)}")

        # Consent agenda vote
        consent = next((v for v in votes if "MCP250001" in (v.get("case_number", "") or "")), None)
        self.assertIsNotNone(consent, "Consent agenda vote not found")
        self.assertEqual(consent["motion_result"], "approved")
        self.assertGreater(len(consent.get("commissioner_second", "")), 20)

        # SU250007 vote
        su = next((v for v in votes if "SU250007" in (v.get("case_number", "") or "")), None)
        self.assertIsNotNone(su, "SU250007 vote not found")
        self.assertEqual(su["motion_result"], "approved")

    def test_members_extracted(self):
        """Meeting 3711 — all commissioners present."""
        members, _ = self._extract("3711", "04092026")

        present = [m["name"] for m in members.get("present", [])]
        expected = ["Linda Milhaven", "Jan Leighton", "Derrik Rochwalik",
                     "Mihai Toma", "Warren Whitney", "Erik Hernandez",
                     "Alex Finter", "Jimmy Lindblom"]
        for name in expected:
            self.assertIn(name, present, f"Expected {name} in present list")

    def test_minutes_not_yet_available_handles_gracefully(self):
        """Non-existent file returns empty text."""
        from scraper.common.pz_minutes import extract_minutes_text
        text = extract_minutes_text("/tmp/nonexistent_file.pdf")
        self.assertIsNone(text, "Extracting non-existent file should return None")
