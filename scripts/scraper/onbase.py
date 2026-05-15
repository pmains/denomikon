"""
OnBase Agenda Online — platform adapter for Hyland OnBase instances.

Provides a reusable client that works across any OnBase Agenda Online
instance (Maricopa County BOS, City of Tempe, Mesa, Gilbert, etc.)
with per-jurisdiction configuration for search method, token handling,
meeting type IDs, and URL patterns.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────

@dataclass
class OnBaseConfig:
    """Per-instance configuration for an OnBase Agenda Online server.

    Fields
    ------
    name : str
        Human-readable label (e.g. "Maricopa BOS", "Tempe City Council").
    host : str
        Hostname (e.g. ``"mccobagenda.databankcloud.com"``).
    base_path : str
        Path prefix shared across all endpoints (e.g. ``"/AgendaOnline"``).
    search_path : str
        Path for the search/meetings listing page.
        Combined with *base_path* (e.g. ``"/Meetings"`` → full path
        ``/AgendaOnline/Meetings``).
    search_method : str
        ``"GET"`` or ``"POST"``.
    search_params : dict
        Default query-string or form-body parameters included in every
        search request.
    csrf_required : bool
        Whether the search page requires a ``__RequestVerificationToken``.
    meeting_types : dict[str, list[int]]
        Mapping of public-body slug → list of OnBase meeting-type IDs.
        Example: ``{"tempe-city-council": [109, 101, 106, 102]}``
    public_body_code : str
        Default body code assigned to meetings discovered via this config.
    download_url_pat : str
        Python-format string for document download URLs.
        Available placeholders: ``{base}``, ``{meeting_id}``, ``{name}``,
        ``{doc_type}``.
    agenda_view_path : str
        Path template for the agenda-items HTML endpoint.
        Placeholder: ``{meeting_id}``.
    meeting_view_path : str
        Path template for the meeting detail page.
        Placeholder: ``{meeting_id}``.
    source_system : str
        Value for the meetings ``source_system`` column.
    source_instance_url : str
        Base URL for the OnBase instance (e.g.
        ``"https://tempe.hylandcloud.com/Agendaonline"``).
    """
    name: str
    host: str
    base_path: str
    search_path: str
    search_method: str = "GET"
    search_params: dict = field(default_factory=dict)
    csrf_required: bool = False
    meeting_types: dict[str, list[int]] = field(default_factory=dict)
    public_body_code: str = ""
    # Form field names (vary by instance)
    date_start_field: str = "StartDate"
    date_end_field: str = "EndDate"
    meeting_type_field: str = "MeetingTypeIDs"
    # Value for the DateRangeOptionID select when using custom dates
    date_range_custom_value: str = "1"
    download_url_pat: str = "{base}/Documents/Downloadfile/{name}.pdf?documentType={doc_type}&meetingId={meeting_id}"
    agenda_view_path: str = "/Meetings/ViewMeetingAgenda?meetingId={meeting_id}&type=agenda"
    meeting_view_path: str = "/Meetings/ViewMeeting?id={meeting_id}&doctype=1"
    source_system: str = "hyland_onbase_agenda_online"
    source_instance_url: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.host}{self.base_path}"

    def build_search_url(self, start_date: str, end_date: str,
                         meeting_type_ids: Optional[list[int]] = None) -> str:
        """Build the full search URL (for GET-based instances)."""
        params = dict(self.search_params)
        params.setdefault("Keywords", "")
        params["DateRangeOptionID"] = self.date_range_custom_value
        params[self.date_start_field] = start_date
        params[self.date_end_field] = end_date
        if meeting_type_ids:
            params[self.meeting_type_field] = ",".join(str(mid) for mid in meeting_type_ids)
        qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return f"{self.base_url}{self.search_path}?{qs}"

    def build_search_post_data(self, start_date: str, end_date: str,
                                token: str,
                                meeting_type_ids: Optional[list[int]] = None) -> dict[str, str]:
        """Build form-data dict for POST-based search (Tempe).

        Uses config field names for date inputs (e.g. ``DateRangeCustomStartDate``
        for Tempe, ``StartDate`` for other instances).
        """
        data = {
            "__RequestVerificationToken": token,
            "Keywords": "",
            "DateRangeOptionID": self.date_range_custom_value,
            self.date_start_field: start_date,
            self.date_end_field: end_date,
        }
        if meeting_type_ids:
            data[self.meeting_type_field] = ",".join(str(mid) for mid in meeting_type_ids)
        # Add alias date fields in case the server checks both
        data.setdefault("StartDate", start_date)
        data.setdefault("EndDate", end_date)
        return data

    def build_agenda_view_url(self, meeting_id: int) -> str:
        path = self.agenda_view_path.format(meeting_id=meeting_id)
        return f"{self.base_url}{path}"

    def build_meeting_view_url(self, meeting_id: int) -> str:
        path = self.meeting_view_path.format(meeting_id=meeting_id)
        return f"{self.base_url}{path}"

    def build_download_url(self, meeting_id: int, name: str, doc_type: int = 1) -> str:
        return self.download_url_pat.format(
            base=self.base_url,
            meeting_id=meeting_id,
            name=name,
            doc_type=doc_type,
        )

    def build_agenda_download_url(self, meeting_id: int, name: str) -> str:
        return self.build_download_url(meeting_id, name, doc_type=1)

    def build_packet_download_url(self, meeting_id: int, name: str) -> str:
        return self.build_download_url(meeting_id, name, doc_type=5) + "&isAttachment=True"


# ──────────────────────────────────────────────
#  Pre-built configs
# ──────────────────────────────────────────────

TEMPE_CONFIG = OnBaseConfig(
    name="Tempe",
    host="tempe.hylandcloud.com",
    base_path="/Agendaonline",
    search_path="/Meetings",
    search_method="POST",
    csrf_required=True,
    meeting_types={
        "tempe-city-council": [109, 101, 106, 102],
        "tempe-drc": [104, 105],
        "tempe-boa": [110, 111],
        "tempe-hpc": [112],
        "tempe-ha": [107],
        "tempe-rio": [108],
        "tempe-rmt": [113],
        "tempe-jrc": [114, 115],
    },
    public_body_code="tempe-cc",
    date_start_field="DateRangeCustomStartDate",
    date_end_field="DateRangeCustomEndDate",
    date_range_custom_value="11",
    source_instance_url="https://tempe.hylandcloud.com/Agendaonline",
)

MARICOPA_BOS_CONFIG = OnBaseConfig(
    name="Maricopa BOS",
    host="mccobagenda.databankcloud.com",
    base_path="/AgendaOnline",
    search_path="/Meetings/Search",
    search_method="GET",
    search_params={
        "dropid": "11",
        "dropsv": "",
        "dropev": "",
    },
    csrf_required=False,
    meeting_types={
        "board-of-supervisors": [11],
    },
    public_body_code="bos",
    source_instance_url="https://mccobagenda.databankcloud.com/AgendaOnline",
)


# ──────────────────────────────────────────────
#  Meeting listing parsing
# ──────────────────────────────────────────────

def parse_meetings_from_html(html: str, base_url: str,
                             public_body_code: str = "",
                             meeting_type_ids: Optional[list[int]] = None) -> list[dict]:
    """Parse OnBase meeting search results HTML into dicts.

    Handles both GET (Maricopa) and POST (Tempe) result formats.
    Returns a list of dicts with keys:
        meeting_id, meeting_date, meeting_time, meeting_title,
        meeting_type, body, agenda_url, summary_url, minutes_url,
        video_url, source_url
    """
    from scraper.html_utils import _parse_html, _find_all, _clean_html_text, _node_text

    root = _parse_html(html)
    meetings: list[dict] = []
    seen_ids: set[str] = set()

    # Find the meetings table — OnBase renders it as a <table> under
    # the #meetings-list container.  Fall back to any table with
    # meeting-row <tr> elements.
    rows = _find_all(root, "tr")
    meeting_rows = [r for r in rows if "meeting-row" in r.attrs.get("class", "")]
    if not meeting_rows:
        meeting_rows = rows

    for row in meeting_rows:
        row_text = _clean_html_text(_node_text(row))
        if not re.search(r"\d{1,2}/\d{1,2}/\d{4}", row_text):
            continue

        # Extract data attributes (Tempe pattern) or anchor links
        meeting_id_attr = row.attrs.get("data-meeting-id", "").strip()
        has_meeting_id = bool(meeting_id_attr)

        # Collect links
        anchors = []
        seen_links: set[str] = set()
        for anchor in _find_all(row, "a"):
            href = (anchor.attrs.get("href") or "").strip()
            if not href:
                continue
            decoded = urllib.parse.unquote(href)
            abs_url = urllib.parse.urljoin(base_url, decoded)
            text = _clean_html_text(_node_text(anchor))
            key = abs_url or text
            if not key or key in seen_links:
                continue
            seen_links.add(key)
            anchors.append({"text": text.strip(), "href": abs_url})

        # Extract meeting ID from attribute or agenda URL
        mid = meeting_id_attr
        if not mid:
            for a in anchors:
                m = re.search(r"[?&]id=(\d+)", a["href"], re.I)
                if m:
                    mid = m.group(1)
                    break
        if not mid:
            continue
        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        # Determine links by doctype or text
        agenda_url = ""
        agenda_packet_url = ""
        summary_url = ""
        minutes_url = ""
        video_url = ""
        for a in anchors:
            text_lower = a["text"].lower()
            if text_lower == "agenda":
                agenda_url = a["href"]
            elif text_lower == "agenda packet":
                agenda_packet_url = a["href"]
            elif text_lower == "summary":
                summary_url = a["href"]
            elif text_lower in ("minutes", "action minutes"):
                minutes_url = a["href"]
            elif text_lower in ("media", "video", "view media"):
                video_url = a["href"]

        # If anchor-text matching failed, try doctype params
        if not agenda_url:
            for a in anchors:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query)
                if qs.get("doctype") == ["1"] and not a["text"].startswith("Agenda Packet"):
                    agenda_url = a["href"]
                    break
        if not summary_url:
            for a in anchors:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query)
                if qs.get("doctype") == ["3"]:
                    summary_url = a["href"]
                    break

        source_url = agenda_url or meeting_view_url(base_url, mid)

        # Extract date/time from data-sortable attribute or text
        date_val = (row.attrs.get("data-sortable-label") or "").strip()
        time_val = ""
        if has_meeting_id:
            # Tempe format: date-sortable attribute on the meeting-date cell
            for cell in _find_all(row, "td"):
                label = (cell.attrs.get("data-sortable-label") or "").strip()
                if label and re.match(r"\d{1,2}/\d{1,2}/\d{4}", label):
                    date_val = label
                    date_text = _clean_html_text(_node_text(cell))
                    time_m = re.search(r"(\d{1,2}:\d{2}:\d{2}\s*[AP]M)", date_text, re.I)
                    if time_m:
                        time_val = time_m.group(1)
                    break
        else:
            # Maricopa format: extract from row text
            date_m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", row_text)
            if date_m:
                date_val = date_m.group(1)
            time_m = re.search(r"(\d{1,2}:\d{2}:\d{2}\s*[AP]M)", row_text, re.I)
            if time_m:
                time_val = time_m.group(1)

        # Extract title and type from data cells
        cells = []
        for cell in _find_all(row, "td"):
            t = _clean_html_text(_node_text(cell))
            if t:
                cells.append(t)
        meeting_title = ""
        meeting_type_val = ""
        if has_meeting_id:
            # Tempe: first td-ish cell is the meeting name
            for anchor in _find_all(row, "a"):
                text = _clean_html_text(_node_text(anchor))
                if text not in ("Agenda", "Agenda Packet", "Summary", "Media", "Video"):
                    if "lnkMeetingAgenda" not in anchor.attrs.get("id", ""):
                        continue
            # Use the row's semantic cells; prefer data-sortable-type=mtgName
            for cell in _find_all(row, "td"):
                if cell.attrs.get("data-sortable-type") == "mtgName":
                    meeting_title = _clean_html_text(_node_text(cell))
                    break
            if not meeting_title:
                # Fall back to first non-empty, non-link cell
                meeting_title = cells[0] if cells else ""
            # Meeting type from title or cell
            meeting_type_val = meeting_title.split("Meeting")[0].strip()
            if meeting_type_val:
                meeting_type_val += " Meeting"
        else:
            # Maricopa: cells[0]=title, cells[1]=type
            meeting_title = cells[0] if cells else ""
            meeting_type_val = cells[1] if len(cells) > 1 else meeting_title

        # Normalize date
        normalized_date = _normalize_onbase_date(date_val)

        meetings.append({
            "meeting_id": mid,
            "meeting_date": normalized_date,
            "meeting_time": time_val,
            "meeting_title": meeting_title,
            "meeting_type": meeting_type_val,
            "body": public_body_code,
            "agenda_url": agenda_url,
            "agenda_packet_url": agenda_packet_url,
            "summary_url": summary_url,
            "minutes_url": minutes_url,
            "video_url": video_url,
            "source_url": source_url,
        })

    return meetings


def _normalize_onbase_date(date_str: str) -> str:
    """Convert MM/DD/YYYY to YYYY-MM-DD."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str.strip())
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return date_str


