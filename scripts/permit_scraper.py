#!/usr/bin/env python3
"""
Maricopa County Weekly Permit Activity Report scraper.

Usage:

    # Discover available reports and print to stdout
    python scripts/permit_scraper.py --discover

    # Discover and save index CSV
    python scripts/permit_scraper.py --discover --output-dir=data/permit-activity

    # Download reports (uses archive_index.csv to decide what to fetch)
    python scripts/permit_scraper.py --download
    python scripts/permit_scraper.py --download --limit 3
    python scripts/permit_scraper.py --download --start-date=2026-01-01 --end-date=2026-06-01
    python scripts/permit_scraper.py --download --force   # re-download even if present

    # Inspect the 3 newest downloaded reports
    python scripts/permit_scraper.py --inspect

    # All in one: discover, download (limit 5), inspect
    python scripts/permit_scraper.py --discover --download --limit 5 --inspect
"""

import argparse
import csv
import datetime
import hashlib
import io
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


# ── Constants ───────────────────────────────────────────────────────────────

ARCHIVE_URL = "https://www.maricopa.gov/Archive.aspx?AMID=128"
BASE_URL = "https://www.maricopa.gov"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
DEFAULT_OUTPUT_DIR = "data/permit-activity"
INDEX_FILENAME = "archive_index.csv"
RAW_SUBDIR = "raw"


# ── Date parsing ────────────────────────────────────────────────────────────

# Month name → number mapping
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Ordinal suffix pattern (1st, 2nd, 3rd, 4th, ...) — we strip these before parsing
ORDINAL_RE = re.compile(r"(\d+)(?:st|nd|rd|th)", re.I)


def _parse_report_date(text: str) -> Optional[str]:
    """Parse a date string like 'May 4, 2026' or 'December 19th, 2022'
    and return YYYY-MM-DD.

    Returns None if the date cannot be parsed.
    """
    text = text.strip()
    # Strip ordinal suffixes: 19th → 19
    text = ORDINAL_RE.sub(r"\1", text)
    # Remove stray commas around spaces: "November27,, 2023" → "November 27, 2023"
    text = re.sub(r"(\d)\s*,?\s*,?\s*(\d{4})", r"\1, \2", text)
    text = re.sub(r",\s*,", ",", text)
    # Clean internal spaces: "October 16 , 2023" → "October 16, 2023"
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+,", ",", text)

    # Try: "Month Day, Year"
    m = re.match(
        r"([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})",
        text,
    )
    if m:
        month_name = m.group(1).lower()
        day = int(m.group(2))
        year = int(m.group(3))
        month = MONTH_MAP.get(month_name)
        if month and 1 <= day <= 31 and 2000 <= year <= 2099:
            return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def _extract_links_from_archive(html: str) -> list[dict]:
    """Parse the archive page HTML and extract report links.

    Returns a list of dicts with keys: report_title, report_date, archive_url, adid.
    Dates are returned as YYYY-MM-DD strings.
    """
    records: list[dict] = []
    seen_adids: set[str] = set()

    # Structure:
    #   <a href="Archive.aspx?ADID=XXXX">
    #     <span>Weekly Permit Activity Report May 4, 2026  </span>
    #   </a>
    # Titles may include "(XLS)" suffix.
    link_pattern = re.compile(
        r'<a\s+href="Archive\.aspx\?ADID=(\d+)"[^>]*>'
        r'\s*<span[^>]*>(Weekly\s+Permit\s+Activity\s+Report\b.*?)</span>',
        re.I,
    )

    for adid, title_raw in link_pattern.findall(html):
        title_raw = title_raw.strip()

        if adid in seen_adids:
            continue
        seen_adids.add(adid)

        # Remove "(XLS)" marker before date extraction
        title_clean = re.sub(r"\s*\(XLS\)\s*", "", title_raw, flags=re.I).strip()
        title_for_date = title_clean  # use the cleaned title for date parsing

        # Extract date from the trailing portion of the title.
        # Handle odd spacing: "November27,, 2023" (no space), "October 16 , 2023" (space before comma)
        date_str = None
        date_match = re.search(
            r"([A-Za-z]+)[,\s]*(\d{1,2})(?:st|nd|rd|th)?[,\s]*(\d{4})\s*$",
            title_for_date,
        )
        if date_match:
            combined = f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}"
            date_str = _parse_report_date(combined)
        if not date_str:
            date_str = _parse_report_date(title_for_date)

        records.append({
            "report_title": title_clean,
            "report_date": date_str or "",
            "archive_url": f"{BASE_URL}/Archive.aspx?ADID={adid}",
            "adid": adid,
        })

    # Sort by date descending (newest first)
    records.sort(key=lambda r: (r["report_date"] or "0000-00-00", r["adid"]), reverse=True)
    return records


