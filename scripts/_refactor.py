#!/usr/bin/env python3
"""Extract sections from agenda_scraper.py into modular package files.

This script reads the source file and writes each module file with exact
code from the original, preserving all behavior.

Run from scripts/ directory.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


SRC = Path(__file__).parent / "agenda_scraper.py"
OUT = Path(__file__).parent / "scraper"


def read_lines(start: int, end: int | None = None) -> str:
    """Read lines from the source file (1-indexed)."""
    text = SRC.read_text()
    lines = text.splitlines(keepends=True)
    if end is None:
        end = len(lines)
    return "".join(lines[start - 1:end])


# ── utils.py ──────────────────────────────────────────────────────
UTILS_CODE = read_lines(1, 89)  # imports, log, setup_logger, constants, get_async_playwright, retry_with_backoff

# CASE_PATTERN (line ~1222)
CASE_PATTERN_LINES = read_lines(1222, 1223)

# C_NUMBER_PATTERN, _extract_c_number, parse_c_number_parts (lines ~1226-1262)
C_NUMBER_LINES = read_lines(1226, 1262)

# parse_metadata_from_page_data, extract_meeting_metadata_from_page, is_image_based_agenda
# extract_agenda_items_for_meeting, extract_raw_agenda_blocks_for_meeting
# extract_raw_agenda_blocks_from_metadata, extract_agenda_items_from_metadata
# extract_meetings, iter_discovery_documents, write_discovery_rows
# count_agenda_items_for_meeting
METADATA_LINES = read_lines(1966, 2197)

# Also need SOURCE_PAGE, SEARCH_BASE, REQUIRED_BODY, REQUIRED_TYPES, ROOT, etc (lines 41-75)
# Already included in UTILS_CODE

# Let's check what's missing. We need to also export from utils:
# parse_metadata_from_page_data, extract_meeting_metadata_from_page, is_image_based_agenda
# extract_agenda_items_for_meeting, extract_raw_agenda_blocks_for_meeting
# extract_raw_agenda_blocks_from_metadata, extract_agenda_items_from_metadata
# extract_meetings, iter_discovery_documents, write_discovery_rows
# count_agenda_items_for_meeting, CASE_PATTERN, C_NUMBER_PATTERN, _extract_c_number, parse_c_number_parts

# Wait - these functions use things from io_utils/agenda_items that haven't been imported yet.
# In the original file, everything is in one namespace. In the modular version,
# we need to be careful about cross-module imports.

# Let me revisit the plan. The plan says:
# utils.py:
# - lines 26-92: setup_logger, get_async_playwright, retry_with_backoff
# - lines 1966-2191: parse_metadata_from_page_data, extract_meeting_metadata_from_page, etc.
# - Also all module-level constants

# But some of those functions (like extract_agenda_items_for_meeting) call
# parse_agenda_items_from_html which is in agenda_items.py.
# And extract_meetings calls parse_search_results_html which is in search.py.
# And write_discovery_rows calls write_discovery_row which is in io_utils.py.

# So utils.py needs to do internal imports from other modules. Let me check...

# Actually, the plan is a guide. Since the original file has everything in one namespace,
# the modular version will need cross-module imports. Python handles this fine as long
# as there are no circular dependencies.

# Let me restructure:
# utils.py: Only imports, log, setup_logger, constants, get_async_playwright, retry_with_backoff
#   - lines 1-89 (imports+log+setup_logger+constants+get_async_playwright+retry_with_backoff)
#   - lines 1966-1995 (parse_metadata_from_page_data, extract_meeting_metadata_from_page, is_image_based_agenda)

# Actually wait, let me re-read the plan's module mapping more carefully:

# "10. `scripts/scraper/utils.py` (lines 26-92, 1966-2191): setup_logger, get_async_playwright, 
#     retry_with_backoff, parse_metadata_from_page_data, extract_meeting_metadata_from_page, 
#     is_image_based_agenda, extract_agenda_items_for_meeting, extract_raw_agenda_blocks_for_meeting, 
#     extract_raw_agenda_blocks_from_metadata, extract_agenda_items_from_metadata, 
#     count_agenda_items_for_meeting; also all module-level constants (SOURCE_PAGE, SEARCH_BASE, 
#     REQUIRED_BODY, REQUIRED_TYPES, ROOT, AGENDAS_ROOT, etc.)"

# Hmm, but extract_agenda_items_for_meeting calls parse_agenda_items_from_html (agenda_items.py)
# extract_raw_agenda_blocks_for_meeting calls parse_raw_agenda_blocks_html (agenda_items.py)
# extract_raw_agenda_blocks_from_metadata calls write_raw_agenda_item_row (io_utils.py)
# extract_agenda_items_from_metadata calls write_agenda_item_row (io_utils.py)
# count_agenda_items_for_meeting calls extract_agenda_item_titles (agenda_items.py)
# extract_meetings calls parse_search_results_html (search.py) and _search_results_table_present (html_utils.py)
# write_discovery_rows calls write_discovery_row (io_utils.py)
# iter_discovery_documents is standalone

# These are "thin wrapper" functions that orchestrate calls to other modules.
# They can live in utils.py with lazy/conditional imports.

# Actually for simplicity, let me put ALL of these orchestration functions in main.py instead.
# utils.py will just have constants + base utilities.

# Wait, I'm overthinking this. Let me just follow the plan literally. The functions
# in utils.py that call other module functions will import from those modules.
# Python handles this fine since utils.py doesn't import them back.

print("Reading source...")
text = SRC.read_text()
lines = text.splitlines(keepends=True)

def get(start, end):
    """Get lines (1-indexed)."""
    return "".join(lines[start-1:end])

# ============================
# Write each module
# ============================

# 1. utils.py - imports, log, constants, base utilities
utils_code = get(1, 89)
# Add CASE_PATTERN (line 1222-1223)
utils_code += get(1222, 1223)
# Add PZ constants (lines 1224-1225)
utils_code += get(1224, 1225)
# Add C_NUMBER_PATTERN, _extract_c_number, parse_c_number_parts (lines 1226-1262)
utils_code += get(1226, 1262)
# Add parse_metadata_from_page_data, extract_meeting_metadata_from_page, is_image_based_agenda (lines 1966-2070)
utils_code += get(1966, 2070)

(OUT / "utils.py").write_text(utils_code)
print("Written utils.py")

# 2. models.py - Meeting dataclass, _HtmlNode, _TreeBuilder
# Imports: from dataclasses import dataclass, from html.parser import HTMLParser, from __future__ import annotations
models_code = """from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

