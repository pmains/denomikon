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
import logging
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


def _build_meeting_id(body_slug: str, date_str: str, title: str, existing_ids: set[str] | None = None) -> str:
    """Build a unique meeting ID from body and date only."""
    base = f"{body_slug}-{date_str}"
    if existing_ids is None:
        return base
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _build_agenda_item_id(body_slug: str, meeting_id: str) -> str:
    """Build a single agenda item ID for a meeting."""
    return f"{body_slug}-{meeting_id}-item-1"


# -- PDF agenda parsing -------------------------------------------------------


def _extract_agenda_items_from_pdf(pdf_url: str, meeting_date: str) -> list[dict] | None:
    """Download a PDF via the Node.js helper, extract text via pdftotext,
    and parse agenda items.

    Looks for numbered items (1., 2., 3., etc.) followed by titles.
    Returns a list of item dicts matching the agenda_item_dicts format,
    or None if parsing fails.
    """
    import tempfile
    log = logging.getLogger(__name__)

    # Download via the Node.js helper (handles tempe.gov's Akamai blocking)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_pdf.close()
    try:
        result = _helper_download(pdf_url, tmp_pdf.name)
        if not result.get("downloaded"):
            log.debug("PDF download failed for %s: %s", pdf_url, result.get("error", "unknown"))
            return None
    except Exception as e:
        log.debug("PDF download exception for %s: %s", pdf_url, e)
        return None

    # Extract text with pdftotext
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", tmp_pdf.name, "-"],
            capture_output=True, text=True, timeout=30,
        )
        text = proc.stdout
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed for %s: %s", pdf_url, e)
        return None
    finally:
        try:
            os.unlink(tmp_pdf.name)
        except (NameError, OSError):
            pass

    if not text or len(text.strip()) < 50:
        return None

    # Parse numbered items: "1. Title" or "1. Title" followed by description lines
    items = []
    sort_order = 0
    lines = text.split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        # Match "1. Title" optionally followed by Action/Information/Discussion
        m = re.match(r"^(\d+)\.\s+(.+?)\s*(Action|Information|Discussion)?$", line, re.I)
        if m:
            num = m.group(1)
            title = m.group(2).strip()
            item_type = (m.group(3) or "").lower()
            # If the type wasn't on this line, check the next non-empty line
            if not item_type:
                for j in range(i + 1, min(i + 5, len(lines))):
                    nxt = lines[j].strip()
                    if not nxt:
                        continue
                    tm = re.match(r"^(Action|Information|Discussion)$", nxt, re.I)
                    if tm:
                        item_type = tm.group(1).lower()
                    break
            sort_order += 1
            # Classify item
            is_substantive = title.lower() not in (
                "call to order", "adjourn"
            )
            items.append({
                "agenda_item_number": num,
                "agenda_item_title": title,
                "agenda_item_text": "",
                "item_type": "item" if is_substantive else "",
                "sort_order": sort_order,
            })

    return items if items else None


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
    existing_ids_by_body: dict[str, set[str]] = {}

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
        # Collect existing meeting IDs for this body to avoid collisions
        if body_slug not in existing_ids_by_body:
            existing_ids_by_body[body_slug] = set()
            rows = session.execute(
                select(MeetingModel.meeting_id).where(
                    MeetingModel.body == body_slug
                )
            ).fetchall()
            existing_ids_by_body[body_slug] = {r[0] for r in rows}

        meeting_id = _build_meeting_id(
            body_slug, date_str, clean_title,
            existing_ids=existing_ids_by_body[body_slug],
        )
        existing_ids_by_body[body_slug].add(meeting_id)

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

        # Try to extract agenda items from the PDF
        parsed_items = _extract_agenda_items_from_pdf(pdf_url, date_str)

        if parsed_items:
            agenda_item_dicts = []
            for pit in parsed_items:
                an = pit["agenda_item_number"]
                item_title = pit["agenda_item_title"]
                item_type = pit["item_type"]
                sort_order = pit["sort_order"]
                agenda_item_dicts.append({
                    "agenda_item_id": f"{body_slug}-{meeting_id}-item-{an}",
                    "agenda_item_number": an,
                    "agenda_item_title": item_title,
                    "agenda_item_text": "",
                    "agenda_item_url": pdf_url,
                    "item_type": item_type,
                    "source_body": body_slug,
                    "source_url": pdf_url,
                    "sort_order": sort_order,
                })
        else:
            # Fall back to a single generic item
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
