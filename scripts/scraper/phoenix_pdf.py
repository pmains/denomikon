"""
Phoenix meeting extraction from the city's AEM JSON API.

Phoenix's city council meetings page loads meeting data via a JSON endpoint:
  .../dynamic_table.table-results.json

Each meeting has properties including agenda PDF, results PDF, and meeting type.
"""

from __future__ import annotations
import json
import logging
import re
import subprocess
import tempfile
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

PHOENIX_GOV = "https://www.phoenix.gov"
JSON_API = (
    PHOENIX_GOV
    + "/administration/departments/cityclerk/programs-services/"
    + "city-council-meetings/"
    + "_jcr_content/root/container/container/"
    + "container-content/dynamic_table.table-results.json"
)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Meeting types that are City Council (not subcommittees or info packets)
COUNCIL_TYPES = {"Formal Meeting", "Policy Session", "Work Study", "Special"}

# Body code assignment
BODY_CODE = "phoenix-cc"


# ── JSON API ──

def fetch_all_meetings() -> list[dict]:
    """Paginate through the JSON API and return all meetings."""
    all_meetings: list[dict] = []
    total = 9999
    offset = 0

    while offset < total:
        url = f"{JSON_API}?offset={offset}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                results = data.get("results", [])
                if not results:
                    break
                total = int(data.get("resultTotal", 0))
                for r in results:
                    props = r.get("properties", {})
                    agenda_url = props.get("agendaDocumentLinkPdf", "") or ""
                    results_url = props.get("resultsDocumentLinkPDF", "") or ""
                    if agenda_url:
                        agenda_url = PHOENIX_GOV + agenda_url
                    if results_url:
                        results_url = PHOENIX_GOV + results_url
                    all_meetings.append({
                        "meeting_date": (props.get("meetingDatetime") or "")[:10].replace("T", ""),
                        "meeting_type": props.get("meetingType", ""),
                        "agenda_url": agenda_url or None,
                        "results_url": results_url or None,
                        "minutes_url": (props.get("minutesDocumentLinkPDF") or ""),
                    })
                offset += len(results)
        except Exception as e:
            log.warning("Error at offset %d: %s", offset, e)
            break

    # Normalize dates
    for m in all_meetings:
        d = m["meeting_date"]
        if d and "T" in d:
            m["meeting_date"] = d[:10]
        elif d and "-" not in d:
            m["meeting_date"] = ""

    return all_meetings


# ── PDF extraction ──

def fetch_pdf_bytes(url: str) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


_BOILERPLATE = re.compile(
    r"^(Agenda Date|Page \d+ of \d+|City Council|City of Phoenix|Mayor and Council|"
    r"Formal Meeting|Policy Session|Work Study|"
    r"Printed on|Data Refreshed|Agenda Online)"
)

_ITEM_START = re.compile(r"Item No\.\s+\*?(\d+)")
_ITEM_ADDON = re.compile(r"\*\*\*ITEM (?:REVISED|ADD.ON|CONTINUED)")


def parse_agenda_items(text: str) -> list[dict]:
    """Extract agenda items with full descriptions from a Phoenix agenda PDF.

    Captures everything between consecutive "Item No." markers
    (minus boilerplate lines) as agenda_item_text for that item.
    Only the first occurrence of each item number is used; page-break
    continuations that repeat the item number are merged into the same entry.
    """
    items: list[dict] = []
    sort_order = 0
    lines = text.split("\n") if text else []

    # Find all item start positions, dedup by item number (keep first)
    seen: set[int] = set()
    item_starts: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _ITEM_START.search(line)
        if m:
            num = int(m.group(1))
            if num not in seen and num <= 999:
                seen.add(num)
                item_starts.append((i, num))

    if not item_starts:
        return items

    for idx, (start_line, item_num) in enumerate(item_starts):
        end_line = item_starts[idx + 1][0] if idx + 1 < len(item_starts) else len(lines)

        body_lines: list[str] = []
        title = ""
        for j in range(start_line + 1, end_line):
            s = lines[j].strip()
            if not s:
                if body_lines:
                    body_lines.append("")
                continue
            if _BOILERPLATE.match(s) or _ITEM_ADDON.match(s):
                continue
            if not title:
                title = s[:200]
            # Skip pure-page-number lines and continuation headers
            if re.match(r"^\d+$", s):
                continue
            body_lines.append(s)

        if title:
            sort_order += 1
            body_text = "\n".join(body_lines).strip()
            items.append({
                "agenda_item_number": str(item_num),
                "item_type_category": "item",
                "agenda_item_title": title,
                "agenda_item_text": body_text if body_text else title,
                "sort_order": sort_order,
            })

    return items


def fetch_and_parse_agenda(agenda_url: str, meeting_id: str) -> list[dict]:
    """Download and parse agenda items from a Phoenix agenda PDF."""
    pdf_bytes = fetch_pdf_bytes(agenda_url)
    if not pdf_bytes:
        return []
    text = extract_pdf_text(pdf_bytes)
    if not text or len(text) < 100:
        return []
    items = parse_agenda_items(text)
    # Add meeting_id and source fields
    for item in items:
        an = item.get("agenda_item_number", "") or ""
        item["meeting_id"] = meeting_id
        item["agenda_item_id"] = f"phoenix-cc-{meeting_id}_{an}"
        item["source_body"] = "phoenix-cc"
        item["source_url"] = agenda_url
    return items
