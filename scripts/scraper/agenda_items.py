from __future__ import annotations

import csv
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _has_class, _node_text
from scraper.io_utils import (
    read_existing_structured_item_keys, read_existing_rejected_block_keys,
    write_rejected_raw_block_row, write_structured_agenda_item_row,
    ensure_dir,
)
from scraper.utils import AGENDA_ITEMS_CSV, RAW_AGENDA_ITEMS_CSV, REJECTED_RAW_BLOCKS_CSV, CASE_PATTERN, _extract_c_number, parse_c_number_parts

def parse_raw_agenda_blocks_html(html: str, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []

    root = _parse_html(html)
    container = next(
        (
            node
            for node in _find_all(root, "div")
            if node.attrs.get("id") == "agenda-table" and _has_class(node, "container-fluid")
        ),
        None,
    )
    if container is None:
        return []

    normalized_meeting = {
        "meeting_id": (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting",
        "meeting_date": (meeting.get("record_date") or meeting.get("meeting_date") or "").strip(),
        "meeting_type": (meeting.get("meeting_type") or "").strip(),
    }

    blocks: list[dict[str, str]] = []
    for index, table in enumerate(_find_all(container, "table"), start=1):
        raw_text = _clean_html_text(_node_text(table))
        if not re.search(r"(?<!\d)\d+\.\s+", raw_text):
            continue
        if not any((anchor.attrs.get("id") or "").lower().startswith("lnkagendaitem_") for anchor in _find_all(table, "a")):
            continue
        blocks.append({
            "source_body": "Board of Supervisors",
            "meeting_id": normalized_meeting["meeting_id"],
            "meeting_date": normalized_meeting["meeting_date"],
            "meeting_type": normalized_meeting["meeting_type"],
            "raw_block_index": str(index),
            "raw_text": raw_text,
            "source_url": source_url,
        })

    return blocks


def split_bilingual_title(title: str) -> str:
    title = _clean_line(title)
    if " - " in title:
        return title.split(" - ", 1)[0].strip()
    if " / " in title:
        return title.split(" / ", 1)[0].strip()
    return title


def _raw_block_boilerplate_reason(line: str) -> str:
    if _looks_like_boilerplate(line):
        return "boilerplate first line"
    if re.search(r"\baudio access code\b", line, re.I):
        return "contains Audio Access code boilerplate"
    return ""


def validate_raw_block(raw_text: str) -> tuple[bool, str]:
    text = (raw_text or "").strip()
    if not text:
        return False, "empty raw text"
    first_line = _clean_line(text.splitlines()[0] if text.splitlines() else text)
    if not first_line:
        return False, "missing first line"
    if re.match(r"^\d{1,2}:\d{2}\s?[AP]M\b", first_line, re.I):
        return False, "begins with time"
    if re.match(r"^\d+\s+[A-Za-z]", first_line):
        return False, "begins with address"
    boilerplate_reason = _raw_block_boilerplate_reason(first_line)
    if boilerplate_reason:
        return False, boilerplate_reason
    if not re.match(r"^\d+\.\s+.+", first_line):
        return False, "does not begin with numbered agenda item"

    spam_terms = [
        "meeting location",
        "board members",
        "mission",
        "webinar",
        "public notice",
        "live video feeds",
        "the public is invited",
        "accommodations for individuals",
    ]
    lowered = text.lower()
    if any(term in lowered for term in spam_terms):
        return False, "contains non-agenda notice text"

    return True, ""


def split_raw_block_into_items(raw_text: str) -> list[dict[str, str]]:
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    if not text:
        return []

    matches = list(re.finditer(r"(?<!\d)(\d+)\.\s+", text))
    if not matches:
        return []

    items: list[dict[str, str]] = []

    for idx, match in enumerate(matches):
        number = int(match.group(1))
        if idx == 0:
            if number != 1 and len(matches) > 1:
                # still accept the first visible top-level item if it is the first number we see
                pass
        else:
            prev_number = int(matches[idx - 1].group(1))
            if number != prev_number + 1:
                continue

        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if not re.match(r"^\d+\.\s+", block):
            continue

        header = re.match(r"^(\d+)\.\s*(.*)$", block)
        if not header:
            continue

        agenda_number = header.group(1)
        body = header.group(2).strip()
        title = split_bilingual_title(body)
        if not title:
            title = body[:200]

        items.append({
            "agenda_item_number": agenda_number,
            "agenda_item_title": title,
            "agenda_item_text": block,
        })

    return items


def splitter_self_test(verbose: bool = False) -> bool:
    cases = [
        (
            "3. TREASURER ... 4. RECORDER ...",
            2,
            ["3", "4"],
        ),
        (
            "6. DOMRES 90 Case #: MCP250001 a. Development shall ... b. Site plan shall ...",
            1,
            ["6"],
        ),
        (
            "1. ROLL CALL 2. INVOCATION 3. PLEDGE OF ALLEGIANCE",
            3,
            ["1", "2", "3"],
        ),
        (
            "This item includes 24 hours advance notice for public comment.",
            0,
            [],
        ),
        (
            "Audio Access code 154-419-871 is provided for attendees.",
            0,
            [],
        ),
        (
            "1. TITLE ... (C-06-25-252-X-00) 2. TITLE ...",
            2,
            ["1", "2"],
        ),
    ]

    passed = True
    for idx, (sample, expected_count, expected_numbers) in enumerate(cases, start=1):
        items = split_raw_block_into_items(sample)
        numbers = [item["agenda_item_number"] for item in items]
        ok = len(items) == expected_count and numbers == expected_numbers
        passed = passed and ok
        if verbose:
            print(f"splitter_self_test case {idx}: {'PASS' if ok else 'FAIL'} (got {len(items)} items: {numbers})")

    if verbose:
        print(f"splitter_self_test overall: {'PASS' if passed else 'FAIL'}")
    return passed


def split_raw_agenda_blocks_to_structured() -> int:
    if not RAW_AGENDA_ITEMS_CSV.exists():
        print("No raw_agenda_items.csv found.")
        return 0

    ensure_dir(AGENDA_ITEMS_CSV.parent)
    if not AGENDA_ITEMS_CSV.exists():
        AGENDA_ITEMS_CSV.write_text(
            "source_body,meeting_id,meeting_date,meeting_type,agenda_item_number,agenda_item_title,agenda_item_text,source_url\n",
            encoding="utf-8",
        )
    existing_keys = read_existing_structured_item_keys(AGENDA_ITEMS_CSV)
    rejected_keys = read_existing_rejected_block_keys(REJECTED_RAW_BLOCKS_CSV)
    wrote = 0

    with RAW_AGENDA_ITEMS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            meeting_id = (raw_row.get("meeting_id") or "").strip()
            meeting_date = (raw_row.get("meeting_date") or "").strip()
            meeting_type = (raw_row.get("meeting_type") or "").strip()
            source_url = (raw_row.get("source_url") or "").strip()
            raw_text = raw_row.get("raw_text") or ""
            raw_block_index = (raw_row.get("raw_block_index") or "").strip()
            is_valid, reason = validate_raw_block(raw_text)
            if not is_valid:
                key = (meeting_id, raw_block_index)
                if key not in rejected_keys:
                    write_rejected_raw_block_row({
                        "source_body": "Board of Supervisors",
                        "meeting_id": meeting_id,
                        "meeting_date": meeting_date,
                        "meeting_type": meeting_type,
                        "raw_block_index": raw_block_index,
                        "raw_text": raw_text,
                        "source_url": source_url,
                        "rejection_reason": reason,
                    })
                    rejected_keys.add(key)
                continue

            for item in split_raw_block_into_items(raw_text):
                key = (meeting_id, item["agenda_item_number"], item["agenda_item_title"])
                if key in existing_keys:
                    continue
                write_structured_agenda_item_row({
                    "source_body": "Board of Supervisors",
                    "meeting_id": meeting_id,
                    "meeting_date": meeting_date,
                    "meeting_type": meeting_type,
                    "agenda_item_number": item["agenda_item_number"],
                    "agenda_item_title": item["agenda_item_title"],
                    "agenda_item_text": item["agenda_item_text"],
                    "source_url": source_url,
                })
                existing_keys.add(key)
                wrote += 1

    return wrote


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line or "").strip()


def _looks_like_boilerplate(line: str) -> bool:
    return bool(re.match(r"^(?:page\s+\d+.*|copyright.*|hyland software.*|view meeting.*|agenda online.*)$", line, re.I))


def _looks_like_item_heading(line: str) -> Optional[re.Match[str]]:
    line = _clean_line(line)
    if not line or _looks_like_boilerplate(line):
        return None
    return re.match(r"^(?P<number>\d+(?:\.\d+)*)\.?\s*(?P<title>.*)$", line)


def _looks_like_section_heading(line: str) -> bool:
    line = _clean_line(line)
    if not line or _looks_like_boilerplate(line):
        return False
    if re.match(r"^\d", line):
        return False
    if len(line) > 180:
        return False
    if any(ch.isdigit() for ch in line):
        return False
    if any(token in line for token in [":", "/", "AM", "PM"]):
        return False
    letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", line)
    if not letters:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / max(len(letters), 1)
    return upper_ratio >= 0.65 and bool(re.search(r"[A-Za-zÀ-ÿ]", line))


def _detect_vote_or_action(text: str) -> str:
    t = text.lower()
    action_patterns = [
        (r"\bno action\b", "no action"),
        (r"\breceived and filed\b", "received and filed"),
        (r"\bapproved\b", "approved"),
        (r"\badopted\b", "adopted"),
        (r"\bpassed\b", "passed"),
        (r"\bfailed\b", "failed"),
        (r"\bdenied\b", "denied"),
        (r"\bcontinued\b", "continued"),
        (r"\bheld\b", "held"),
        (r"\bpostponed\b", "postponed"),
    ]
    for pattern, label in action_patterns:
        if re.search(pattern, t, re.I):
            return label
    return ""


def _build_item_url(source_url: str, agenda_item_id: str) -> str:
    return f"{source_url}#{urllib.parse.quote(agenda_item_id, safe='')}"



def parse_agenda_items_from_html(html: str, source_url: str, meeting: dict[str, str]) -> list[dict[str, str]]:
    """Extract agenda items from HTML by identifying true top-level numbered items.

    Real agenda items are marked in the HTML with a bold <span> containing
    the item number followed by a period:
        <span style="font-weight:bold">1.</span>

    This avoids promoting nested numbered paragraphs (a., b., c.), warrant
    numbers, dollar amounts, parcel numbers, and boilerplate into agenda rows.
    Item titles use the first lnkAgendaItem anchor after the bold span.
    Subsequent links in the same table are section headings for upcoming items.
    Items without any anchor fall back to the most recent section heading.
    """
    meeting_id = meeting["meeting_id"]
    bold_item_pattern = re.compile(
        r'<span[^>]*font-weight:bold[^>]*>(\d+)\.</span>'
    )

    item_spans: list[tuple[int, int]] = []
    for m in bold_item_pattern.finditer(html):
        num = int(m.group(1))
        pos = m.start()
        item_spans.append((num, pos))

    if not item_spans:
        return []

    item_spans.sort(key=lambda x: x[1])

    seen_positions: set[int] = set()
    deduped: list[tuple[int, int]] = []
    for num, pos in item_spans:
        if pos in seen_positions:
            continue
        seen_positions.add(pos)
        deduped.append((num, pos))

    items: list[dict[str, str]] = []
    pending_section = ""

    for item_num, pos in deduped:
        number_str = str(item_num)

        before = html[:pos]
        tstart = before.rfind("<table")
        tend = html.find("</table>", pos)
        if tstart < 0 or tend < 0:
            continue
        table_html = html[tstart : tend + 8]

        # Find ALL lnkAgendaItem anchors in this table (after the bold span)
        bold_offset = pos - tstart
        lnk_titles: list[str] = []
        for lm in re.finditer(
            r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>', table_html, re.DOTALL
        ):
            if lm.start() <= bold_offset:
                continue
            raw = re.sub(r"<[^>]+>", " ", lm.group(1)).strip()
            raw = _clean_html_text(raw)
            if raw:
                lnk_titles.append(raw)

        if lnk_titles:
            title = split_bilingual_title(lnk_titles[0])
            for extra_title in lnk_titles[1:]:
                pending_section = split_bilingual_title(extra_title)
        else:
            if pending_section:
                title = pending_section
            else:
                # No title in table and no pending section — scan backward in
                # the full HTML for the nearest preceding lnkAgendaItem.
                # Handles items like "CALL TO THE PUBLIC" whose section heading
                # lives in the gap between item tables.
                title = f"Item {number_str}"
                before_html = html[:pos]
                for prev_m in reversed(
                    list(
                        re.finditer(
                            r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>',
                            before_html,
                            re.DOTALL,
                        )
                    )
                ):
                    raw = re.sub(r"<[^>]+>", " ", prev_m.group(1)).strip()
                    raw = _clean_html_text(raw)
                    if raw:
                        title = split_bilingual_title(raw)
                        break

        item_id = f"{meeting_id}-{number_str}-item"
        full_text = _clean_html_text(
            re.sub(r"<[^>]+>", " ", table_html)
        )

        items.append({
            "source_body": "Board of Supervisors",
            "meeting_id": meeting_id,
            "meeting_date": meeting["meeting_date"],
            "meeting_type": meeting["meeting_type"],
            "agenda_item_section": "",
            "agenda_item_id": item_id,
            "agenda_item_number": number_str,
            "agenda_item_title": title,
            "agenda_item_text": full_text,
            "agenda_item_url": _build_item_url(source_url, item_id),
            "vote_or_action": _detect_vote_or_action(full_text),
            "c_number": _extract_c_number(full_text),
            "c_number_base": "",
            "c_number_revision": "",
            "case_number": "",
            "source_url": source_url,
        })

        # Populate base/revision after the item dict is in items
        # Extract case number from item text and title
        c_m = CASE_PATTERN.search(full_text + " " + (title or ""))
        if c_m:
            items[-1]["case_number"] = c_m.group(1).upper()
        c_num = items[-1]["c_number"]
        if c_num:
            parts = parse_c_number_parts(c_num)
            items[-1]["c_number_base"] = parts["c_number_base"]
            items[-1]["c_number_revision"] = parts["c_number_revision"]

    return items


def _clean_lnk_title(text: str) -> str:
    """Decode HTML entities and collapse whitespace."""
    return _clean_html_text(text)


def _find_item_tables(html: str) -> list[tuple[int, int, int]]:
    """Find all numbered agenda items and their containing table boundaries.

    Returns: list of (item_number, bold_span_position, table_end_position)
    """
    bold_pattern = re.compile(
        r'<span[^>]*font-weight:bold[^>]*>(\d+)\.</span>'
    )
    items: list[tuple[int, int, int]] = []
    for m in bold_pattern.finditer(html):
        num = int(m.group(1))
        pos = m.start()
        tend = html.find("</table>", pos)
        if tend < 0:
            continue
        items.append((num, pos, tend))
    return items


def _extract_lnk_from_table(table_html: str, bold_offset: int) -> list[str]:
    """Extract all lnkAgendaItem titles from a table that appear after the bold span."""
    titles: list[str] = []
    for m in re.finditer(
        r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>', table_html, re.DOTALL
    ):
        if m.start() <= bold_offset:
            continue  # Before the bold span — not the item's title
        text = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        text = _clean_lnk_title(text)
        if text:
            titles.append(text)
    return titles


async def extract_agenda_item_titles(page, meeting_url: str) -> list[tuple[int, str]]:
    """Visit an agenda HTML page and extract (item_number, title) pairs.

    Finds bold numbered <span> elements and their associated titles:
    - Uses the first lnkAgendaItem anchor after the bold span in the item's
      own table as the title.
    - Subsequent lnkAgendaItems in the same table are section headings for
      the next items.
    - Items without any anchor in their table fall back to the nearest
      preceding lnkAgendaItem by position in the full HTML.
    """
    await page.goto(meeting_url, wait_until="load")
    html = await page.content()

    # Find all numbered items with their table boundaries
    items = _find_item_tables(html)
    if not items:
        return []

    # Sort by display position
    items.sort(key=lambda x: x[1])

    # Build position-sorted list of all lnkAgendaItem entries
    all_lnk_positions: list[tuple[str, int]] = []
    for m in re.finditer(
        r'id="lnkAgendaItem_\d+"[^>]*>(.*?)</a>', html, re.DOTALL
    ):
        text = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        text = _clean_lnk_title(text)
        if text:
            all_lnk_positions.append((text, m.start()))
    all_lnk_positions.sort(key=lambda x: x[1])

    # Track latest section heading found as extra lnk within an item's table
    pending_section = ""

    results: list[tuple[int, str, int]] = []
    for idx, (num, item_pos, tend) in enumerate(items):
        tstart = html.rfind("<table", 0, item_pos)
        if tstart < 0:
            results.append((num, pending_section, item_pos))
            continue

        table_html = html[tstart : tend + 8]
        lnk_titles = _extract_lnk_from_table(table_html, item_pos - tstart)

        if lnk_titles:
            # First title is the item's own
            title = lnk_titles[0]
            # Subsequent titles in the same table are section headings
            # for upcoming items (e.g. "CALL TO THE PUBLIC" in the
            # same table as the preceding FCD item)
            for extra_title in lnk_titles[1:]:
                pending_section = extra_title
        else:
            # No title anchor in this item's table — fall back to
            # the most recently seen section heading (from a prior
            # item's extra lnkAgendaItem, not from the TOC area).
            title = pending_section

        results.append((num, title, item_pos))

    return [(num, title) for num, title, _ in results]