def meeting_view_url(base_url: str, meeting_id: str) -> str:
    return f"{base_url}/Meetings/ViewMeeting?id={meeting_id}&doctype=1"


# ──────────────────────────────────────────────
#  CSRF token extraction
# ──────────────────────────────────────────────

async def fetch_csrf_token(page, config: OnBaseConfig) -> Optional[str]:
    """Navigate to the meeting search page and extract a CSRF token."""
    url = f"{config.base_url}{config.search_path}"
    log.info("Tempe: fetching search page for CSRF token (%s)", url)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    html = await page.content()
    token = extract_csrf_token_from_html(html, config)
    log.info("Tempe: CSRF token %s", "found" if token else "NOT FOUND")
    return token


def extract_csrf_token_from_html(html: str, config: OnBaseConfig) -> Optional[str]:
    """Extract a CSRF token from search page HTML.

    Looks for an ``<input name="__RequestVerificationToken">`` element.
    """
    if not config.csrf_required:
        return None
    from scraper.html_utils import _parse_html, _find_all
    root = _parse_html(html)
    for inp in _find_all(root, "input"):
        if inp.attrs.get("name") == "__RequestVerificationToken":
            return (inp.attrs.get("value") or "").strip() or None
    return None


# ──────────────────────────────────────────────
#  Agenda / section parsing
# ──────────────────────────────────────────────