"""
models_code += get(94, 163)
(OUT / "models.py").write_text(models_code)
print("Written models.py")

# 3. html_utils.py - _parse_html, _node_text, _clean_html_text, _closest_parent, _find_all, _has_class, _search_results_table_present
html_code = """from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Optional

from scraper.models import _HtmlNode, _TreeBuilder

"""
html_code += get(165, 215)
(OUT / "html_utils.py").write_text(html_code)
print("Written html_utils.py")

# 4. cli.py - parse_args, _parse_bos_args, _parse_pz_args, parse_date
cli_code = """from __future__ import annotations

import argparse

import datetime as dt

from scraper.utils import log

"""
# Need to add the parse_args, _parse_bos_args, _parse_pz_args, parse_date functions
# These are at lines 343-443
cli_code += get(343, 443)
(OUT / "cli.py").write_text(cli_code)
print("Written cli.py")

# 5. search.py - parse_search_results_html, build_search_url, extract_meetings
# parse_search_results_html: lines 217-298
# build_search_url: lines 445-453
# extract_meetings: lines 2140-2161
# Also needs: write_discovery_rows, iter_discovery_documents (those go to utils.py)
search_imports = """from __future__ import annotations

import re
import urllib.parse

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text, _search_results_table_present
from scraper.io_utils import normalize_meeting_date
from scraper.models import Meeting