# ── File-type detection ─────────────────────────────────────────────────────

def _detect_file_type(content: bytes, content_type: Optional[str] = None) -> str:
    """Detect file type from magic bytes or Content-Type header."""
    if content_type:
        ct = content_type.lower()
        if "spreadsheet" in ct and "openxml" in ct:
            return "xlsx"
        if "vnd.ms-excel" in ct or "xls" in ct:
            return "xls"
        if "pdf" in ct:
            return "pdf"
        if "html" in ct:
            return "html"

    # Magic bytes
    if content[:4] == b"\x50\x4B\x03\x04":
        # Could be xlsx (OOXML) or zip
        return "xlsx"
    if content[:5] == b"\x25\x50\x44\x46\x2D":
        return "pdf"
    if content[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
        return "xls"
    if content[:4] == b"\xEF\xBB\xBF" or content[:2] in (b"\xFF\xFE", b"\xFE\xFF"):
        return "csv"

    return "unknown"


# ── HTML fetch helpers ──────────────────────────────────────────────────────

def _fetch_html(url: str) -> str:
    """Fetch a URL and return its text content."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_raw(url: str) -> tuple[bytes, Optional[str]]:
    """Fetch a URL and return (raw_bytes, content_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        ct = resp.headers.get("Content-Type")
        return resp.read(), ct


def _resolve_download_url(archive_url: str) -> tuple[str, bytes, Optional[str]]:
    """Given an archive URL (Archive.aspx?ADID=XXXX), follow the redirect
    to the ViewFile URL and return (viewfile_url, content_bytes, content_type).

    The ViewFile URL serves the actual file (XLSX / XLS / PDF).
    """
    req = urllib.request.Request(archive_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
            ct = resp.headers.get("Content-Type")
            final_url = resp.url
            return final_url, content, ct
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} fetching {archive_url}", file=sys.stderr)
        return archive_url, b"", None


# ── CSV index I/O ───────────────────────────────────────────────────────────

def _index_path(output_dir: str) -> Path:
    return Path(output_dir) / INDEX_FILENAME


def _load_index(output_dir: str) -> list[dict]:
    """Load existing archive_index.csv, returning list of dicts."""
    path = _index_path(output_dir)
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save_index(records: list[dict], output_dir: str):
    """Write records to archive_index.csv."""
    path = _index_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for r in records:
        if "scraped_at" not in r:
            r["scraped_at"] = now_iso

    fieldnames = [
        "report_title", "report_date", "archive_url",
        "resolved_download_url", "adid",
        "file_name", "file_type",
        "scraped_at",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        # Deduplicate by adid, keep newest first
        seen: set[str] = set()
        for r in records:
            adid = r.get("adid", "")
            if adid and adid not in seen:
                seen.add(adid)
                writer.writerow(r)


# ── Download ────────────────────────────────────────────────────────────────

def _download_record(
    record: dict,
    output_dir: str,
    force: bool = False,
) -> dict:
    """Download a single report, save to disk, and return updated record with
    resolved_download_url, file_name, and file_type.

    Idempotent: skips if the file already exists and force is False.
    """
    adid = record.get("adid", "")
    date_str = record.get("report_date", "")

    # Resolve download URL and get content
    archive_url = record["archive_url"]
    final_url, content, content_type = _resolve_download_url(archive_url)
    record["resolved_download_url"] = final_url

    # Determine file extension
    file_type = _detect_file_type(content, content_type)
    record["file_type"] = file_type

    ext_map = {"xlsx": ".xlsx", "xls": ".xls", "pdf": ".pdf", "csv": ".csv"}
    ext = ext_map.get(file_type, ".bin")

    # Generate filename: YYYY-MM-DD_ADID.ext
    date_prefix = date_str if date_str else "unknown_date"
    file_name = f"{date_prefix}_{adid}{ext}"
    record["file_name"] = file_name

    # Determine save path: raw/YYYY/YYYY-MM-DD/
    year = date_str[:4] if len(date_str) >= 4 else "unknown"
    save_dir = Path(output_dir) / RAW_SUBDIR / year / date_str
    save_path = save_dir / file_name

    # Check if file already exists and has the same content
    if save_path.exists() and not force:
        existing_hash = hashlib.sha256(save_path.read_bytes()).hexdigest()
        new_hash = hashlib.sha256(content).hexdigest()
        if existing_hash == new_hash:
            print(f"  Skipped {file_name} (already exists, unchanged)")
            return record

    # Save
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)
    print(f"  Downloaded {file_name} ({file_type}, {len(content):,} bytes)")
    return record


def _filter_records_by_date(
    records: list[dict],
    start_date: Optional[str],
    end_date: Optional[str],
) -> list[dict]:
    """Filter records by ISO date range (inclusive)."""
    if not start_date and not end_date:
        return records
    filtered = []
    for r in records:
        rd = r.get("report_date", "")
        if not rd:
            continue
        if start_date and rd < start_date:
            continue
        if end_date and rd > end_date:
            continue
        filtered.append(r)
    return filtered


# ── Inspect ─────────────────────────────────────────────────────────────────

def _inspect_report(path: Path):
    """Print file type and apparent column headers from a report."""
    content = path.read_bytes()
    file_type = _detect_file_type(content)
    print(f"\n  File: {path.name}")
    print(f"  Type: {file_type}")
    print(f"  Size: {len(content):,} bytes")

    if file_type == "xlsx":
        # Attempt to read first sheet via zipfile + xml; fallback to strings
        try:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                # Find the shared strings and first sheet
                sheet_names = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
                if sheet_names:
                    sheet_xml = z.read(sheet_names[0]).decode("utf-8", errors="replace")
                    # Extract text between <c>...</c> tags as a crude header extraction
                    header_cells = re.findall(r'<c[^>]*>.*?<v>(.*?)</v>', sheet_xml[:5000])
                    if header_cells:
                        print(f"  First cell values: {header_cells[:10]}")
                    # Try to read shared strings for more readable output
                    try:
                        ss_xml = z.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                        strings = re.findall(r"<si>.*?<t>(.*?)</t>.*?</si>", ss_xml, re.DOTALL)
                        if strings and header_cells:
                            resolved = [strings[int(s)] if s.isdigit() and int(s) < len(strings) else s for s in header_cells]
                            print(f"  Headers (resolved): {resolved[:15]}")
                    except KeyError:
                        pass
                    # Print raw XML snippet of first 3 rows
                    rows = re.findall(r"<row[^>]*>(.*?)</row>", sheet_xml[:10000], re.DOTALL)
                    print(f"  Rows in first sheet (approx): {len(rows)}")
        except ImportError:
            print("  (install 'openpyxl' for detailed XLSX inspection)")
        except Exception as e:
            print(f"  XLSX parse error: {e}")
    elif file_type == "xls":
        print("  (install 'xlrd' for XLS inspection)")
    elif file_type == "pdf":
        print("  (install 'pdfplumber' or 'tabula' for PDF inspection)")
    else:
        # Attempt to print first few lines for text-based formats
        try:
            text = content.decode("utf-8", errors="replace")
            lines = text.strip().split("\n")[:5]
            for i, line in enumerate(lines):
                print(f"  Line {i}: {line[:200]}")
        except Exception:
            pass


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maricopa County Weekly Permit Activity Report scraper",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Enumerate all report links from the archive page",
    )
    parser.add_argument(
        "--download", action="store_true",
        help="Download reports (uses archive_index.csv)",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="Print file type and column headers from the newest 3 downloaded reports",
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of reports to process",
    )
    parser.add_argument(
        "--start-date", default=None,
        help="Earliest report date (inclusive, YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date", default=None,
        help="Latest report date (inclusive, YYYY-MM-DD)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download even if files already exist",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    output_dir = args.output_dir
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # ── Discover ────────────────────────────────────────────────────────
    if args.discover:
        print(f"Fetching archive page: {ARCHIVE_URL}", file=sys.stderr)
        html = _fetch_html(ARCHIVE_URL)
        records = _extract_links_from_archive(html)
        print(f"Found {len(records)} reports", file=sys.stderr)

        # Filter by date range
        if args.start_date or args.end_date:
            records = _filter_records_by_date(records, args.start_date, args.end_date)
            print(f"After date filter: {len(records)} reports", file=sys.stderr)

        # Apply limit (before saving — latest records)
        if args.limit is not None:
            records = records[: args.limit]
            print(f"After limit: {len(records)} reports", file=sys.stderr)

        # Merge with existing index (preserve resolved URLs from prior runs)
        existing = _load_index(output_dir)
        existing_by_adid = {r["adid"]: r for r in existing if r.get("adid")}

        merged = []
        for r in records:
            adid = r.get("adid", "")
            if adid and adid in existing_by_adid:
                old = existing_by_adid[adid]
                for key in ("resolved_download_url", "file_name", "file_type", "scraped_at"):
                    if old.get(key):
                        r[key] = old[key]
            merged.append(r)

        _save_index(merged, output_dir)
        print(f"Saved index to {_index_path(output_dir)}", file=sys.stderr)

        # Print to stdout
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=["report_date", "adid", "report_title", "file_type", "file_name"],
            extrasaction="ignore",
        )
        writer.writeheader()
        for r in merged:
            writer.writerow(r)

    # ── Download ────────────────────────────────────────────────────────
    if args.download:
        records = _load_index(output_dir)
        # Auto-discover if the index is empty or very small (unlikely to
        # represent the full archive).  The user can always pre-populate
        # with an explicit --discover for full control.
        if len(records) < 100:
            print("Index empty or incomplete — auto-discovering...", file=sys.stderr)
            html = _fetch_html(ARCHIVE_URL)
            fresh = _extract_links_from_archive(html)
            if fresh:
                # Preserve any resolved download URLs from the old index
                old_by_adid = {r["adid"]: r for r in records if r.get("adid")}
                for r in fresh:
                    adid = r.get("adid", "")
                    if adid in old_by_adid:
                        old = old_by_adid[adid]
                        for k in ("resolved_download_url", "file_name", "file_type"):
                            if old.get(k):
                                r[k] = old[k]
                records = fresh
                _save_index(records, output_dir)
                print(f"Discovered {len(records)} reports", file=sys.stderr)
            else:
                print("Auto-discover found nothing.", file=sys.stderr)

        # Filter by date range
        if args.start_date or args.end_date:
            records = _filter_records_by_date(records, args.start_date, args.end_date)

        # Apply limit (from the NEWEST records since they're sorted by date desc)
        if args.limit is not None:
            records = records[: args.limit]

        if args.limit and not (args.start_date or args.end_date):
            print(f"Downloading up to {args.limit} reports (newest first)", file=sys.stderr)
        else:
            print(f"Downloading {len(records)} reports", file=sys.stderr)

        updated = []
        for record in records:
            updated.append(_download_record(record, output_dir, force=args.force))
            time.sleep(2)  # rate-limit politeness

        # Update index with resolved URLs and file info
        _save_index(updated, output_dir)

    # ── Inspect ─────────────────────────────────────────────────────────
    if args.inspect:
        raw_dir = Path(output_dir) / RAW_SUBDIR
        if not raw_dir.exists():
            print("No raw downloads found. Run --download first.", file=sys.stderr)
            sys.exit(1)

        # Find all downloaded files, sorted by date-modified or path (newest first)
        all_files = sorted(
            raw_dir.rglob("*"),
            key=lambda p: p.stat().st_mtime if p.is_file() else 0,
            reverse=True,
        )
        files = [f for f in all_files if f.is_file() and f.suffix in (".xlsx", ".xls", ".pdf", ".csv")]

        if not files:
            print("No report files found under {raw_dir}.", file=sys.stderr)
            sys.exit(1)

        limit = min(3, len(files))
        print(f"Inspecting {limit} newest downloaded report(s):", file=sys.stderr)
        for f in files[:limit]:
            _inspect_report(f)
        print()


if __name__ == "__main__":
    main()
