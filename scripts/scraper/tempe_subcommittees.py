#!/usr/bin/env python3
"""Tempe Council Subcommittees Scraper.

Fetches agenda and minutes documents from Tempe Council Subcommittee
document folders on tempe.gov (CivicPlus CMS). Uses a Node.js helper
for HTTP requests since tempe.gov blocks direct Python/curl/Playwright
but allows node's native fetch() via Akamai.

Usage:
    python -m scripts.scraper.tempe_subcommittees [--all] [--body <slug>]
        [--download] [--limit N]

Sync handler in main.py:
    python agenda_scraper.py tempe-subcommittees --sync
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Project path setup
_SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent
_HELPER_PATH = _SCRIPT_DIR / "tempe_subcommittees_helper.mjs"

# -- Subcommittee configuration ------------------------------------------------

# Maps database slug -> (page slug used in tempe.gov URL, parent_folder, agenda_folder, minutes_folder)
SUBOMMITTEES = {
    "tempe-animal-welfare-subcommittee": {
        "page_slug": "animal-welfare-and-cruelty-in-tempe-council-subcommittee",
        "parent_folder": 7910,
        "agenda_folder": 7911,
        "minutes_folder": 7912,
    },
    "tempe-community-engagement-subcommittee": {
        "page_slug": "community-engagement-and-connection-council-subcommittee",
        "parent_folder": 7913,
        "agenda_folder": 7914,
        "minutes_folder": 7915,
    },
    "tempe-drink-spiking-subcommittee": {
        "page_slug": "drink-spiking-education-and-prevention-council-subcommittee",
        "parent_folder": 7842,
        "agenda_folder": 7843,
        "minutes_folder": 7844,
    },
    "tempe-mixed-use-space-subcommittee": {
        "page_slug": "mixed-use-space-council-subcommittee",
        "parent_folder": 7887,
        "agenda_folder": 7888,
        "minutes_folder": 7889,
    },
    "tempe-mobility-safety-subcommittee": {
        "page_slug": "motorized-and-electric-mobility-device-safety-council-subcommittee",
        "parent_folder": 7917,
        "agenda_folder": 7918,
        "minutes_folder": 7919,
    },
    "tempe-town-lake-subcommittee": {
        "page_slug": "revitalization-of-tempe-town-lake-council-subcommittee",
        "parent_folder": 7705,
        "agenda_folder": 7706,
        "minutes_folder": 7707,
    },
    "tempe-term-limits-subcommittee": {
        "page_slug": "tempe-term-limits-policy-review-council-subcommittee",
        "parent_folder": 7990,
        "agenda_folder": 7991,
        "minutes_folder": 7992,
    },
    "tempe-advocacy-review-subcommittee": {
        "page_slug": "federal-and-state-advocacy-review-council-subcommittee",
        "parent_folder": 7987,
        "agenda_folder": 7988,
        "minutes_folder": 7989,
    },
}


# -- Node.js helper interface --------------------------------------------------


def _helper_fetch(path: str) -> dict:
    """Call the Node.js helper to fetch a page and return parsed JSON."""
    result = subprocess.run(
        ["node", str(_HELPER_PATH), "fetch", path],
        capture_output=True, text=True, timeout=30,
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Helper fetch failed: {stderr}")
    return json.loads(result.stdout)


def _helper_download(url: str, output_path: str) -> dict:
    """Call the Node.js helper to download a PDF file."""
    result = subprocess.run(
        ["node", str(_HELPER_PATH), "download", url, output_path],
        capture_output=True, text=True, timeout=60,
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Helper download failed: {stderr}")
    return json.loads(result.stdout)


# -- Date parsing --------------------------------------------------------------


_DATE_PATTERNS = [
    # YYMMDD without separators, e.g. 260217 = 2026-02-17
    re.compile(r"(?<![0-9])(\d{2})(\d{2})(\d{2})(?![0-9])"),
    # 6.2.26 or 06/02/2026
    re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})"),
    # 12-08-2025
    re.compile(r"(\d{1,2})[-.](\d{1,2})[-.](\d{2,4})"),
]


def _parse_date_from_title(title: str) -> Optional[str]:
    """Extract a date from a PDF title. Returns YYYY-MM-DD or None."""
    # Clean common date formatting issues before parsing
    cleaned = title
    for pat in _DATE_PATTERNS:
        for m in reversed(list(pat.finditer(cleaned))):
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            # Try YYMMDD format (e.g. 260217)
            if a > 20 and a < 99:
                if 1 <= b <= 12 and 1 <= c <= 31:
                    return f"20{a:02d}-{b:02d}-{c:02d}"
                # Handle YY0MMDD → YYMMDD (e.g. 2600217 → 2026-02-17)
                if b == 0 and 1 <= c <= 12:
                    return f"20{a:02d}-{c:02d}-01"
                # Handle YYMM0DD → YYMMDD (e.g. 260217)
                if 1 <= b <= 12 and c == 0:
                    return f"20{a:02d}-{b:02d}-01"
            # Try conventional M/D/Y format
            if 1 <= a <= 12 and 1 <= b <= 31:
                year = c
                if year < 100:
                    year += 2000
                if 2000 <= year <= 2030:
                    return f"{year:04d}-{a:02d}-{b:02d}"
    return None


def _slugify(text: str) -> str:
    """Simple slugify for document titles."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