def parse_agenda_html(html: str, meeting_id: str,
                       public_body_code: str = "") -> list[dict]:
    """Parse OnBase ViewMeetingAgenda HTML into agenda item dicts.

    Returns a list of dicts with keys:
        meeting_id, agenda_item_number, agenda_item_id,
        agenda_item_title, agenda_item_text, section_level,
        section_title
    """
    from scraper.html_utils import _parse_html, _find_all, _find_one, _clean_html_text, _node_text

    root = _parse_html(html)
    items: list[dict] = []
    all_divs = _find_all(root, "div")
    section_stack: list[str] = []

    for node in all_divs:
        class_str = node.attrs.get("class", "")

        # ── Sections (accessible-section) ──
        if "accessible-section" in class_str:
            level_match = re.search(r"accessible-section-level-(\d+)", class_str)
            level = int(level_match.group(1)) if level_match else 0

            header = _find_first_header(node)
            header_text = _clean_html_text(_node_text(header)).strip() if header else ""

            num_match = re.match(r"([\dA-Z]+)\s+(.*)", header_text)
            item_number = num_match.group(1) if num_match else ""
            item_title = num_match.group(2).strip() if num_match else header_text

            # Track nesting
            while level < len(section_stack):
                section_stack.pop()
            if item_title:
                section_stack.append(item_title)
            section_title = " / ".join(section_stack) if section_stack else ""

            # Gather text content beyond the header
            item_text = ""
            if level > 0:
                full_text = _clean_html_text(_node_text(node))
                if header_text and header_text in full_text:
                    after_header = full_text.split(header_text, 1)[-1].strip()
                    item_text = after_header

            aid = f"{meeting_id}-{item_number}" if item_number else f"{meeting_id}-s{level}"
            # Skip sections at level 0 (root container — duplicate of level 1+)
            if level > 0:
                items.append({
                    "meeting_id": meeting_id,
                    "agenda_item_number": item_number,
                    "agenda_item_id": aid,
                    "agenda_item_title": item_title,
                    "agenda_item_text": item_text,
                    "section_level": level,
                    "section_title": section_title,
                    "body": public_body_code,
                    "item_type": "section",
                })

        # ── Items (accessible-item) ──
        elif "accessible-item" in class_str:
            level_match = re.search(r"accessible-item-level-(\d+)", class_str)
            level = int(level_match.group(1)) if level_match else 0

            # Find the anchor tag with the title and number
            a_tag = _find_one(node, "a")
            if a_tag:
                full_text = _clean_html_text(_node_text(a_tag))
            else:
                full_text = _clean_html_text(_node_text(node))

            num_match = re.match(r"([\dA-Z]+)\s+(.*)", full_text)
            item_number = num_match.group(1) if num_match else ""
            item_title = num_match.group(2).strip() if num_match else full_text

            # Determine which section this item belongs to
            section_title = ""
            if section_stack:
                parent_depth = level - 1
                parent_sections = section_stack[:parent_depth] if parent_depth > 0 else section_stack[:1]
                section_title = " / ".join(parent_sections) if parent_sections else ""

            # Skip items at level 0 (duplicate section names)
            if level == 0:
                continue

            aid = f"{meeting_id}-{item_number}" if item_number else f"{meeting_id}-i{level}"
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": item_number,
                "agenda_item_id": aid,
                "agenda_item_title": item_title,
                "agenda_item_text": "",
                "section_level": level,
                "section_title": section_title,
                "body": public_body_code,
                "item_type": "item",
            })

    return items


