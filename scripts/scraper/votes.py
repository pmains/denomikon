from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path

async def extract_votes_from_summary(page, source_url: str, agenda_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Extract vote results from a meeting summary page (doctype=3).

    Visits the summary URL, waits for #agendaView to load, extracts the
    text content, and parses it to find:
    - Supervisors present (with district info)
    - For each agenda item: motion text, Ayes, Nays, and withdrawn status

    The summary page uses \xa0 (non-breaking space) as the item separator
    instead of newlines, so parsing works on the single-line text via regex.

    Args:
        page: Playwright page object
        source_url: The summary URL (doctype=3)
        agenda_items: List of parsed agenda item dicts (for matching C-numbers)

    Returns:
        (supervisors, votes) where:
        - supervisors: [{"name": ..., "normalized_name": ..., "district": ..., "role": ...}]
        - votes: [{"agenda_item_number": ..., "c_number": ..., "motion_result": ...,
                   "vote_text": ..., "supervisor_votes": [{"name": ..., "vote": ...}]}]
    """
    await page.goto(source_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Wait for #agendaView to be populated
    try:
        await page.wait_for_function(
            """() => {
                const av = document.getElementById('agendaView');
                return av && av.textContent && av.textContent.length > 100;
            }""",
            timeout=15000,
        )
    except Exception:
        pass

    # Get the text content of the agenda view
    text = await page.evaluate(
        """() => {
            const av = document.getElementById('agendaView');
            return av ? av.textContent || '' : '';
        }"""
    )

    if not text or len(text.strip()) < 50:
        return [], []

    # Normalize: collapse \xa0 to regular spaces so we can split and match
    # The summary uses \xa0 between items
    text_normalized = text.replace("\xa0", " ")
    # Also collapse multiple spaces
    text_normalized = re.sub(r"\s{3,}", "\n\n", text_normalized)

    # --- Parse supervisors present ---
    supervisors: list[dict] = []
    # Extract everything between "with the following members present:" and ". Also present:"
    sup_match = re.search(
        r"with the following members present:\s+(.*?)\.\s*Also present",
        text_normalized,
        re.I | re.DOTALL,
    )
    if not sup_match:
        # Try alternative ending: just before a numbered item
        sup_match = re.search(
            r"with the following members present:\s+(.*?)(?=\d+\.)",
            text_normalized,
            re.I | re.DOTALL,
        )
    if sup_match:
        sup_text = sup_match.group(1)
        # Split by semicolons
        for part in re.split(r";\s*", sup_text):
            part = part.strip().rstrip(";,.")
            if not part:
                continue
            # Remove parenthetical comments like "(entered the meeting late)"
            part = re.sub(r"\s*\([^)]*\)", "", part).strip()
            # Match "Thomas Galvin, Chairman, District 2" or "Thomas Galvin, District 2"
            m = re.match(
                r"([A-Za-z]+(?:\s+[A-Za-z']+)+)"
                r"(?:,\s*(?:Chairman|Vice Chair|Supervisor))?"
                r"(?:,\s*District\s+(\d+))?",
                part,
                re.I,
            )
            if m:
                name = m.group(1).strip()
                district = m.group(2)
                role = ""
                if re.search(r"Chairman\b", part, re.I) and not re.search(r"Vice", part, re.I):
                    role = "Chairman"
                elif re.search(r"Vice Chair", part, re.I):
                    role = "Vice Chair"
                if name:
                    supervisors.append({
                        "name": name,
                        "normalized_name": re.sub(r"[^a-z0-9]+", " ", name.lower()).strip(),
                        "district": district,
                        "role": role if role else None,
                        "present": True,
                    })

    # --- Build a lookup from item number to C-number from agenda_items ---
    item_cnumber_map: dict[str, str] = {}
    for item in agenda_items:
        num = str(item.get("agenda_item_number", ""))
        c = item.get("c_number", "") or ""
        if num and c:
            item_cnumber_map[num] = c

    # --- Parse votes for each agenda item ---
    votes: list[dict] = []

    # Split the text into sections by numbered items
    # The text is run-together like "...5.SUN BASIN..." with non-breaking
    # spaces or double spaces as separators
    lines = text_normalized.split("\n")

    # Find all line positions where a numbered item starts.
    # Use finditer to catch multiple items per line (common when normalization
    # doesn't split them onto their own line).
    item_boundaries: list[tuple[int, int, str, str]] = []
    for i, line in enumerate(lines):
        for m in re.finditer(r"(?:^|\D)(\d{1,3})\.\s*([A-Z])", line):
            num = m.group(1)
            # Skip numbers that look like dates or other non-items
            if len(num) > 3 and num not in item_cnumber_map:
                continue
            # Use character position within the line for section ordering
            pos = m.start()
            rest = line[pos:].lstrip()
            rest = re.sub(r"^\d+\.\s*", "", rest)
            c_m = re.search(r"\(([A-Z]-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3}){1,3})\)", rest)
            c_num = c_m.group(1) if c_m else item_cnumber_map.get(num, "")
            item_boundaries.append((i, pos, num, c_num))

    # Use a counter for unique agenda_item_id within this batch
    agenda_item_counter = 0

    # Parse each item's section for vote information
    for idx, (start_line, _start_pos, item_num, c_num) in enumerate(item_boundaries):
        end_line = item_boundaries[idx + 1][0] if idx + 1 < len(item_boundaries) else len(lines)
        section_lines = lines[start_line:end_line]
        section_text = " ".join(line.strip() for line in section_lines)
        section_text = re.sub(r"\s+", " ", section_text).strip()

        # Check for "withdrawn" 
        if re.search(r"\bwithdrawn\b", section_text, re.I):
            agenda_item_counter += 1
            votes.append({
                "agenda_item_id": agenda_item_counter,
                "agenda_item_number": int(item_num),
                "c_number": c_num if c_num else None,
                "c_number_base": c_num[:-4] if c_num and len(c_num) > 4 else c_num if c_num else None,
                "motion_result": "withdrawn",
                "vote_text": section_text[:2000],
                "supervisor_votes": [],
            })
            continue

        # Find motion line
        motion_match = re.search(
            r"Motion to (\w+)[^.]*?(?:by Supervisor ([^,]+),\s*seconded by Supervisor ([^)]+))",
            section_text,
            re.I,
        )

        # Build a set of known supervisor normalized names for filtering
        known_supervisor_names = {s["normalized_name"] for s in supervisors}
        # Also match partial names (first + last)
        def is_known_supervisor(name: str) -> bool:
            """Check if a name matches a known supervisor."""
            normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            if normalized in known_supervisor_names:
                return True
            # Check partial matches: if the name starts with a known supervisor's name
            for known in known_supervisor_names:
                if normalized.startswith(known) or known.startswith(normalized):
                    return True
            return False

        # Find Ayes - capture names and stop before text that isn't a name
        ayes: list[str] = []
        ayes_match = re.search(r"Ayes:\s*(.*?)(?:\s*Nay:|\s*$)", section_text, re.I)
        if ayes_match:
            raw = ayes_match.group(1).strip()
            # Only keep entries that look like names (shorter than 60 chars, start with capital letter)
            candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
            for c in candidates:
                # Clean up: trim trailing content after a Spanish translation marker "-"
                # Supervisors don't have " - " in their names
                c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
                # Remove trailing Spanish section names (ALL CAPS)
                c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
                # Remove trailing text after role markers
                c = re.sub(r"\s+(County|Human|Public|Parks|Transportation|Elections|Risk|Finance|Real Estate|Library|Planning).*$", "", c, flags=re.I).strip()
                # Remove trailing text after specific section names
                c = re.sub(r"\s+STATUTORY.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+AUDIENCIAS.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+BOARD.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+CALL TO.*$", "", c, flags=re.I).strip()
                c = re.sub(r"\s+LIBRARY.*$", "", c, flags=re.I).strip()
                c = c.rstrip(",;.:").strip()
                if not c or len(c) < 3 or len(c) > 60:
                    continue
                if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                    continue
                if re.search(r"\b(with|and|the|for|of|that|this|from|please|email|prior|local|fire|written|except|amenos|como|que|del|para|una|los|las|por|notado)", c, re.I):
                    continue
                if c not in ayes:
                    ayes.append(c)

        # Filter ayes against known supervisors when we have them
        if known_supervisor_names and len(ayes) > len(supervisors):
            filtered = [n for n in ayes if is_known_supervisor(n)]
            if filtered:
                ayes = filtered

        # Find Nays - same approach
        nays: list[str] = []
        nays_match = re.search(r"Nay:\s*(.*?)(?:\s+(?=\d+\.)|\s*$)", section_text, re.I)
        if nays_match:
            raw = nays_match.group(1).strip()
            candidates = [n.strip() for n in re.split(r"[,\n]+", raw) if n.strip()]
            for c in candidates:
                c = re.sub(r"\s*-\s*[A-Z].*$", "", c).strip()
                c = re.sub(r"\s+[A-ZÁÉÍÓÚÑ\s]{10,}$", "", c).strip()
                c = c.rstrip(",;.:").strip()
                if not c or len(c) < 3 or len(c) > 60:
                    continue
                if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑ'][A-Za-zÁÉÍÓÚÜÑ'\s\.-]+$", c):
                    continue
                if c not in nays:
                    nays.append(c)

            # Filter nays against known supervisors
            if known_supervisor_names:
                filtered = [n for n in nays if is_known_supervisor(n)]
                if filtered:
                    nays = filtered

        # Determine motion_result
        motion_result = ""
        if motion_match:
            action = motion_match.group(1).lower()
            if action in ("approve", "adopt", "concur"):
                motion_result = "approved"
            elif action in ("deny", "denied"):
                motion_result = "denied"
            elif action == "continue":
                motion_result = "continued"
            else:
                motion_result = action
        elif ayes and not nays:
            motion_result = "approved"
        elif nays:
            motion_result = "carried"

        # Build a lookup: normalized_name → canonical supervisor name/role
        known_supervisor_lookup: dict[str, dict] = {
            s["normalized_name"]: s for s in supervisors
        }

        supervisor_votes: list[dict] = []

        def find_canonical_name(raw_name: str) -> str:
            """Match a raw extracted name to its canonical supervisor name."""
            cleaned = re.sub(r"^Supervisor\s+|^Vice Chair\s+|^Chairman\s+", "", raw_name, flags=re.I).strip()
            normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
            # Direct match
            if normalized in known_supervisor_lookup:
                return known_supervisor_lookup[normalized]["name"]
            # Partial match
            for kn, kd in known_supervisor_lookup.items():
                if normalized.startswith(kn) or kn.startswith(normalized):
                    return kd["name"]
            return cleaned

        # Build ayes as "yes" votes
        for name in ayes:
            canonical = find_canonical_name(name)
            if canonical and not re.match(r"^(\d+|none|and)$", canonical, re.I):
                nrm = re.sub(r"[^a-z0-9]+", " ", canonical.lower()).strip()
                supervisor_votes.append({
                    "name": canonical,
                    "normalized_name": nrm,
                    "vote": "yes",
                    "raw_vote_text": "Ayes",
                })

        # Build nays as "no" votes
        for name in nays:
            canonical = find_canonical_name(name)
            if canonical and not re.match(r"^(\d+|none|and)$", canonical, re.I):
                nrm = re.sub(r"[^a-z0-9]+", " ", canonical.lower()).strip()
                supervisor_votes.append({
                    "name": canonical,
                    "normalized_name": nrm,
                    "vote": "no",
                    "raw_vote_text": "Nay",
                })

        # Deduplicate supervisor_votes by normalized_name
        seen_sup: set[str] = set()
        deduped_sv: list[dict] = []
        for sv in supervisor_votes:
            key = sv.get("normalized_name", sv.get("name", "").lower())
            if key not in seen_sup:
                seen_sup.add(key)
                deduped_sv.append(sv)
        supervisor_votes = deduped_sv

        if motion_result or supervisor_votes:
            agenda_item_counter += 1
            votes.append({
                "agenda_item_id": agenda_item_counter,
                "agenda_item_number": int(item_num),
                "c_number": c_num if c_num else None,
                "c_number_base": c_num[:-4] if c_num and len(c_num) > 4 else c_num if c_num else None,
                "motion_result": motion_result or "unknown",
                "vote_text": section_text[:2000],
                "supervisor_votes": supervisor_votes,
            })

    return supervisors, votes


