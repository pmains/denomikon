"""DOM-based BOS meeting summary vote extractor.

Walks the #agendaView content DIV's TABLE children (Aspose.Words HTML output)
to identify agenda items, motions, and vote records using DOM structure instead
of flat-text regex.  This avoids the boundary ambiguity of the legacy regex
parser — each TABLE is a natural boundary.

Architecture (layers):
  1. Walk DOM children → block list (item / motion / ayes / section_header / other)
  2. Associate motion + ayes blocks with their preceding item block
  3. Parse motion text → mover, seconder, motion_type
  4. Parse ayes block → supervisor names + vote (yes/nay/absent)
  5. Detect consent-agenda items (no individual vote following)
  6. Return structured vote dicts compatible with persist_votes()
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


# ── helpers ──────────────────────────────────────────────────────────

def _is_item_table(text: str) -> bool:
    """True if the table text starts with a number followed by a dot."""
    return bool(re.match(r"^\s*\d+\.", text))


def _extract_item_number(text: str) -> int | None:
    """Return the agenda item number from a N. prefix, or None."""
    m = re.match(r"^\s*(\d+)\.", text)
    return int(m.group(1)) if m else None


def _is_motion_table(text: str) -> bool:
    """True if the table text is a motion declaration."""
    return bool(re.match(r"^Motion\s+to\b", text, re.I))


def _is_ayes_table(text: str) -> bool:
    """True if the table text starts with Ayes: or Roll Call:."""
    return bool(re.match(r"^(?:Ayes:|Roll\s+Call:)", text))


def _is_section_header(text: str) -> bool:
    """True if the table looks like a section header, not an item."""
    t = text.strip()
    # All-caps headers that aren't numbered items
    if re.match(r"^[A-Z][A-Z\s\-/&]+$", t) and not re.match(r"^\d+\.", t):
        return True
    # Bilingual headers like "PLANNING AND ZONING HEARINGS - AUDIENCIAS..."
    if re.match(r"^[A-Z][A-Z\s]+-\s+[A-Z]", t):
        return True
    return False


# ── DOM walker ───────────────────────────────────────────────────────

async def _get_content_div(page: Page) -> dict | None:
    """Get the last DIV child of #agendaView (the content container).

    Returns a Playwright ElementHandle or None if the container is empty
    (e.g. Executive session summaries with no vote records).
    """
    try:
        handle = await page.evaluate_handle(
            """() => {
                const av = document.getElementById("agendaView");
                if (!av) return null;
                const divs = av.querySelectorAll(":scope > div");
                if (divs.length === 0) return null;
                return divs[divs.length - 1];
            }"""
        )
        return handle
    except Exception:
        return None


async def _get_children_tags(handle) -> list[dict]:
    """Return [{tag, index, text}] for each child of the content DIV.

    We use a single evaluate to avoid many round-trips.
    """
    result = await handle.evaluate(
        """(el) => {
            const items = [];
            for (let i = 0; i < el.children.length; i++) {
                const c = el.children[i];
                items.push({
                    tag: c.tagName,
                    index: i,
                    text: c.textContent || ""
                });
            }
            return items;
        }"""
    )
    return result


# ── block builder ────────────────────────────────────────────────────

def _build_blocks(children: list[dict]) -> list[dict]:
    """Convert flat DOM children into typed blocks.

    Returns:
        [{type, index, item_number, text, trimmed}]
        Types: "item", "motion", "ayes", "section_header", "other"
    """
    blocks: list[dict] = []
    for child in children:
        tag = child["tag"]
        if tag != "TABLE":
            continue
        text = child["text"]
        trimmed = text.strip()

        if _is_item_table(trimmed):
            blocks.append({
                "type": "item",
                "index": child["index"],
                "item_number": _extract_item_number(trimmed),
                "text": text,
                "trimmed": trimmed,
            })
        elif _is_motion_table(trimmed):
            blocks.append({
                "type": "motion",
                "index": child["index"],
                "text": text,
                "trimmed": trimmed,
            })
        elif _is_ayes_table(trimmed):
            blocks.append({
                "type": "ayes",
                "index": child["index"],
                "text": text,
                "trimmed": trimmed,
            })
        elif _is_section_header(trimmed):
            blocks.append({
                "type": "section_header",
                "index": child["index"],
                "text": text,
                "trimmed": trimmed,
            })
        else:
            blocks.append({
                "type": "other",
                "index": child["index"],
                "text": text,
                "trimmed": trimmed,
            })
    return blocks


# ── block grouping ───────────────────────────────────────────────────

def _group_item_blocks(blocks: list[dict]) -> list[dict]:
    """Group motion + ayes blocks with their preceding item block.

    Each item block becomes: {item_block, sub_items: [{motion, ayes}]}
    where sub_items is a list of (motion, ayes) pairs.  Most items have
    exactly one sub-item.  Items with lettered sub-items (a., b., c.)
    produce multiple, one per motion+ayes pair seen.

    Sub-items without explicit motions (e.g. consent sub-items that reuse
    the previous motion type) are detected by a new ayes block appearing
    after a non-ayes intervening block.
    """
    groups: list[dict] = []
    current_group: dict | None = None
    current_sub: dict | None = None  # {motion, ayes[]}

    def _flush_sub():
        nonlocal current_sub
        if current_sub and current_group:
            if current_sub["motion"] is not None or current_sub["ayes"]:
                current_group.setdefault("sub_items", []).append(current_sub)
        current_sub = {"motion": None, "ayes": []}

    prev_block_type: str | None = None

    for block in blocks:
        if block["type"] == "item":
            _flush_sub()
            if current_group:
                groups.append(current_group)
            current_group = {"item": block, "sub_items": [], "footnotes": []}
            current_sub = {"motion": None, "ayes": []}
            prev_block_type = "item"
        elif current_group is None:
            groups.append(block)
            prev_block_type = block["type"]
        elif block["type"] == "motion":
            if current_sub and (current_sub["motion"] is not None or current_sub["ayes"]):
                _flush_sub()
            current_sub["motion"] = block
            prev_block_type = "motion"
        elif block["type"] == "ayes":
            if current_sub and current_sub["ayes"]:
                # A new ayes block.  If the previous block was also an ayes,
                # this is a continuation of the same vote (split across TABLEs).
                # Otherwise it belongs to a new sub-item.
                if prev_block_type != "ayes":
                    _flush_sub()
            current_sub["ayes"].append(block)
            prev_block_type = "ayes"
        elif block["type"] == "other":
            current_group.setdefault("footnotes", []).append(block)
            prev_block_type = "other"
        elif block["type"] == "section_header":
            _flush_sub()
            groups.append(block)
            if current_group:
                groups.append(current_group)
                current_group = None
                current_sub = None
            prev_block_type = "section_header"
        else:
            current_group.setdefault("footnotes", []).append(block)
            prev_block_type = block["type"]

    _flush_sub()
    if current_group:
        groups.append(current_group)

    return groups


# ── vote parsers ─────────────────────────────────────────────────────

_MOTION_RE = re.compile(
    r"Motion\s+to\s+(?P<motion>\w+(?:\s+\w+)*?)"
    r"(?:\.|,)?"
    r"\s+by\s+Supervisor\s+(?P<mover>[^,]+)"
    r"(?:,\s*seconded\s+by\s+Supervisor\s+(?P<seconder>[^)]+))?",
    re.I,
)

_AYES_RE = re.compile(
    r"Ayes:\s*(?P<ayes>.*?)(?:\s*(?:Nays?|Recused):|\s*Absent:|\s*$)",
    re.I,
)

_NAYS_RE = re.compile(
    r"Nays?:\s*(?P<nays>.*?)(?:\s*(?:Nays?|Recused):|\s*Absent:|\s*$)",
    re.I,
)

_ABSENT_RE = re.compile(
    r"Absent:\s*(?P<absent>.*?)(?:\.|$)",
    re.I,
)


def _parse_motion(text: str) -> dict | None:
    """Extract motion_type, mover, seconder from motion text."""
    m = _MOTION_RE.search(text)
    if not m:
        return None
    return {
        "motion_type": m.group("motion").strip(),
        "mover": m.group("mover").strip(),
        "seconder": (m.group("seconder") or "").strip(),
        "raw_text": text.strip(),
    }


_BAD_NAME_KEYWORDS = re.compile(
    r"(?:Nays?|Ayes|Recused|Absent|Abstain|Approve|Motion|Setting|Agenda|Road|Flood|Regular|Configuración|Audiencias|Planificación|Distrito|Control|Inundaciones):?",
    re.I,
)


def _parse_names(text: str) -> list[str]:
    """Split comma-separated name list, stripping whitespace and periods.

    Filters out fragments containing vote/agenda keywords that shouldn't
    be part of a supervisor name (e.g. "Bill GatesNays: Debbie Lesko").
    """
    names = []
    for part in text.split(","):
        name = part.strip().rstrip(".")
        if not name:
            continue
        if name.lower().startswith("and "):
            name = re.sub(r"^and\s+", "", name, flags=re.I).rstrip(".")
        # Reject names containing vote/agenda keywords
        if _BAD_NAME_KEYWORDS.search(name):
            continue
        names.append(name)
    return names


def _parse_supervisors(member_table_text: str) -> list[dict]:
    """Extract supervisor list from the first content TABLE (member list).

    The member TABLE contains entries like:
      "Board MembersClint Hickman, Chairman, District 4Jack Sellers, ..."
    where names run together without spaces (Aspose.Words rendering).

    For non-Formal meetings the TABLE may start with "Informal Meeting
    Summary..." or "Special Meeting Summary..." before the Board Members
    section.  We locate "Board Members" and start from there.

    Returns [{name, normalized_name, district, role}] or empty list.
    """
    if not member_table_text:
        return []

    # Normalize \xa0 and collapse whitespace
    member_table_text = member_table_text.replace("\xa0", " ").strip()

    # Find "Board Members" anywhere (may be after meeting type header)
    bm_match = re.search(r"Board\s+Members\s*", member_table_text, re.I)
    if bm_match:
        # Start from after "Board Members"
        member_table_text = member_table_text[bm_match.end():]

    # Split on "District N" to separate entries
    parts = re.split(r"(District\s+\d+)\s*", member_table_text)

    # parts: [..., "Name, Title,", "District N", "Name", "District N", ...]
    # Each supervisor occupies two consecutive parts (name-and-title, "District N")
    supervisors = []
    i = 0
    while i + 1 < len(parts):
        segment = parts[i].strip()
        district_str = parts[i + 1]

        # Skip if segment doesn't look like a name block
        if not segment or not segment[0].isupper():
            i += 2
            continue

        dist_m = re.search(r"(\d+)", district_str)
        district = int(dist_m.group(1)) if dist_m else None

        # Split on commas to isolate name from title/role
        tokens = [t.strip() for t in segment.split(",")]
        name_candidate = tokens[0] if tokens else ""
        title = tokens[1] if len(tokens) > 1 else ""

        # Validate name: at least two alphabetic words, starts with capital
        if not re.match(r"^[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+$", name_candidate):
            i += 2
            continue

        # Determine role
        role = "member"
        title_lower = title.lower()
        if "chair" in title_lower and "vice" not in title_lower:
            role = "chair"
        elif "vice" in title_lower or "vice chair" in title_lower:
            role = "vice_chair"

        supervisors.append({
            "name": name_candidate,
            "normalized_name": re.sub(r"[^a-z0-9]+", " ", name_candidate.lower()).strip(),
            "district": district,
            "role": role,
            "present": True,
        })
        i += 2

    # Deduplicate by normalized_name (keep first occurrence)
    seen = set()
    unique = []
    for s in supervisors:
        if s["normalized_name"] not in seen:
            seen.add(s["normalized_name"])
            unique.append(s)

    return unique


# ── main extraction ──────────────────────────────────────────────────

async def extract_votes_from_summary_dom(
    page: Page,
    source_url: str,
    agenda_items: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Extract votes from a meeting summary page using DOM structure.

    Args:
        page: Playwright page object
        source_url: Summary page URL (doctype=3)
        agenda_items: List of agenda item dicts (for C-number matching)

    Returns:
        (supervisors, votes) — same format as extract_votes_from_summary()
    """
    await page.goto(source_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Wait for #agendaView
    try:
        await page.wait_for_function(
            """() => {
                const av = document.getElementById("agendaView");
                return av && av.textContent && av.textContent.length > 100;
            }""",
            timeout=15000,
        )
    except Exception:
        pass

    content_div = await _get_content_div(page)
    if content_div is None:
        return [], []

    # Guard: the handle may point to null (empty agendaView divs)
    try:
        children = await _get_children_tags(content_div)
    except Exception:
        return [], []
    if not children:
        return [], []

    # --- Supervisors (from first content TABLE) ---
    member_table_text = ""
    for c in children:
        if c["tag"] == "TABLE":
            member_table_text = c["text"]
            break
    supervisors = _parse_supervisors(member_table_text)
    known_names = {s["name"] for s in supervisors}

    def is_known(name: str) -> bool:
        for kn in known_names:
            if kn.lower() in name.lower() or name.lower() in kn.lower():
                return True
        return False

    # --- Build C-number lookup ---
    item_cnumber: dict[int, str] = {}
    for item in agenda_items:
        num = item.get("agenda_item_number")
        cn = item.get("c_number", "")
        if num is not None:
            item_cnumber[int(num)] = cn or ""

    # --- Build blocks & groups ---
    blocks = _build_blocks(children)
    groups = _group_item_blocks(blocks)

    # --- Extract votes from each item group ---
    votes: list[dict] = []
    agenda_item_counter = 0

    for group in groups:
        # Only process item groups
        if not isinstance(group, dict) or "item" not in group:
            continue

        item_blk = group["item"]
        item_num = item_blk["item_number"]
        if item_num is None:
            continue

        c_number = item_cnumber.get(item_num, "")

        # Process each sub-item (most items have exactly 1)
        for sub in group.get("sub_items", []):
            if not sub["motion"] and not sub["ayes"]:
                continue

            motion_blk = sub["motion"]
            ayes_blks = sub["ayes"]

            # Combine all ayes blocks text for parsing
            ayes_text = ""
            for ab in ayes_blks:
                ayes_text += " " + ab["trimmed"].replace("\xa0", " ")
            ayes_text = ayes_text.strip()
            # Collapse multiple spaces from \xa0 normalization
            ayes_text = re.sub(r"\s{2,}", " ", ayes_text)

            # --- Parse motion ---
            motion_result = None
            mover = ""
            seconder = ""
            motion_text = ""

            if motion_blk:
                parsed = _parse_motion(motion_blk["trimmed"])
                if parsed:
                    motion_result = parsed["motion_type"]
                    mover = parsed["mover"]
                    seconder = parsed["seconder"]
                    motion_text = parsed["raw_text"]

            # --- Parse votes ---
            supervisor_votes: list[dict] = []

            if ayes_text:
                # Parse Ayes
                am = _AYES_RE.search(ayes_text)
                ayes = am.group("ayes").strip() if am else ""
                ayes_names = _parse_names(ayes) if ayes else []

                # Parse Nays
                nm = _NAYS_RE.search(ayes_text)
                nays = nm.group("nays").strip() if nm else ""
                nays_names = _parse_names(nays) if nays else []

                # Parse Absent
                abs_m = _ABSENT_RE.search(ayes_text)
                absent = abs_m.group("absent").strip() if abs_m else ""
                absent_names = _parse_names(absent) if absent else []

                for name in ayes_names:
                    if is_known(name):
                        supervisor_votes.append({"name": name, "vote": "yes"})
                for name in nays_names:
                    if is_known(name):
                        supervisor_votes.append({"name": name, "vote": "no"})
                for name in absent_names:
                    if is_known(name):
                        supervisor_votes.append({"name": name, "vote": "absent"})

            # If we have votes, record them — even if no motion text
            if supervisor_votes or motion_result:
                agenda_item_counter += 1
                votes.append({
                    "agenda_item_number": item_num,
                    "agenda_item_counter": agenda_item_counter,
                    "c_number": c_number,
                    "motion_result": motion_result or "approved",
                    "vote_text": ayes_text,
                    "motion_text": motion_text,
                    "supervisor_votes": supervisor_votes,
                })

    return supervisors, votes
