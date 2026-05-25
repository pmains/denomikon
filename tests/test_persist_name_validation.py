"""Test the _name_looks_like_a_person validation function in persist.py."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from db.persist import _name_looks_like_a_person

def test_real_person_names():
    """All of these should be accepted as real person names."""
    assert _name_looks_like_a_person("Mark Freeman") is True
    assert _name_looks_like_a_person("Scott Somers") is True
    assert _name_looks_like_a_person("Rich Adams") is True
    assert _name_looks_like_a_person("Jennifer Duff") is True
    assert _name_looks_like_a_person("Alicia Goforth") is True
    assert _name_looks_like_a_person("Francisco Heredia") is True
    assert _name_looks_like_a_person("Dorean Taylor") is True
    assert _name_looks_like_a_person("John Giles") is True
    assert _name_looks_like_a_person("Julie Spilsbury") is True
    assert _name_looks_like_a_person("Kevin Hartke") is True
    assert _name_looks_like_a_person("Angel Encinas") is True
    assert _name_looks_like_a_person("OD Harris") is True
    assert _name_looks_like_a_person("Kate Brophy McGee") is True  # 3 words OK
    assert _name_looks_like_a_person("Bill Gates") is True
    assert _name_looks_like_a_person("Steve Gallardo") is True
    print("PASS: all real person names accepted")

def test_garbage_names():
    """All of these should be rejected as non-person names."""
    # Section headers / presentation topics that were leaked into supervisors list
    assert _name_looks_like_a_person("Study Session") is False
    assert _name_looks_like_a_person("Previous Studies") is False
    assert _name_looks_like_a_person("Project Background") is False
    assert _name_looks_like_a_person("Recommended HCT Corridor") is False
    assert _name_looks_like_a_person("Art Selection Consideration") is False
    assert _name_looks_like_a_person("Seeking Council Direction") is False

    # Garbage from live DB that was leaked
    assert _name_looks_like_a_person("Admin Spaces") is False
    assert _name_looks_like_a_person("Adult Upcharg") is False
    assert _name_looks_like_a_person("Advanced Metering") is False
    assert _name_looks_like_a_person("Affordable Services") is False
    assert _name_looks_like_a_person("Apache Junction") is False
    assert _name_looks_like_a_person("Arizona Science Center") is False
    assert _name_looks_like_a_person("Audience Types") is False
    assert _name_looks_like_a_person("Alternative Construction Permitted") is False  # 3 words, stop word
    assert _name_looks_like_a_person("Medical Plan Premiums") is False
    assert _name_looks_like_a_person("Financial Considerations") is False
    assert _name_looks_like_a_person("Utility Fund Forecast") is False
    assert _name_looks_like_a_person("Beginning Reserve Balance") is False
    assert _name_looks_like_a_person("Ending Reserve Balance") is False
    assert _name_looks_like_a_person("Natural History Museum") is False

    # ALL-CAPS names (from agenda headers)
    assert _name_looks_like_a_person("BUDGET OVERVIEW") is False
    assert _name_looks_like_a_person("EXECUTIVE SESSION") is False

    # Lowercase artifacts
    assert _name_looks_like_a_person("mayor giles conducted") is False  # all words start lowercase

    # Names starting with non-identifiable words
    assert _name_looks_like_a_person("Previous Current") is False
    assert _name_looks_like_a_person("Our Services") is False

    # Too many words
    assert _name_looks_like_a_person("John Michael Jacob Robert Smith") is False

    print("PASS: all garbage names rejected")

def test_edge_cases():
    assert _name_looks_like_a_person("") is False
    assert _name_looks_like_a_person(None) is False
    assert _name_looks_like_a_person("   ") is False
    assert _name_looks_like_a_person("A") is False  # too short
    print("PASS: edge cases handled")

if __name__ == "__main__":
    test_real_person_names()
    test_garbage_names()
    test_edge_cases()
    print("\nAll tests passed!")
