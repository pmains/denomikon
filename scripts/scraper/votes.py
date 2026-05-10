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

    # Build item boundaries using the full text (may be single-line)
    # Use character positions for splitting, since all items may share one line.
    full_text = "\n".join(lines) if len(lines) > 1 else lines[0] if lines else ""
    
    item_boundaries: list[tuple[int, str, str]] = []
    seen_nums: set[int] = set()
    valid_item_nums = {int(a.get("agenda_item_number", 0)) for a in agenda_items if a.get("agenda_item_number")}
    for m in re.finditer(r"(?:^|[^\w\'\u2019\u2018])(\d{1,3})\.(?=\s*[A-Z0-9])", full_text):
        num = m.group(1)
        num_int = int(num)
        if num_int in seen_nums:
            continue
        # Filter spurious matches (dollar amounts, subsection numbers) early
        if valid_item_nums and num_int not in valid_item_nums:
            continue
        if len(num) > 3 and num not in item_cnumber_map:
            continue
        # Post-filter: reject spurious boundary matches
        # Check 1: decimal numbers like "0,72.50" (char before separator is digit)
        if m.start() > 0 and full_text[m.start() - 1].isdigit():
            continue
        # Check 2: percentages like " 27.66%" (2 digits then '%' after dot)
        if len(full_text) > m.end() + 2:
            after_dot = full_text[m.end():m.end()+3]
            if re.match(r'\d{2}%', after_dot):
                continue
        # Check 3: condition sub-numbers in PZ consent text, where text after
        # the dot is a space (" 7. Drainage" vs real " 7.OFF 17 NORTH").
        if len(full_text) > m.end() and full_text[m.end()] == ' ':
            continue
        seen_nums.add(num_int)
        pos = m.start()
        rest = full_text[pos:].lstrip()
        rest = re.sub(r"^\d+\.\s*", "", rest)
        c_m = re.search(r"\(([A-Z]-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3}){1,3})\)", rest)
        c_num = c_m.group(1) if c_m else item_cnumber_map.get(num, "")
        item_boundaries.append((pos, num, c_num))

    # Use a counter for unique agenda_item_id within this batch
    agenda_item_counter = 0

    # Parse each item's section
    valid_item_nums = {int(a.get("agenda_item_number", 0)) for a in agenda_items if a.get("agenda_item_number")}
    for idx, (start_pos, item_num, c_num) in enumerate(item_boundaries):
        num = int(item_num)
        # Skip items that aren't in the known agenda_items range (false positives from regex)
        if valid_item_nums and num not in valid_item_nums:
            continue
        end_pos = item_boundaries[idx + 1][0] if idx + 1 < len(item_boundaries) else len(full_text)
        section_text = full_text[start_pos:end_pos].strip()
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
                "vote_text": section_text,
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
                "vote_text": section_text,
                "supervisor_votes": supervisor_votes,
            })

    return supervisors, votes


# ────────────────────────────────────────────────────────────────────────
# Synchronous vote analysis helpers (no Playwright dependency)
# ────────────────────────────────────────────────────────────────────────


def detect_split_vote(supervisor_votes: list[dict]) -> dict:
    """Analyze a list of supervisor vote dicts and return vote attributes.

    Returns a dict with:
        is_split_vote (bool): True if ayes and nays both present
        unanimous (bool): True if all substantive votes are the same
        majority_position (str): "yes"|"no"|"tie"|"unknown"
        dissenters (list[str]): names of members voting against the majority

    Only "yes" and "no" votes are considered substantive.
    "abstain", "recused", "absent", "not_voting" are excluded.
    """
    substantive = [
        sv for sv in supervisor_votes
        if sv.get("vote") in ("yes", "no")
    ]
    if not substantive:
        return {
            "is_split_vote": False,
            "unanimous": None,
            "majority_position": "unknown",
            "dissenters": [],
        }

    vote_set = {sv["vote"] for sv in substantive}
    is_split = len(vote_set) > 1
    unanimous = len(vote_set) == 1

    yes_count = sum(1 for sv in substantive if sv["vote"] == "yes")
    no_count = sum(1 for sv in substantive if sv["vote"] == "no")

    if yes_count > no_count:
        majority = "yes"
    elif no_count > yes_count:
        majority = "no"
    else:
        majority = "tie"

    dissenters = []
    if majority not in ("tie", "unknown"):
        for sv in substantive:
            if sv["vote"] != majority:
                name = sv.get("name", "") or sv.get("normalized_name", "")
                if name:
                    dissenters.append(name)

    return {
        "is_split_vote": is_split,
        "unanimous": unanimous,
        "majority_position": majority,
        "dissenters": dissenters,
    }


