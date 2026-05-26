#!/usr/bin/env python3
"""Backward-compatible shim. Import from scraper.* for modular code."""
from __future__ import annotations

import os
import sys

# ── Database tier enforcement ────────────────────────────────────────────
# Require an explicit --dev or --test flag before importing any db modules.
# This prevents accidentally writing to the wrong database.
for flag, tier in [("--dev", "development"), ("-d", "development"),
                   ("--test", "test"), ("-t", "test"),
                   ("--prod", "production"), ("-p", "production")]:
    if flag in sys.argv:
        os.environ["POLISCOPIC_DB_TIER"] = tier
        sys.argv.remove(flag)
        break
else:
    if "POLISCOPIC_DB_TIER" not in os.environ and "DATABASE_URL" not in os.environ:
        print(
            "\n  ⚠  No database tier selected.\n"
            "\n"
            "     Add one of these flags:\n"
            "       --dev     Development database (data/maricopa.sqlite)\n"
            "       --test    Test database (temporary, destroyed after)\n"
            "\n"
            "     Or set POLISCOPIC_DB_TIER or DATABASE_URL in your environment.\n",
            file=sys.stderr,
        )
        sys.exit(1)

import asyncio

# Re-export everything from the scraper package
from scraper import *

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