# -- Document parsing ----------------------------------------------------------


def _is_archived(title: str) -> bool:
    """Check if a document title indicates it's archived (past meeting)."""
    return title.startswith("ARCHIVED") or title.startswith("ARCHIEVED")


def _is_cancellation(title: str) -> bool:
    """Check if a document is a cancellation notice (not a real meeting)."""
    return bool(re.search(r"cancel", title, re.I))


def _build_meeting_id(body_slug: str, date_str: str, title: str) -> str:
    """Build a unique meeting ID."""
    suffix = _slugify(title[:40])
    return f"{body_slug}-{date_str}-{suffix}"


def _build_agenda_item_id(body_slug: str, meeting_id: str) -> str:
    """Build a single agenda item ID for a meeting."""
    return f"{body_slug}-{meeting_id}-item-1"


# -- Scraper logic -------------------------------------------------------------


def _folder_url(page_slug: str, folder_id: int) -> str:
    """Build the URL for a document folder page."""
    return (
        f"/government/mayor-and-city-council/council-subcommittees"
        f"/{page_slug}/-folder-{folder_id}"
    )


def sync_subcommittee(
    body_slug: str,
    download: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Sync a single subcommittee's agenda documents.

    Returns a list of meeting dicts created.
    """
    sc = SUBOMMITTEES[body_slug]
    page_slug = sc["page_slug"]
    agenda_folder = sc["agenda_folder"]
    minutes_folder = sc["minutes_folder"]

    # Import DB modules
    from db import get_session, init_db, replace_meeting_data_safe
    from db.models import Meeting as MeetingModel
    from sqlalchemy import select

    init_db()
    session = get_session()
    meetings_created = []

    # Fetch agenda documents
    print(f"  Fetching agenda folder {agenda_folder}...")
    try:
        data = _helper_fetch(_folder_url(page_slug, agenda_folder))
    except Exception as e:
        print(f"  ERROR fetching agenda folder: {e}")
        session.close()
        return []

    agenda_docs = data.get("documents", [])
    if not agenda_docs:
        print(f"  No agenda documents found.")
        session.close()
        return []

    # Fetch minutes documents
    minutes_docs = []
    try:
        minutes_data = _helper_fetch(_folder_url(page_slug, minutes_folder))
        minutes_docs = minutes_data.get("documents", [])
    except Exception:
        pass  # Minutes folder may be empty

    # Build a map of minutes docs by date (keep order for index matching)
    minutes_by_date: dict[str, list[dict]] = {}
    for mdoc in minutes_docs:
        mdate = _parse_date_from_title(mdoc["title"])
        if mdate:
            minutes_by_date.setdefault(mdate, []).append(mdoc)

    # Track which minutes index to use per date (handles multiple docs per date)
    minutes_index: dict[str, int] = {}

    # Build meeting records from agenda documents
    for idx, doc in enumerate(agenda_docs):
        if limit and idx >= limit:
            break

        title = doc["title"]
        pdf_url = doc["url"]

        # Skip cancellation notices (not actual meetings)
        if _is_cancellation(title):
            print(f"  Skip cancellation: {title[:50]}")
            continue

        # Parse date
        date_str = _parse_date_from_title(title)
        if not date_str:
            print(f"  WARNING: Could not parse date from: {title}")
            continue

        # Build meeting identity
        meeting_type = "Council Subcommittee Meeting"
        clean_title = re.sub(r"^ARCHIVED\s+", "", title, flags=re.I)
        clean_title = re.sub(r"^ARCHIEVED\s+", "", clean_title, flags=re.I)
        clean_title = re.sub(r"\(pdf\)$", "", clean_title, flags=re.I).strip()
        meeting_id = _build_meeting_id(body_slug, date_str, clean_title)

        # Check if already synced
        existing = session.execute(
            select(MeetingModel).where(
                MeetingModel.body == body_slug,
                MeetingModel.meeting_id == meeting_id,
            )
        ).scalar_one_or_none()
        if existing and existing.sync_status in ("complete", "no_agenda"):
            continue

        print(f"  Meeting: {date_str} | {clean_title[:60]}...")

        # Match this agenda doc to the Nth minutes doc for the same date
        minutes_index.setdefault(date_str, 0)
        matching_minutes = minutes_by_date.get(date_str, [])
        this_minutes_idx = minutes_index[date_str]
        minutes_url = None
        if this_minutes_idx < len(matching_minutes):
            minutes_url = matching_minutes[this_minutes_idx]["url"]
            minutes_index[date_str] = this_minutes_idx + 1

        # Build meeting dict
        meeting_dict = {
            "meeting_id": meeting_id,
            "meeting_date": date_str,
            "meeting_type": meeting_type,
            "meeting_title": clean_title,
            "source_url": pdf_url,
        }
        if minutes_url:
            meeting_dict["minutes_url"] = minutes_url

        # Build a single agenda item for this meeting
        agenda_item_dicts = [
            {
                "agenda_item_id": _build_agenda_item_id(body_slug, meeting_id),
                "agenda_item_number": "1",
                "agenda_item_title": clean_title,
                "agenda_item_text": f"Council Subcommittee meeting on {date_str}",
                "agenda_item_url": pdf_url,
                "item_type": "item",
                "source_body": body_slug,
                "source_url": pdf_url,
                "sort_order": 0,
            }
        ]

        # Build supporting documents
        supporting_doc_dicts = [
            {
                "agenda_item_id": 0,
                "agenda_item_number": "1",
                "document_title": f"Agenda: {clean_title}",
                "document_url": pdf_url,
                "document_type": "Agenda",
                "file_name": f"{body_slug}_{date_str}_agenda.pdf",
                "file_extension": ".pdf",
            }
        ]
        if minutes_url:
            supporting_doc_dicts.append({
                "agenda_item_id": 0,
                "agenda_item_number": "1",
                "document_title": f"Minutes: {clean_title}",
                "document_url": minutes_url,
                "document_type": "Minutes",
                "file_name": f"{body_slug}_{date_str}_minutes.pdf",
                "file_extension": ".pdf",
            })

        # Persist
        try:
            replace_meeting_data_safe(
                session, body_slug, meeting_id,
                meeting_dict, agenda_item_dicts,
                supporting_doc_dicts= supporting_doc_dicts,
            )
            meetings_created.append({
                "body": body_slug,
                "meeting_id": meeting_id,
                "meeting_date": date_str,
                "title": clean_title,
                "agenda_url": pdf_url,
                "minutes_url": minutes_url,
            })
        except Exception as e:
            print(f"  ERROR persisting meeting {meeting_id}: {e}")
            continue

        # Download the PDF if requested
        if download:
            from scraper.io_utils import ensure_dir
            from sqlalchemy import select

            docs_dir = _PROJECT_ROOT / "data" / "tempe-subcommittees" / body_slug / date_str
            ensure_dir(str(docs_dir))

            agenda_path = str(docs_dir / f"agenda.pdf")
            try:
                _helper_download(pdf_url, agenda_path)
            except Exception as e:
                print(f"    WARNING: Agenda PDF download failed: {e}")

            if minutes_url:
                minutes_path = str(docs_dir / f"minutes.pdf")
                try:
                    _helper_download(minutes_url, minutes_path)
                except Exception as e:
                    print(f"    WARNING: Minutes PDF download failed: {e}")

    session.close()
    return meetings_created


def sync_all(download: bool = False, limit: int | None = None) -> int:
    """Sync all Tempe council subcommittees.

    Returns total meetings created.
    """
    total = 0
    for body_slug in sorted(SUBOMMITTEES.keys()):
        print(f"\n=== {body_slug} ===")
        meetings = sync_subcommittee(body_slug, download=download, limit=limit)
        total += len(meetings)
        print(f"  Created {len(meetings)} meeting(s)")
    return total


# -- CLI entry point -----------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="tempe-subcommittees")
    parser.add_argument("--sync", action="store_true", help="Sync all subcommittees")
    parser.add_argument("--all", action="store_true", help="Sync all subcommittees")
    parser.add_argument("--body", help="Sync a specific subcommittee slug")
    parser.add_argument("--download", action="store_true", help="Download PDF files")
    parser.add_argument("--limit", type=int, default=None, help="Max meetings per body")

    args = parser.parse_args()

    if not (args.sync or args.all or args.body):
        parser.print_help()
        return 1

    if args.body:
        if args.body not in SUBOMMITTEES:
            print(f"Unknown subcommittee slug: {args.body}")
            print(f"Valid slugs: {', '.join(sorted(SUBOMMITTEES.keys()))}")
            return 1
        meetings = sync_subcommittee(args.body, download=args.download, limit=args.limit)
        print(f"Created {len(meetings)} meeting(s)")
        return 0

    if args.sync or args.all:
        total = sync_all(download=args.download, limit=args.limit)
        print(f"\nTotal: {total} meeting(s) created across all subcommittees")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