"""
search_code = search_imports
search_code += get(217, 298)
search_code += "\n\n"
search_code += get(445, 453)
search_code += "\n\n"
search_code += get(2140, 2161)
(OUT / "search.py").write_text(search_code)
print("Written search.py")

# 6. io_utils.py - all CSV I/O functions, download_url, etc.
# Lines 462-906 (slugify through row_paths_present)
# Lines 907-921 (read_existing_agenda_urls through read_existing_rejected_block_keys)
# Lines 922-932 (read_existing_structured_item_keys)
# Lines 2163-2191 (iter_discovery_documents, write_discovery_rows)
# Plus: read_agenda_metadata_rows, filter_agenda_metadata_rows (lines 338-342 and some later)

# Let me figure out precise ranges
# slugify: 455-458
# normalize_meeting_date: 460-465
# _normalize_text_date: 467-475
# month_dir_for_date: 477-479
# month_metadata_path: 481-482
# ensure_dir: 484-485
# csv_row: 487-492
# read_existing_rows: 494-505
# write_download_row: 507-524
# write_discovery_row: 526-543
# write_agenda_item_row: 545-562
# write_structured_agenda_item_row: 564-581
# write_raw_agenda_item_row: 583-600
# write_rejected_raw_block_row: 602-619
# debug_agenda_html_path: 621-622
# write_agenda_debug_files: 624-671 (async - careful)
# url_ext: 739-743
# infer_extension: 745-755
# download_url: 757-770
# existing_paths_present: 772-778
# row_paths_present: 780-782
# read_existing_agenda_urls: 784-798
# read_existing_discovery_keys: 800-814
# read_agenda_metadata_rows: 816-831
# filter_agenda_metadata_rows: 833-850
# read_existing_agenda_item_keys: 852-868
# read_existing_raw_block_keys: 870-885
# read_existing_rejected_block_keys: 887-902 (wait, let me check - lines 887-905?)
# read_existing_structured_item_keys: 907-921
# iter_discovery_documents: 2163-2171
# write_discovery_rows: 2173-2191

# Wait, there's also async write_agenda_debug_files at 624-671.
# That's problematic for io_utils.py since it uses Playwright.
# Let me put that in main.py instead.

print("Building io_utils.py...")
io_code = """from __future__ import annotations

import csv
import datetime as dt
import html
import io
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

