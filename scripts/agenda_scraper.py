#!/usr/bin/env python3
"""
Backward-compatible shim — kept so daily_sync.py (which calls this via
subprocess) and any scripts referencing it continue to work.

Delegates to scripts/scraper/main.py's main() after enforcing the
database tier selection.  The ./scrape wrapper now calls main.py
directly and bypasses this file.
"""
from __future__ import annotations

import os
import sys

# ── Database tier enforcement ────────────────────────────────────────────
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
from scraper.main import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
