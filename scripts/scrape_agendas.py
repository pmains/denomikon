#!/usr/bin/env python3
"""
Single-jurisdiction agenda scraper.

Usage:
  python scripts/scrape_agendas.py <jurisdiction> [--sync] [--start-date=...]

Delegates to scripts/scraper/main.py after enforcing database tier selection.
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
        # db/config.py will load .env and default to PostgreSQL; proceed
        pass

import asyncio
from scraper.main import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