def flag_dissent_in_votes(votes: list[dict]) -> list[dict]:
    """Flag dissenting members in each vote's supervisor_votes sub-list.

    Mutates each vote dict's supervisor_votes entries in-place, adding
    'is_dissent': True/False to each supervisor_vote sub-dict.

    Returns the same votes list for convenience.
    """
    for vote in votes:
        sv_list = vote.get("supervisor_votes", [])
        if not sv_list:
            continue
        analysis = detect_split_vote(sv_list)
        majority = analysis["majority_position"]
        if majority in ("tie", "unknown"):
            continue
        for sv in sv_list:
            if sv.get("vote") in ("yes", "no") and sv["vote"] != majority:
                sv["is_dissent"] = True
            else:
                sv.setdefault("is_dissent", False)
    return votes


def infer_absence(
    known_members: list[dict],
    vote_records_with_members: list[list[dict]],
) -> list[dict]:
    """Infer absences when known members never vote in a meeting.

    Args:
        known_members: List of dicts with 'normalized_name' key for all
                       expected participants of a meeting.
        vote_records_with_members: List of lists, each inner list being the
                                   supervisor_votes for one agenda item.

    Returns:
        List of dicts:
            {"normalized_name": str, "attendance_status": "inferred_absent",
             "inference_method": "other_members_voted_but_member_did_not"}

    IMPORTANT: Never present inferred absence as confirmed absence.
    """
    # Collect all members who cast a vote in ANY item
    all_voted_normalized: set[str] = set()
    for sv_list in vote_records_with_members:
        for sv in sv_list:
            nrm = sv.get("normalized_name", "")
            if nrm:
                all_voted_normalized.add(nrm)

    absent: list[dict] = []
    for member in known_members:
        nrm = member.get("normalized_name", "")
        if not nrm:
            continue
        if nrm not in all_voted_normalized:
            absent.append({
                "normalized_name": nrm,
                "name": member.get("name", ""),
                "attendance_status": "inferred_absent",
                "inference_method": "other_members_voted_but_member_did_not",
            })
    return absent


def classify_vote_result(votes: list[dict]) -> dict:
    """Classify a meeting's vote results.

    Returns aggregate stats:
        total_items: int
        split_items: int
        unanimous_items: int
        items_with_dissent: int
        items_with_no_substantive_votes: int
    """
    total = len(votes)
    split_count = 0
    unanimous_count = 0
    dissent_count = 0
    no_vote_count = 0

    for vote in votes:
        sv_list = vote.get("supervisor_votes", [])
        if not sv_list:
            no_vote_count += 1
            continue
        analysis = detect_split_vote(sv_list)
        if analysis["unanimous"]:
            unanimous_count += 1
        elif analysis["is_split_vote"]:
            split_count += 1
        if analysis["dissenters"]:
            dissent_count += 1
        if analysis["majority_position"] == "unknown":
            no_vote_count += 1

    return {
        "total_items": total,
        "split_items": split_count,
        "unanimous_items": unanimous_count,
        "items_with_dissent": dissent_count,
        "items_with_no_substantive_votes": no_vote_count,
    }


# ────────────────────────────────────────────────────────────────────────
# Executive session participant extraction
# ────────────────────────────────────────────────────────────────────────