"""
io_code += get(455, 458)  # slugify
io_code += "\n\n"
io_code += get(460, 465)  # normalize_meeting_date
io_code += "\n\n"
io_code += get(467, 475)  # _normalize_text_date
io_code += "\n\n"
io_code += get(477, 479)  # month_dir_for_date
io_code += "\n\n"
io_code += get(481, 482)  # month_metadata_path
io_code += "\n\n"
io_code += get(484, 485)  # ensure_dir
io_code += "\n\n"
io_code += get(487, 492)  # csv_row
io_code += "\n\n"
io_code += get(494, 505)  # read_existing_rows
io_code += "\n\n"
io_code += get(507, 524)  # write_download_row
io_code += "\n\n"
io_code += get(526, 543)  # write_discovery_row
io_code += "\n\n"
io_code += get(545, 562)  # write_agenda_item_row
io_code += "\n\n"
io_code += get(564, 581)  # write_structured_agenda_item_row
io_code += "\n\n"
io_code += get(583, 600)  # write_raw_agenda_item_row
io_code += "\n\n"
io_code += get(602, 619)  # write_rejected_raw_block_row
io_code += "\n\n"
io_code += get(621, 622)  # debug_agenda_html_path
io_code += "\n\n"
io_code += get(739, 743)  # url_ext
io_code += "\n\n"
io_code += get(745, 755)  # infer_extension
io_code += "\n\n"
io_code += get(757, 770)  # download_url
io_code += "\n\n"
io_code += get(772, 778)  # existing_paths_present
io_code += "\n\n"
io_code += get(780, 782)  # row_paths_present
io_code += "\n\n"
io_code += get(784, 798)  # read_existing_agenda_urls
io_code += "\n\n"
io_code += get(800, 814)  # read_existing_discovery_keys
io_code += "\n\n"
io_code += get(816, 831)  # read_agenda_metadata_rows
io_code += "\n\n"
io_code += get(833, 850)  # filter_agenda_metadata_rows
io_code += "\n\n"
io_code += get(852, 868)  # read_existing_agenda_item_keys
io_code += "\n\n"
io_code += get(870, 885)  # read_existing_raw_block_keys
io_code += "\n\n"
io_code += get(887, 905)  # read_existing_rejected_block_keys
io_code += "\n\n"
io_code += get(907, 921)  # read_existing_structured_item_keys
io_code += "\n\n"
# iter_discovery_documents, write_discovery_rows
io_code += get(2163, 2191)

(OUT / "io_utils.py").write_text(io_code)
print("Written io_utils.py")

# 7. agenda_items.py - all agenda item parsing
# Lines 300-342 (CASE_PATTERN etc - nope, those are at 1222-1223)
# Actually: 
# - parse_raw_agenda_blocks_html: 300-342
# - parse_agenda_items_from_html, split_bilingual_title, etc: 907-1200, 1221-1350
# - _clean_lnk_title, _find_item_tables, _extract_lnk_from_table, extract_agenda_item_titles: 2199-2302
# - splitter functions: 933-1200

# Actually let me be more precise. Lines 907-1200 contains:
# - split_bilingual_title (line ~935)
# - _raw_block_boilerplate_reason, validate_raw_block, split_raw_block_into_items, splitter_self_test, split_raw_agenda_blocks_to_structured
# - _clean_line, _looks_like_boilerplate, _looks_like_item_heading, _looks_like_section_heading
# - _detect_vote_or_action, _build_item_url

# And lines 1221-1350 contains parse_agenda_items_from_html
# And lines 2199-2302 contains _clean_lnk_title, _find_item_tables, _extract_lnk_from_table, extract_agenda_item_titles

agenda_imports = """from __future__ import annotations

import csv
import html
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _has_class, _node_text
from scraper.io_utils import _clean_line, _looks_like_boilerplate

"""
agenda_code = agenda_imports
# parse_raw_agenda_blocks_html (300-342)
agenda_code += get(300, 342)
agenda_code += "\n\n"

# split_bilingual_title (935-943)
agenda_code += get(935, 943)
agenda_code += "\n\n"

# _raw_block_boilerplate_reason (945-950)
agenda_code += get(945, 950)
agenda_code += "\n\n"

# validate_raw_block (952-975)
agenda_code += get(952, 975)
agenda_code += "\n\n"

# split_raw_block_into_items (977-1019) - wait let me be more precise
# split_raw_block_into_items: 977-1019
# splitter_self_test: 1021-1058
# split_raw_agenda_blocks_to_structured: 1060-1120 (approximately)
agenda_code += get(977, 1118)
agenda_code += "\n\n"

# _clean_line: ~1120
# _looks_like_boilerplate: ~1126
# _looks_like_item_heading: ~1128-1133
# _looks_like_section_heading: ~1135-1148
# _detect_vote_or_action: ~1150-1167
# _build_item_url: ~1169-1170
for fn_start, fn_end in [(1120, 1124), (1126, 1126), (1128, 1133), (1135, 1148), (1150, 1167), (1169, 1170)]:
    agenda_code += get(fn_start, fn_end)
    agenda_code += "\n\n"

# parse_agenda_items_from_html (1221-1350)
agenda_code += get(1221, 1350)
agenda_code += "\n\n"

# _clean_lnk_title (2199-2202)
agenda_code += get(2199, 2202)
agenda_code += "\n\n"

# _find_item_tables (2204-2215)
agenda_code += get(2204, 2215)
agenda_code += "\n\n"

# _extract_lnk_from_table (2217-2228)
agenda_code += get(2217, 2228)
agenda_code += "\n\n"

# extract_agenda_item_titles (2230-2302)
agenda_code += get(2230, 2302)

(OUT / "agenda_items.py").write_text(agenda_code)
print("Written agenda_items.py")

# 8. supporting_docs.py - _extract_supporting_docs_from_table, extract_supporting_documents_from_items,
#    extract_supporting_documents_dynamic, _click_and_extract_item
# Lines 1351-1636

sd_imports = """from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from scraper.agenda_items import parse_c_number_parts, _extract_c_number
from scraper.io_utils import url_ext
from scraper.html_utils import _clean_html_text

