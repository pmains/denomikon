#!/usr/bin/env python3
"""Backward-compatible shim. Import from scraper.* for modular code."""
from __future__ import annotations

import asyncio

# Re-export everything from the scraper package
from scraper import *

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
