from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from scraper.common.utils import (
    AGENDAS_ROOT, SUPPORT_ROOT, AGENDA_ITEMS_ROOT, AGENDA_ITEMS_CSV,
    RAW_AGENDA_ITEMS_CSV, REJECTED_RAW_BLOCKS_CSV, DISCOVERY_CSV, LOGS_ROOT, ROOT, SOURCE_PAGE,
)

def slugify(value: str) -> str:
    value = re.sub(r"[\u0300-\u036f]", "", value or "")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value.lower() or "meeting"


def normalize_meeting_date(raw: str) -> str:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw or "")
    if not m:
        return ""
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def _normalize_text_date(raw: str) -> str:
    """Parse a text date like 'Apr 23, 2026' or 'April 9, 2026' to YYYY-MM-DD."""
    from datetime import datetime as _dt
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            d = _dt.strptime(raw.strip(), fmt)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def month_dir_for_date(date_iso: str, base: Path) -> Path:
    year, month, _ = date_iso.split("-")
    return base / year / month


def month_metadata_path(date_iso: str) -> Path:
    return month_dir_for_date(date_iso, AGENDAS_ROOT) / "metadata.csv"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def csv_row(fieldnames: list[str], row: dict[str, str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writerow(row)
    return buf.getvalue()


def read_existing_rows() -> dict[str, dict[str, str]]:
    existing: dict[str, dict[str, str]] = {}
    for csv_path in AGENDAS_ROOT.rglob("metadata.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    agenda_url = (row.get("agenda_url") or row.get("document_url") or "").strip()
                    if agenda_url:
                        existing[agenda_url] = row
        except FileNotFoundError:
            continue
    return existing


def write_download_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "document_category",
        "record_id",
        "record_date",
        "record_time",
        "record_title",
        "meeting_type",
        "source_page_url",
        "document_url",
        "local_path",
        "download_status",
        "downloaded_at",
        "source_search_url",
        "notes",
    ]
    csv_path = month_metadata_path(row["record_date"])
    ensure_dir(csv_path.parent)
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_discovery_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "document_category",
        "record_id",
        "record_date",
        "record_time",
        "record_title",
        "meeting_type",
        "source_page_url",
        "document_url",
        "local_path",
        "download_status",
        "downloaded_at",
        "source_search_url",
        "notes",
    ]
    ensure_dir(DISCOVERY_CSV.parent)
    new_file = not DISCOVERY_CSV.exists()
    with DISCOVERY_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_agenda_item_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "agenda_item_section",
        "agenda_item_id",
        "agenda_item_number",
        "agenda_item_title",
        "agenda_item_text",
        "agenda_item_url",
        "vote_or_action",
        "source_url",
    ]
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    new_file = not AGENDA_ITEMS_CSV.exists()
    with AGENDA_ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_structured_agenda_item_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "agenda_item_number",
        "agenda_item_title",
        "agenda_item_text",
        "source_url",
    ]
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    new_file = not AGENDA_ITEMS_CSV.exists()
    with AGENDA_ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_raw_agenda_item_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "raw_block_index",
        "raw_text",
        "source_url",
    ]
    ensure_dir(RAW_AGENDA_ITEMS_CSV.parent)
    new_file = not RAW_AGENDA_ITEMS_CSV.exists()
    with RAW_AGENDA_ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def write_rejected_raw_block_row(row: dict[str, str]) -> None:
    fieldnames = [
        "source_body",
        "meeting_id",
        "meeting_date",
        "meeting_type",
        "raw_block_index",
        "raw_text",
        "source_url",
        "rejection_reason",
    ]
    ensure_dir(REJECTED_RAW_BLOCKS_CSV.parent)
    new_file = not REJECTED_RAW_BLOCKS_CSV.exists()
    with REJECTED_RAW_BLOCKS_CSV.open("a", newline="", encoding="utf-8") as f:
        if new_file:
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
        f.write(csv_row(fieldnames, row))


def debug_agenda_html_path(meeting_id: str, suffix: str) -> Path:
    return LOGS_ROOT / f"agenda_debug_{meeting_id}{suffix}"