def _walk_accessible_sections(root):
    """Walk the tree depth-first, yielding elements in order."""
    from scraper.html_utils import _find_all
    # Since our HTML parser is custom, do a simple DFS
    yield root
    for child in getattr(root, "children", []):
        if isinstance(child, str):
            continue
        yield from _walk_accessible_sections(child)


def _find_first_header(node):
    """Find the first heading element (h1-h6) inside a node."""
    tag = getattr(node, "tag", "")
    if re.match(r"^h[1-6]$", tag):
        return node
    for child in getattr(node, "children", []):
        if isinstance(child, str):
            continue
        result = _find_first_header(child)
        if result:
            return result
    return None


# ──────────────────────────────────────────────
#  Search / meeting extraction (Playwright)
# ──────────────────────────────────────────────

def _do_search(config: OnBaseConfig,
               start_date: str, end_date: str,
               meeting_type_ids: Optional[list[int]] = None,
               public_body_code: str = "") -> list[dict]:
    """Perform a GET or POST search for meetings, returning parsed results.

    Uses urllib directly (no Playwright needed for search).  Playwright is
    still used for the AJAX-dependent agenda item extraction.
    """
    import urllib.request
    from http.cookiejar import CookieJar

    if not public_body_code:
        public_body_code = config.public_body_code

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    search_url = f"{config.base_url}{config.search_path}"

    if config.search_method == "GET":
        url = config.build_search_url(start_date, end_date, meeting_type_ids)
        log.info("GET search: %s", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
        log.info("GET search: %d bytes", len(html))
    else:
        # POST — first visit the search page to get the CSRF token + cookies
        log.info("Tempe: fetching CSRF token from %s", search_url)
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")

        # Extract CSRF token
        import html as html_mod
        m = re.search(
            r'__RequestVerificationToken.*?value="([^"]+)"',
            html_mod.unescape(html) if hasattr(html_mod, 'unescape') else html,
        )
        if not m:
            # Try unescaped
            m = re.search(r'__RequestVerificationToken.*?value="([^"]+)"', html)
        token = m.group(1) if m else None
        log.info("Tempe: CSRF token %s", "found" if token else "NOT FOUND")
        if token is None and config.csrf_required:
            log.warning("No CSRF token found for %s — search will likely fail", config.name)
            return []

        # Build POST data
        form_data = config.build_search_post_data(
            start_date, end_date, token or "", meeting_type_ids
        )
        log.info("POST search: submitting %s to %s  type_ids=%s",
                 f"{start_date} to {end_date}", search_url, meeting_type_ids or "all")

        post_bytes = urllib.parse.urlencode(form_data, doseq=True).encode("utf-8")
        req = urllib.request.Request(
            search_url,
            data=post_bytes,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with opener.open(req, timeout=30) as resp:
                html = resp.read().decode("utf-8")
            log.info("POST search: %d bytes received", len(html))
        except urllib.error.HTTPError as e:
            log.warning("POST search HTTP %d: %s", e.code, e.reason)
            error_body = e.read().decode("utf-8", errors="replace")
            log.debug("Error body: %s", error_body[:500])
            return []
        except Exception as e:
            log.warning("POST search failed: %s", e)
            return []

    base_url = f"{config.base_url}{config.search_path}"
    return parse_meetings_from_html(html, base_url, public_body_code, meeting_type_ids)


async def search_meetings(page, config: OnBaseConfig,
                           start_date: str, end_date: str,
                           meeting_type_ids: Optional[list[int]] = None,
                           public_body_code: str = "") -> list[dict]:
    """Search for meetings and return parsed results.

    Delegates to the synchronous _do_search() which uses urllib directly.
    The *page* argument is accepted for API compatibility but not used.
    """
    return _do_search(config, start_date, end_date, meeting_type_ids, public_body_code)
async def fetch_agenda_html(page, config: OnBaseConfig, meeting_id: int) -> str:
    """Fetch the agenda-items HTML for a meeting."""
    url = config.build_agenda_view_url(meeting_id)
    log.debug("Fetching agenda HTML: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    return await page.content()


# ──────────────────────────────────────────────
#  Document download (two-step OnBase protocol)
# ──────────────────────────────────────────────

def download_document(config: OnBaseConfig, meeting_id: int, name: str,
                      doc_type: int = 1) -> bytes:
    """Download a meeting document (agenda PDF or packet PDF).

    OnBase uses a two-step protocol:
      1. POST InvokeDownloadMeetingDocument -> {DocumentName, MeetingId, DocumentType}
      2. GET ViewDocument -> returns the actual PDF bytes
    """
    import urllib.request, json
    from http.cookiejar import CookieJar

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    base = config.base_url
    log.info("Download: establishing session at %s", base)
    opener.open(urllib.request.Request(base, headers={"User-Agent": "Mozilla/5.0"}))

    invoke_url = ("%s/Documents/InvokeDownloadMeetingDocument/%s"
                  "?meetingId=%s&documentType=%s") % (base, name, meeting_id, doc_type)
    log.info("Download: invoking %s", invoke_url)
    req1 = urllib.request.Request(invoke_url, data=b"", headers={
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    })
    with opener.open(req1, timeout=30) as resp:
        meta = json.loads(resp.read().decode("utf-8"))

    doc_name = meta.get("DocumentName", name)
    meeting_id_resp = meta.get("MeetingId", meeting_id)
    doc_type_str = meta.get("DocumentType", str(doc_type))
    log.info("Download: metadata received: %s", doc_name)

    view_url = ("%s/Documents/ViewDocument/%s"
                "?meetingId=%s&documentType=%s") % (base, doc_name, meeting_id_resp, doc_type_str)
    log.info("Download: fetching %s", view_url)
    req2 = urllib.request.Request(view_url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req2, timeout=120) as resp:
        data = resp.read()

    log.info("Download: %d bytes (%s)", len(data), doc_name)
    return data


# ──────────────────────────────────────────────
#  Agenda fetch (synchronous, no Playwright needed)
# ──────────────────────────────────────────────


# ──────────────────────────────────────────────

def fetch_agenda_sync(config: OnBaseConfig, meeting_id: int) -> str:
    """Fetch agenda HTML for a meeting via direct HTTP GET.

    The OnBase ViewMeetingAgenda endpoint returns full HTML without
    requiring JavaScript execution, so Playwright is not needed.
    """
    import urllib.request
    url = config.build_agenda_view_url(meeting_id)
    log.info("Fetching agenda: %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")
    log.debug("Agenda HTML: %d bytes", len(html))
    return html


# ──────────────────────────────────────────────
#  Client class (wraps module-level functions)
# ──────────────────────────────────────────────

class OnBaseAgendaClient:
    """Client for an OnBase Agenda Online instance.

    Wraps module-level functions with an instance bound to a config.

    Usage::

        client = OnBaseAgendaClient(TEMPE_CONFIG)
        client.parse_meetings("<html>...</html>", "https://...")
        meetings = await client.search(page, "2026-01-01", "2026-06-30", [109])
    """

    def __init__(self, config: OnBaseConfig):
        self.config = config

    def parse_meetings(self, html: str, base_url: str,
                       public_body_code: str = "",
                       meeting_type_ids: Optional[list[int]] = None) -> list[dict]:
        return parse_meetings_from_html(
            html, base_url,
            public_body_code or self.config.public_body_code,
            meeting_type_ids,
        )

    def parse_agenda(self, html: str, meeting_id: str,
                     public_body_code: str = "") -> list[dict]:
        return parse_agenda_html(
            html, meeting_id,
            public_body_code or self.config.public_body_code,
        )

    async def fetch_csrf(self, page) -> Optional[str]:
        return await fetch_csrf_token(page, self.config)

    def extract_csrf(self, html: str) -> Optional[str]:
        return extract_csrf_token_from_html(html, self.config)

    async def search(self, page, start_date: str, end_date: str,
                     meeting_type_ids: Optional[list[int]] = None,
                     public_body_code: str = "") -> list[dict]:
        return await search_meetings(
            page, self.config, start_date, end_date,
            meeting_type_ids,
            public_body_code or self.config.public_body_code,
        )

    async def fetch_agenda(self, page, meeting_id: int) -> str:
        return await fetch_agenda_html(page, self.config, meeting_id)

    def build_download_url(self, meeting_id: int, name: str, doc_type: int = 1) -> str:
        return self.config.build_download_url(meeting_id, name, doc_type)

    def build_agenda_download_url(self, meeting_id: int, name: str) -> str:
        return self.config.build_agenda_download_url(meeting_id, name)

    def build_packet_download_url(self, meeting_id: int, name: str) -> str:
        return self.config.build_packet_download_url(meeting_id, name)
