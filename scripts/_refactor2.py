#!/usr/bin/env python3
"""Correct extraction of sections from maricopa_agenda_scraper.py (from git) into modular package.

Run from scripts/ directory.
"""
from __future__ import annotations

from pathlib import Path


SRC = Path("/tmp/orig_scraper.py")
PKG = Path(__file__).parent / "scraper"


def get(start: int, end: int) -> str:
    """Return lines start..end inclusive (1-indexed)."""
    lines = SRC.read_text().splitlines(keepends=True)
    return "".join(lines[start - 1:end])


# ── utils.py: imports, log, setup_logger, constants, get_async_playwright,
#    retry_with_backoff, CASE_PATTERN, PZ constants, C_NUMBER_PATTERN,
#    _extract_c_number, parse_c_number_parts, parse_metadata_from_page_data,
#    extract_meeting_metadata_from_page, is_image_based_agenda
#    (Lines: 1-89, 1172-1173, 1174-1175, 1176-1212, 1966-2073)
utils = ""
utils += get(1, 89)    # imports, log, logger, constants, get_async_playwright, retry_with_backoff
utils += get(1172, 1173)  # CASE_PATTERN
utils += get(1174, 1175)  # PZ_SEARCH_BASE, PZ_AGENDA_BASE
utils += get(1176, 1212)  # C_NUMBER_PATTERN, _extract_c_number, parse_c_number_parts
utils += get(1966, 2077)  # parse_metadata_from_page_data, extract_meeting_metadata_from_page, is_image_based_agenda
(PKG / "utils.py").write_text(utils)

# ── models.py: Meeting, _HtmlNode, _TreeBuilder (lines 93-163)
models = """from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

"""
models += get(93, 163)
(PKG / "models.py").write_text(models)

# ── html_utils.py: _parse_html, _node_text, _clean_html_text, etc. (lines 165-215)
html = """from __future__ import annotations

import html
import re
from typing import Optional

from scraper.models import _HtmlNode, _TreeBuilder

"""
html += get(165, 215)
(PKG / "html_utils.py").write_text(html)

# ── cli.py: parse_args, _parse_bos_args, _parse_pz_args, parse_date (lines 343-443)
cli = """from __future__ import annotations

import argparse
import datetime as dt
import sys

from scraper.utils import log

"""
cli += get(343, 443)
(PKG / "cli.py").write_text(cli)

# ── search.py: parse_search_results_html, build_search_url, extract_meetings
search = """from __future__ import annotations

import re
import urllib.parse

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text, _search_results_table_present
from scraper.io_utils import normalize_meeting_date
from scraper.models import Meeting

"""
search += get(217, 298)
search += "\n\n"
search += get(445, 453)
search += "\n\n"
search += get(2140, 2161)
(PKG / "search.py").write_text(search)

# ── io_utils.py: all CSV I/O and file utility functions
io = """from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

"""
io += get(455, 653)   # slugify .. debug_agenda_html_path
io += get(714, 905)   # url_ext .. read_existing_rejected_block_keys
io += get(907, 921)   # read_existing_structured_item_keys
io += get(2163, 2191) # iter_discovery_documents, write_discovery_rows
(PKG / "io_utils.py").write_text(io)

# ── agenda_items.py: all agenda item parsing functions
ag = """from __future__ import annotations

import csv
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _has_class, _node_text
from scraper.io_utils import _clean_line, _looks_like_boilerplate

"""
ag += get(300, 342)   # parse_raw_agenda_blocks_html
ag += get(907, 1170)  # split_bilingual_title .. _build_item_url
ag += get(1176, 1176) # CASE_PATTERN (re-declared as a local constant)
ag += get(1176, 1176) # PZ_SEARCH_BASE comment - actually let me check
# Wait, CASE_PATTERN is already in utils.py. But agenda_items.py used to access it as a module-level.
# The plan says CASE_PATTERN goes to utils.py. And agenda_items.py's _build_item_url doesn't use it.
# Let me check what parse_agenda_items_from_html uses from the original module-level scope.
# parse_agenda_items_from_html uses _clean_html_text, _build_item_url, _detect_vote_or_action,
# _extract_c_number, parse_c_number_parts, split_bilingual_title, CASE_PATTERN
# So CASE_PATTERN must be imported from utils.
# And it uses _clean_html_text from html_utils.
# Also uses _extract_c_number and parse_c_number_parts which are in utils.

# Let me also add the parse_agenda_items_from_html function
ag += get(1221, 1350)  # parse_agenda_items_from_html
# And the title extraction helpers
ag += get(2199, 2302)  # _clean_lnk_title, _find_item_tables, _extract_lnk_from_table, extract_agenda_item_titles
(PKG / "agenda_items.py").write_text(ag)

# ── supporting_docs.py
sd = """from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from scraper.utils import _extract_c_number, parse_c_number_parts
from scraper.io_utils import url_ext
from scraper.html_utils import _clean_html_text

"""
sd += get(1351, 1636)
(PKG / "supporting_docs.py").write_text(sd)

# ── votes.py
votes = """from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path

"""
votes += get(1638, 1965)
(PKG / "votes.py").write_text(votes)

# ── pz.py
pz = """from __future__ import annotations

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
pz += get(2304, 2669)  # build_pz_search_url .. _format_mm_dd_yyyy
pz += get(2684, 2777)  # parse_pz_agenda_pdf
(PKG / "pz.py").write_text(pz)

# ── main.py: orchestrator functions and main()
main = """from __future__ import annotations

import asyncio
import datetime as dt
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
    debug_agenda_html_path, write_agenda_debug_files,
    url_ext, infer_extension, download_url,
    existing_paths_present, row_paths_present, read_existing_agenda_urls,
    read_existing_discovery_keys, read_agenda_metadata_rows,
    filter_agenda_metadata_rows, read_existing_agenda_item_keys,
    read_existing_raw_block_keys, read_existing_rejected_block_keys,
    read_existing_structured_item_keys, write_discovery_rows,
    iter_discovery_documents,
)
from scraper.agenda_items import (
    parse_agenda_items_from_html, parse_raw_agenda_blocks_html,
    split_bilingual_title,
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
# write_agenda_debug_files (lines 656-712)
main += get(656, 712)
main += "\n\n"

# extract_agenda_items_for_meeting (lines 2063-2076)
main += get(2063, 2076)
main += "\n\n"

# extract_raw_agenda_blocks_for_meeting (lines 2078-2085)
main += get(2078, 2085)
main += "\n\n"

# extract_raw_agenda_blocks_from_metadata (lines 2087-2108)
main += get(2087, 2108)
main += "\n\n"

# extract_agenda_items_from_metadata (lines 2110-2138)
main += get(2110, 2138)
main += "\n\n"

# count_agenda_items_for_meeting (lines 2193-2197)
main += get(2193, 2197)
main += "\n\n"

# main() function (lines 2775-3888)
main += get(2775, 3888)

(PKG / "main.py").write_text(main)

# ── __init__.py
init = '''"""Maricopa Agenda Scraper Package."""
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
(PKG / "__init__.py").write_text(init)

print("Done! All module files written.")