_KNOWN_EXECUTIVE_ADVISORS: dict[str, dict] = {
    # Pattern: normalized_name -> (raw_name, role, organization, participation_type)
    # Known from previous BOS Executive session data
    "kory langhofer": ("Kory Langhofer", "Outside Counsel", "Brownstein Hyatt Farber Schreck", "outside_counsel"),
    "kim miles": ("Kim Miles", "Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "justin ryan": ("Justin Ryan", "Senior Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "justin a. ryan": ("Justin A. Ryan", "Senior Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "justin andrew ryan": ("Justin Andrew Ryan", "Senior Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "john doe": ("John Doe", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "laura wright": ("Laura Wright", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "laura l. wright": ("Laura L. Wright", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "raymond sloan": ("Raymond Sloan", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "ray sloan": ("Ray Sloan", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "jennifer cox": ("Jennifer Cox", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "michelle willett": ("Michelle Willett", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "james johnson": ("James Johnson", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "james n. johnson": ("James N. Johnson", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "thomas hamilton": ("Thomas Hamilton", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "john k. davis": ("John K. Davis", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "josh d. hook": ("Josh D. Hook", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "josh hook": ("Josh Hook", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "william m. ring": ("William M. Ring", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "william ring": ("William Ring", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "bill ring": ("Bill Ring", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "michael k. goodwin": ("Michael K. Goodwin", "Outside Counsel", "Goodwin & Associates", "outside_counsel"),
    "michael goodwin": ("Michael Goodwin", "Outside Counsel", "Goodwin & Associates", "outside_counsel"),
    "thomas holmes": ("Thomas Holmes", "Outside Counsel", "Holmes & Associates", "outside_counsel"),
    "jeffrey sanders": ("Jeffrey Sanders", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "christina golden": ("Christina Golden", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "stephen reed": ("Stephen Reed", "Deputy County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "rebecca mueller": ("Rebecca Mueller", "Assistant County Attorney", "Maricopa County Attorney's Office", "legal_counsel"),
    "mark richardson": ("Mark Richardson", "Chief of Staff", "Board of Supervisors", "staff"),
    "jerry johnson": ("Jerry Johnson", "County Manager", "Maricopa County", "staff"),
    "david cook": ("David Cook", "County Manager", "Maricopa County", "staff"),
    "kathleen o'neill": ("Kathleen O'Neill", "Deputy County Manager", "Maricopa County", "staff"),
    "jennifer larson": ("Jennifer Larson", "Chief Financial Officer", "Maricopa County", "staff"),
}

# Pattern: (raw_label_string, participation_type) for common role indicators
_EXECUTIVE_ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"Attorney(?: for| of)?:?", "legal_counsel"),
    (r"Counsel(?: for| of)?:?", "legal_counsel"),
    (r"Legal Counsel", "legal_counsel"),
    (r"Assistant County Attorney", "legal_counsel"),
    (r"Deputy County Attorney", "legal_counsel"),
    (r"Outside Counsel", "outside_counsel"),
    (r"Special Counsel", "outside_counsel"),
    (r"Present(?:ed)? (?:by|By):?", "presented"),
    (r"Presenting", "presented"),
    (r"Staff(?: Advisor)?:?", "staff"),
    (r"Advisor:?", "advised"),
    (r"Advising", "advised"),
    (r"County Manager", "staff"),
    (r"Deputy County Manager", "staff"),
    (r"Chief of Staff", "staff"),
]


def extract_executive_session_participants(
    agenda_items: list[dict],
    meeting_id: str,
    source_url: str = "",
    body: str = "bos",
) -> list[dict]:
    """Extract executive session participants from BOS Executive meeting agenda items.

    Scans all agenda items for known advisor names and role patterns.
    Applies a known-name lookup for frequently-encountered attorneys and staff,
    then falls back to regex-based extraction for unknown names.

    Args:
        agenda_items: List of agenda item dicts with "agenda_item_text" and
                     "agenda_item_number" keys.
        meeting_id: The meeting ID.
        source_url: The meeting source URL.
        body: Body identifier (default "bos").

    Returns:
        List of dicts suitable for inserting into executive_session_participants.
    """
    participants: list[dict] = []
    seen: set[str] = set()

    for item in agenda_items:
        item_num = item.get("agenda_item_number")
        text = item.get("agenda_item_text", "") or item.get("agenda_item_title", "") or ""
        if not text:
            continue

        item_participants = _extract_participants_from_text(
            text, item_num, source_url
        )
        for p in item_participants:
            # Deduplicate by normalized_name within this meeting
            norm = p["normalized_name"]
            key = f"{norm}:{item_num or ''}"
            if key not in seen:
                seen.add(key)
                participants.append({
                    "body": body,
                    "meeting_id": meeting_id,
                    "person_name": p["person_name"],
                    "normalized_name": norm,
                    "role_or_title": p.get("role_or_title"),
                    "organization": p.get("organization"),
                    "participation_type": p.get("participation_type", "unknown"),
                    "agenda_item_number": item_num,
                    "source_text": p.get("source_text", "")[:500],
                    "source_url": source_url,
                })

    return participants


def _extract_participants_from_text(
    text: str,
    item_number: int | None = None,
    source_url: str = "",
) -> list[dict]:
    """Extract participants from a single block of text."""
    participants: list[dict] = []
    seen_names: set[str] = set()

    # First pass: match known advisors by name
    for norm, (raw_name, role, org, ptype) in _KNOWN_EXECUTIVE_ADVISORS.items():
        # Check if the name appears in the text
        for variant in [raw_name, norm.title(), norm]:
            pattern = re.escape(variant)
            m = re.search(pattern, text, re.I)
            if m:
                if norm not in seen_names:
                    seen_names.add(norm)
                    context_start = max(0, m.start() - 60)
                    context_end = min(len(text), m.end() + 60)
                    context = text[context_start:context_end].strip()
                    participants.append({
                        "person_name": raw_name,
                        "normalized_name": norm,
                        "role_or_title": role,
                        "organization": org,
                        "participation_type": ptype,
                        "source_text": context,
                    })
                break

    # Second pass: look for role-labeled names like "Attorney: John Smith"
    # Only capture names NOT already found in the first pass
    # Uses word-by-word extraction to avoid regex greedy-matching issues
    # that cause the pattern to consume text past name boundaries.
    #
    # A "name candidate" is a sequence of words where each word starts
    # with an uppercase letter followed by lowercase letters.
    # The name must be preceded by a role pattern.

    # Build a set of positions already covered by known-name extraction
    covered_ranges: list[tuple[int, int]] = []
    for p in participants:
        norm = p["normalized_name"]
        idx = text.lower().find(norm)
        if idx >= 0:
            covered_ranges.append((idx, idx + len(norm)))

    def _is_covered(pos: int) -> bool:
        for s, e in covered_ranges:
            if s <= pos < e:
                return True
        return False

    _name_word_re = re.compile(r"[A-Z][a-z']+")
    _role_label = re.compile(
        r"(" + "|".join(pattern for pattern, _ in _EXECUTIVE_ROLE_PATTERNS) + r")",
        re.I
    )

    # Find all words that could be name words
    all_matches = [(m.start(), m.end(), m.group()) for m in _name_word_re.finditer(text)]

    # Check each word as a potential name start
    for i in range(len(all_matches)):
        word_start, word_end, word = all_matches[i]

        # Skip if this word is inside an already-covered range (known name match)
        if _is_covered(word_start):
            continue

        # Collect consecutive uppercase-starting words
        name_span_end = word_end
        j = i + 1
        while j < len(all_matches):
            between = text[name_span_end:all_matches[j][0]]
            if re.match(r"^\s+$", between):
                name_span_end = all_matches[j][1]
                j += 1
            else:
                break

        name_text = text[word_start:name_span_end].strip()
        if not name_text:
            continue
        norm = re.sub(r"[^a-z0-9]+", " ", name_text.lower()).strip()
        if not norm or len(norm) < 5 or norm in seen_names:
            continue

        # Check if a role label appears within ~50 chars before this name
        search_start = max(0, word_start - 50)
        before_text = text[search_start:word_start]
        role_match = _role_label.search(before_text, re.I)
        if not role_match:
            continue

        # Found a role-labeled name
        role_label = role_match.group(0).strip()
        seen_names.add(norm)

        ptype = "unknown"
        for pat, pt in _EXECUTIVE_ROLE_PATTERNS:
            if re.search(pat, role_label, re.I):
                ptype = pt
                break

        if norm in _KNOWN_EXECUTIVE_ADVISORS:
            raw_name, role, org, known_ptype = _KNOWN_EXECUTIVE_ADVISORS[norm]
            participants.append({
                "person_name": raw_name,
                "normalized_name": norm,
                "role_or_title": role,
                "organization": org,
                "participation_type": known_ptype,
                "source_text": text[search_start:name_span_end].strip()[:200],
            })
        else:
            participants.append({
                "person_name": name_text,
                "normalized_name": norm,
                "role_or_title": role_label,
                "organization": None,
                "participation_type": ptype,
                "source_text": text[search_start:name_span_end].strip()[:200],
            })

    return participants