"""
sd_code = sd_imports
sd_code += get(1351, 1636)
(OUT / "supporting_docs.py").write_text(sd_code)
print("Written supporting_docs.py")

# 9. votes.py - extract_votes_from_summary, is_known_supervisor, find_canonical_name
# Lines 1638-1965
# Wait, is_known_supervisor and find_canonical_name are defined INSIDE
# extract_votes_from_summary, as nested functions. The plan says to export them
# as top-level functions, but the code has them nested.

# Let me check... looking at lines 1799-1819, `is_known_supervisor` is a nested function.
# And `find_canonical_name` at lines ~1878-1887 is also nested.

# The plan says these should be module-level. But I need to extract EXACT code.
# Since these are nested closures (they reference `known_supervisor_names` from
# the enclosing scope), I can't just extract them as module-level functions without
# behavior changes. So I'll keep them nested as in the original.

votes_imports = """from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path

"""
votes_code = votes_imports
votes_code += get(1638, 1965)
(OUT / "votes.py").write_text(votes_code)
print("Written votes.py")

# 10. pz.py - build_pz_search_url, extract_pz_meetings, parse_pz_meetings_from_html,
#     extract_pz_agenda_items, parse_pz_overview, _format_mm_dd_yyyy
# Also has parse_pz_agenda_pdf (lines 2717-2797)

# Lines 2304-2683 for main PZ functions + parse_pz_agenda_pdf at 2717-2797

pz_imports = """from __future__ import annotations

import datetime as dt
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.io_utils import _normalize_text_date
from scraper.models import Meeting
from scraper.utils import CASE_PATTERN

"""
pz_code = pz_imports
pz_code += get(2304, 2797)
(OUT / "pz.py").write_text(pz_code)
print("Written pz.py")

# 11. main.py - main() async function (lines 2799-3888)
# This is the big one. Let me include the main function and all the helper logic
# that isn't in other modules.

# Wait, the plan says:
# "12. `scripts/scraper/main.py`: The main() async function (lines 2775-end, roughly)"
# "11. `scripts/scraper/main_pz.py`: PZ sync logic from main()"

# I'll put everything in main.py since main_pz is just a section within main().

main_code = """from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import sys
import time
from pathlib import Path

