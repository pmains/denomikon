"""Test tier helpers for fast offline test runs.

Tier system:
  unit        — pure functions, no network, no DB, no fixtures → always runs
  integration — temp SQLite or local fixture files → runs by default
  live        — Playwright or Agenda Online network calls → skipped by default

Usage:
  @integration_test
  class TestSomething(unittest.TestCase): ...

  @live_test
  class TestLiveAgenda(unittest.TestCase): ...

Environment variables:
  RUN_INTEGRATION_TESTS — set to "0" to skip integration tests (default "1")
  RUN_LIVE_TESTS        — set to "1" to enable live tests (default "0")
"""

import os
import unittest

_RUN_INTEGRATION = os.getenv("RUN_INTEGRATION_TESTS", "1") == "1"
_RUN_LIVE = os.getenv("RUN_LIVE_TESTS") == "1"

integration_test = unittest.skipUnless(
    _RUN_INTEGRATION,
    "set RUN_INTEGRATION_TESTS=0 to disable integration tests",
)

live_test = unittest.skipUnless(
    _RUN_LIVE,
    "set RUN_LIVE_TESTS=1 to run live (Playwright/network) tests",
)
