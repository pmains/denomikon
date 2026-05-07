"""Tests for meeting title normalization helpers in scripts/db.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from db import (
    normalize_meeting_type,
    extract_meeting_context,
    extract_meeting_body,
    build_meeting_display_name,
)


class TestNormalizeMeetingType(unittest.TestCase):
    def test_formal_meeting(self):
        self.assertEqual(normalize_meeting_type("Formal Meeting"), "Formal")

    def test_just_formal(self):
        self.assertEqual(normalize_meeting_type("Formal"), "Formal")

    def test_just_informal(self):
        self.assertEqual(normalize_meeting_type("Informal"), "Informal")

    def test_just_special(self):
        self.assertEqual(normalize_meeting_type("Special"), "Special")

    def test_special_with_extra(self):
        self.assertEqual(normalize_meeting_type("Special", "Election of Chairman"), "Special")

    def test_special_executive(self):
        self.assertEqual(normalize_meeting_type("Special Executive"), "Executive")

    def test_executive_continued(self):
        self.assertEqual(normalize_meeting_type("Executive (CONTINUED)"), "Executive")

    def test_empty_fallback(self):
        self.assertEqual(normalize_meeting_type(""), "Unknown")

    def test_unknown_fallback(self):
        self.assertEqual(normalize_meeting_type("Board of Supervisors"), "Board of Supervisors")

    def test_normalize_via_title(self):
        self.assertEqual(normalize_meeting_type("", "Executive Meeting"), "Executive")
        self.assertEqual(normalize_meeting_type("", "Formal Meeting"), "Formal")


class TestExtractMeetingBody(unittest.TestCase):
    def test_board_of_supervisors_bilingual(self):
        title = "BOARD OF SUPERVISORS - JUNTA DE SUPERVISORES"
        self.assertEqual(extract_meeting_body(title), "Board of Supervisors")
        # Bilingual header should not become display context
        self.assertIsNone(extract_meeting_context(title, "Formal"))

    def test_returns_none_for_other(self):
        self.assertIsNone(extract_meeting_body("Formal Meeting"))
        self.assertIsNone(extract_meeting_body(""))


class TestExtractMeetingContext(unittest.TestCase):
    def test_election_of_chairman(self):
        self.assertEqual(
            extract_meeting_context("Special/Election of Chairman", "Special"),
            "Election of Chairman",
        )

    def test_emergency(self):
        self.assertEqual(
            extract_meeting_context("Emergency Meeting", "Special"),
            "Emergency",
        )

    def test_numeric_title_ignored(self):
        self.assertIsNone(extract_meeting_context("4467", "Formal"))

    def test_empty_title(self):
        self.assertIsNone(extract_meeting_context("", "Formal"))

    def test_known_type_words_return_none(self):
        for title in ("formal", "informal", "special", "executive",
                      "Formal Meeting", "Informal Meeting",
                      "Special Meeting", "Executive Meeting"):
            with self.subTest(title=title):
                self.assertIsNone(
                    extract_meeting_context(title, title.capitalize()))

    def test_special_slash_call(self):
        self.assertEqual(
            extract_meeting_context("Special/Call", "Special"),
            "Call",
        )


class TestBuildDisplayName(unittest.TestCase):
    def test_with_context(self):
        result = build_meeting_display_name("Special", "2026-01-05", "Election of Chairman")
        self.assertEqual(result, "Special Meeting — Election of Chairman — Jan 5, 2026")

    def test_without_context(self):
        result = build_meeting_display_name("Formal", "2026-03-20")
        self.assertEqual(result, "Formal Meeting — Mar 20, 2026")

    def test_executive(self):
        result = build_meeting_display_name("Executive", "2026-04-20")
        self.assertEqual(result, "Executive Meeting — Apr 20, 2026")

    def test_informal(self):
        result = build_meeting_display_name("Informal", "2026-05-04")
        self.assertEqual(result, "Informal Meeting — May 4, 2026")

    def test_fallback_date(self):
        result = build_meeting_display_name("Formal", "bad-date")
        self.assertIn("bad-date", result)

    def test_type_already_ends_with_meeting(self):
        result = build_meeting_display_name("Formal Meeting", "2026-01-05")
        self.assertEqual(result, "Formal Meeting — Jan 5, 2026")

    def test_unknown_fallback(self):
        result = build_meeting_display_name("Unknown", "2026-06-01")
        self.assertEqual(result, "Unknown Meeting — Jun 1, 2026")


if __name__ == "__main__":
    unittest.main()