def url_ext(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    ext = Path(path).suffix
    return ext if ext else ""


def infer_extension(url: str, content_type: str, fallback: str = ".bin") -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct:
        return ".html"
    ext = url_ext(url)
    if ext:
        return ext
    return fallback


def download_url(url: str, destination: Path) -> tuple[Path, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req) as resp:  # nosec - public county documents
        data = resp.read()
        content_type = resp.headers.get_content_type() if resp.headers else ""
    actual_destination = destination
    if destination.suffix == ".bin":
        actual_destination = destination.with_suffix(infer_extension(url, content_type, ".bin"))
    ensure_dir(actual_destination.parent)
    actual_destination.write_bytes(data)
    return actual_destination, content_type


def existing_paths_present(paths: str) -> bool:
    parts = [p for p in (paths or "").split(";") if p]
    if not parts:
        return False
    return all((ROOT / p).exists() for p in parts)


def row_paths_present(row: dict[str, str]) -> bool:
    return existing_paths_present(row.get("local_file_paths", "") or row.get("local_path", ""))


def read_existing_agenda_urls(csv_paths: Iterable[Path]) -> set[str]:
    existing: set[str] = set()
    for csv_path in csv_paths:
        if not csv_path.exists():
            continue
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    agenda_url = (row.get("agenda_url") or row.get("document_url") or "").strip()
                    if agenda_url:
                        existing.add(agenda_url)
        except FileNotFoundError:
            continue
    return existing


def read_existing_discovery_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                category = (row.get("document_category") or "").strip()
                url = (row.get("document_url") or "").strip()
                if category and url:
                    existing.add((category, url))
    except FileNotFoundError:
        pass
    return existing


def read_agenda_metadata_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for csv_path in AGENDAS_ROOT.rglob("metadata.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    category = (row.get("document_category") or row.get("documentCategory") or "").strip().lower()
                    agenda_url = (row.get("document_url") or row.get("agenda_url") or "").strip()
                    if category != "agenda" or not agenda_url:
                        continue
                    rows.append(row)
        except FileNotFoundError:
            continue
    return rows


def filter_agenda_metadata_rows(
    rows: list[dict[str, str]],
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
    limit: Optional[int] = None,
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for row in rows:
        record_date = (row.get("record_date") or row.get("meeting_date") or "").strip()
        try:
            parsed = dt.date.fromisoformat(record_date)
        except Exception:
            continue
        if start_date and parsed < start_date:
            continue
        if end_date and parsed > end_date:
            continue
        filtered.append(row)
        if limit is not None and len(filtered) >= limit:
            break
    return filtered


def read_existing_agenda_item_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                agenda_item_id = (row.get("agenda_item_id") or "").strip()
                agenda_item_url = (row.get("agenda_item_url") or "").strip()
                key = (meeting_id, agenda_item_id or agenda_item_url)
                if key[0] and key[1]:
                    existing.add(key)
    except FileNotFoundError:
        pass
    return existing


def read_existing_raw_block_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                raw_block_index = (row.get("raw_block_index") or "").strip()
                if meeting_id and raw_block_index:
                    existing.add((meeting_id, raw_block_index))
    except FileNotFoundError:
        pass
    return existing


def read_existing_rejected_block_keys(csv_path: Path) -> set[tuple[str, str]]:
    existing: set[tuple[str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                raw_block_index = (row.get("raw_block_index") or "").strip()
                if meeting_id and raw_block_index:
                    existing.add((meeting_id, raw_block_index))
    except FileNotFoundError:
        pass
    return existing


def read_existing_structured_item_keys(csv_path: Path) -> set[tuple[str, str, str]]:
    existing: set[tuple[str, str, str]] = set()
    if not csv_path.exists():
        return existing
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                meeting_id = (row.get("meeting_id") or "").strip()
                agenda_item_number = (row.get("agenda_item_number") or "").strip()
                agenda_item_title = (row.get("agenda_item_title") or "").strip()
                if meeting_id and agenda_item_number:
                    existing.add((meeting_id, agenda_item_number, agenda_item_title))
    except FileNotFoundError:
        pass
    return existing

def iter_discovery_documents(meeting: Meeting):
    yield "agenda", meeting.agenda_url
    yield "summary", meeting.summary_url
    yield "minutes", meeting.minutes_url
    yield "video", meeting.video_url


def write_discovery_rows(meeting: Meeting, search_url: str, existing_keys: set[tuple[str, str]]) -> None:
    for category, url in iter_discovery_documents(meeting):
        if not url or (category, url) in existing_keys:
            continue
        write_discovery_row({
            "source_body": "Board of Supervisors",
            "document_category": category,
            "record_id": meeting.meeting_id,
            "record_date": meeting.meeting_date,
            "record_time": meeting.meeting_time,
            "record_title": meeting.meeting_title,
            "meeting_type": meeting.meeting_type,
            "source_page_url": SOURCE_PAGE,
            "document_url": url,
            "local_path": "",
            "download_status": "discovered",
            "downloaded_at": "",
            "source_search_url": search_url,
            "notes": "",
        })
        existing_keys.add((category, url))