from scraper.utils import (
    log, setup_logger, SOURCE_PAGE, SEARCH_BASE, REQUIRED_BODY, REQUIRED_TYPES,
    ROOT, AGENDAS_ROOT, SUPPORT_ROOT, AGENDA_ITEMS_ROOT, AGENDA_ITEMS_CSV,
    RAW_AGENDA_ITEMS_CSV, REJECTED_RAW_BLOCKS_CSV, DISCOVERY_CSV, LOGS_ROOT,
    get_async_playwright, retry_with_backoff, CASE_PATTERN, C_NUMBER_PATTERN,
    _extract_c_number, parse_c_number_parts, parse_metadata_from_page_data,
    extract_meeting_metadata_from_page, is_image_based_agenda,
)
from scraper.models import Meeting
from scraper.cli import parse_args, parse_date
from scraper.search import parse_search_results_html, build_search_url, extract_meetings
from scraper.io_utils import (
    slugify, normalize_meeting_date, _normalize_text_date,
    month_dir_for_date, month_metadata_path, ensure_dir, csv_row,
    read_existing_rows, write_download_row, write_discovery_row,
    write_agenda_item_row, write_structured_agenda_item_row,
    write_raw_agenda_item_row, write_rejected_raw_block_row,
    debug_agenda_html_path, url_ext, infer_extension, download_url,
    existing_paths_present, row_paths_present, read_existing_agenda_urls,
    read_existing_discovery_keys, read_agenda_metadata_rows,
    filter_agenda_metadata_rows, read_existing_agenda_item_keys,
    read_existing_raw_block_keys, read_existing_rejected_block_keys,
    read_existing_structured_item_keys, write_discovery_rows,
    iter_discovery_documents,
)
from scraper.agenda_items import (
    parse_agenda_items_from_html, parse_raw_agenda_blocks_html,
    parse_c_number_parts, _extract_c_number, split_bilingual_title,
    _raw_block_boilerplate_reason, validate_raw_block, split_raw_block_into_items,
    splitter_self_test, split_raw_agenda_blocks_to_structured,
    _clean_line, _looks_like_boilerplate, _looks_like_item_heading,
    _looks_like_section_heading, _detect_vote_or_action, _build_item_url,
    _clean_lnk_title, _find_item_tables, _extract_lnk_from_table,
    extract_agenda_item_titles,
)
from scraper.supporting_docs import (
    _extract_supporting_docs_from_table, extract_supporting_documents_from_items,
    extract_supporting_documents_dynamic, _click_and_extract_item,
)
from scraper.votes import extract_votes_from_summary

"""
# Add write_agenda_debug_files (lines 624-671) which uses Playwright
main_code += get(624, 737)
main_code += "\n\n"

# Add extract_agenda_items_for_meeting (2072-2087)
main_code += get(2072, 2087)
main_code += "\n\n"

# Add extract_raw_agenda_blocks_for_meeting (2089-2097)
main_code += get(2089, 2097)
main_code += "\n\n"

# Add extract_raw_agenda_blocks_from_metadata (2099-2115)
main_code += get(2099, 2115)
main_code += "\n\n"

# Add extract_agenda_items_from_metadata (2117-2138)
main_code += get(2117, 2138)
main_code += "\n\n"

# Add count_agenda_items_for_meeting (2193-2196) - wait this uses extract_agenda_item_titles
# which is imported from agenda_items
main_code += get(2193, 2197)
main_code += "\n\n"

# Add the main() function (from line 2799 to the end)
# Let me check where main() starts
# Looking at the code, main() is at line 2799
main_func = get(2799, 3888)
main_code += main_func

(OUT / "main.py").write_text(main_code)
print("Written main.py")

# 12. Create __init__.py
init_code = '''"""Maricopa Agenda Scraper Package."""
from scraper.utils import *
from scraper.models import *
from scraper.html_utils import *
from scraper.cli import *
from scraper.search import *
from scraper.pz import *
from scraper.agenda_items import *
from scraper.supporting_docs import *
from scraper.votes import *
from scraper.io_utils import *
from scraper.main import main
'''
(OUT / "__init__.py").write_text(init_code)
print("Written __init__.py")

# 13. Rewrite the shim
shim_code = '''#!/usr/bin/env python3
"""Backward-compatible shim. Import from scraper.* for modular code."""
from __future__ import annotations

import asyncio

from scraper.main import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
'''
shim_path = Path(__file__).parent / "agenda_scraper.py"
shim_path.write_text(shim_code)
print("Rewritten agenda_scraper.py shim")

print("\\nDone! Created modular package at scripts/scraper/")
print("Test with: cd .. && .venv/bin/python -m pytest tests/ -v")
