"""Regression test for the BOS sync code path.

Verifies that module-level ``import time`` is not shadowed by local imports
inside function bodies — the UnboundLocalError that occurred when a local
``import time`` inside the tempe ``if`` block prevented the BOS path from
accessing ``time.monotonic()``.
"""

import ast
import os
import sys
import unittest
from pathlib import Path

_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


class TestBosSyncNoShadowedTime(unittest.TestCase):
    """The BOS sync path uses ``time.monotonic()`` for elapsed-time tracking.
    A local ``import time`` inside the tempe if-block was shadowing the
    module-level ``import time``, causing an UnboundLocalError when the BOS
    path tried to call ``time.monotonic()``.

    This test parses the AST of ``main.py`` to ensure no local ``import time``
    statements exist inside any function body.
    """

    @classmethod
    def setUpClass(cls):
        main_py = Path(__file__).resolve().parent.parent / "scripts" / "scraper" / "main.py"
        with open(main_py) as f:
            cls.tree = ast.parse(f.read())
        cls._find_imports(cls.tree)

    @classmethod
    def _find_imports(cls, node, depth=0):
        """Recursively find all import statements and flag shadowed names."""
        cls.imports = cls.imports if hasattr(cls, 'imports') else {}
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    name = alias.name.split(".")[0]
                    cls.imports.setdefault(name, []).append(
                        (child.lineno, depth, isinstance(child, ast.ImportFrom))
                    )
            cls._find_imports(child, depth + 1)

    def test_time_imported_at_module_level(self):
        """``import time`` must exist at module depth (depth=0)."""
        time_imports = [loc for loc in self.imports.get("time", [])
                       if loc[1] == 0]
        self.assertTrue(time_imports,
                        "No module-level 'import time' found. "
                        "The BOS path depends on it.")

    def test_no_local_time_import_in_functions(self):
        """No ``import time`` or ``from time import`` inside any function."""
        time_imports = [loc for loc in self.imports.get("time", [])
                       if loc[1] > 0]
        self.assertFalse(time_imports,
                         f"Local 'import time' found inside a function at "
                         f"line(s): {[loc[0] for loc in time_imports]}. "
                         f"This shadows the module-level import and breaks "
                         f"the BOS code path.")

    def test_no_shadowed_time_import_in_function(self):
        """No bare ``import time`` inside any function body.
        The regression: a local ``import time`` inside the tempe if-block
        shadowed the module-level ``import time``, causing an
        UnboundLocalError when the BOS path called ``time.monotonic()``."""
        for name, locs in self.imports.items():
            if name == "time":
                for lno, depth, is_from in locs:
                    if depth > 0 and not is_from:
                        self.fail(
                            f"Line {lno}: bare 'import time' inside a function "
                            f"body shadows the module-level 'import time'."
                        )


class TestBosSyncPathModuleImport(unittest.TestCase):
    """Verify the module-level time is accessible for the BOS code path."""

    def test_module_level_time_available(self):
        import scripts.scraper.main as m
        self.assertTrue(hasattr(m, "time"), "main.py has no module-level 'time'")
        self.assertTrue(callable(m.time.monotonic),
                        "time.monotonic() is not callable")

    def test_time_not_replaced_in_function(self):
        """The 'time' name at module scope should be the stdlib time module."""
        import scripts.scraper.main as m
        import time as stdlib_time
        self.assertIs(m.time, stdlib_time,
                      "Module-level 'time' in main.py is not the stdlib module")


class TestTempeSyncNoHardcodedJurisdiction(unittest.TestCase):
    """Regression: the Tempe sync code path must NOT hardcode jurisdiction_id.

    Previously the Tempe sync was setting jurisdiction_id=jur_id (always
    Tempe's ID=2) on every meeting it created.  BOS bilingual meetings that
    got picked up by the search inherited the wrong jurisdiction.

    The fix: use pb.jurisdiction_id (resolved from the meeting's public
    body record) instead of a fixed Tempe jur_id.
    """

    @classmethod
    def setUpClass(cls):
        main_py = Path(__file__).resolve().parent.parent / "scripts" / "scraper" / "main.py"
        with open(main_py) as f:
            source = f.read()
        cls.tree = ast.parse(source)
        cls.source_lines = source.split("\n")

    def test_no_hardcoded_jurisdiction_id_in_meeting_creation(self):
        """Meeting creation in main.py must not use ``jurisdiction_id=jur_id``.
        
        Look for all ``MeethingModel(`` or ``create_or_get_meeting(`` calls
        and ensure they don't set jurisdiction_id to a hardcoded variable.
        """
        source = "\n".join(self.source_lines)
        # Check the current Tempe sync code for old pattern
        # Old pattern: jurisdiction_id=jur_id
        # New pattern: jurisdiction_id=pb.jurisdiction_id
        matches = []
        for i, line in enumerate(self.source_lines, 1):
            stripped = line.strip()
            if "jurisdiction_id=jur_id" in stripped:
                matches.append(i)
        self.assertFalse(
            matches,
            f"Line(s) {matches} still have hardcoded 'jurisdiction_id=jur_id'. "
            f"Should use pb.jurisdiction_id instead."
        )

    def test_jurisdiction_resolved_from_public_body(self):
        """The Tempe sync MeetingModel creation uses pb.jurisdiction_id."""
        source = "\n".join(self.source_lines)
        self.assertIn(
            "jurisdiction_id=pb.jurisdiction_id if pb else None",
            source,
            "Tempe sync MeetingModel must resolve jurisdiction_id from pb, "
            "not hardcode it."
        )


if __name__ == "__main__":
    unittest.main()
