"""
Shared Destiny/AgendaQuick parser — proper HTML parser, not regex-on-HTML.

All inputs/outputs match the per-jurisdiction scraper interfaces:
  parse_meetings(html, body_map) -> list[dict]
  parse_agenda_items(html, meeting_seq) -> list[dict]

Uses Python's ``html.parser.HTMLParser`` to walk meeting list and agenda
detail tables, avoiding row-boundary bugs from the old regex approach.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Optional

from scraper.io_utils import _normalize_text_date

log = logging.getLogger(__name__)

BASE_URL = "https://public.destinyhosted.com"


# ── Utilities ──


def build_month_url(org_id: str, year: int, month: int) -> str:
    return (
        f"{BASE_URL}/agenda_publish.cfm?id={org_id}"
        f"&mt=ALL&get_month={month}&get_year={year}"
    )


def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def extract_meeting_type(body_name: str) -> str:
    tl = body_name.lower()
    if "cancellation" in tl or "canceled" in tl or "cancelled" in tl:
        return "Cancelled"
    if "quorum notice" in tl:
        return "Quorum Notice"
    if "upcoming agenda" in tl:
        return "Upcoming Agenda Items"
    if "study session" in tl:
        return "Study Session"
    if "work session" in tl:
        return "Work Session"
    if "special meeting" in tl or tl.endswith("special"):
        return "Special"
    if "regular meeting" in tl:
        return "Regular Meeting"
    return "Regular Meeting"


# ── Meeting-list parser ──


class MeetingListParser(HTMLParser):
    """Parse Destiny meeting-list HTML table.

    Track <tr> boundary directly — not via html.rfind() — to avoid
    cross-row contamination.
    """

    DATE_RE = re.compile(r"[A-Z][a-z]+ \d+, \d{4}")

    def __init__(self, body_map: dict[str, tuple[str, str]]):
        super().__init__()
        self.body_map = body_map
        self.meetings: list[dict] = []
        self._row_cells: list[dict] = []   # cells for current row
        self._current_text: list[str] = []
        self._current_href = ""
        self._in_tr = False

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._in_tr = True
            self._row_cells = []
            self._current_text = []
            self._current_href = ""
            return
        if not self._in_tr:
            return
        if tag == "a":
            href = attrs_dict.get("href", "")
            if href:
                self._current_href = urllib.parse.urljoin(BASE_URL, href)

    def handle_data(self, data: str):
        if self._in_tr:
            text = data.strip()
            if text:
                self._current_text.append(data)

    def handle_endtag(self, tag: str):
        if not self._in_tr:
            return
        if tag == "td":
            self._close_cell()
            return
        if tag == "tr":
            self._close_cell()  # flush last cell
            self._emit_row()
            self._in_tr = False

    def _close_cell(self):
        text = "".join(self._current_text).strip()
        text = html_module.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        self._row_cells.append({"text": text, "href": self._current_href})
        self._current_text = []
        self._current_href = ""

    def _emit_row(self):
        if not self._row_cells:
            return
        col0 = self._row_cells[0]
        date_raw = col0.get("text", "")
        href = col0.get("href", "")
        if not href or not self.DATE_RE.match(date_raw):
            return

        # Body name is the first non-empty column after col0 that isn't
        # "Meeting Results", "Minutes", or a Video/PDF link artifact
        body_name = ""
        for cell in self._row_cells[1:]:
            t = cell.get("text", "")
            if t and not t.startswith("Meeting Results") \
                    and not t.startswith("Minutes") \
                    and t not in ("PDF", "Video", "Audio", ""):
                body_name = t
                break
        if not body_name:
            return

        slug, code = self._resolve_body(body_name)

        # Destiny table columns: 0=agenda, 1=body, 2=minutes, 3=other links (video/audio)
        # Same layout for both mt=ALL and mt=PC views.
        minutes_url = ""
        if len(self._row_cells) > 2:
            cell2 = self._row_cells[2]
            c2_url = cell2.get("href", "")
            c2_text = cell2.get("text", "").strip()
            # Valid minutes cells have a dsp=min href and non-empty text.
            # Empty cells (&nbsp;) produce no href and strip to "".
            if c2_url and "dsp=min" in c2_url and c2_text:
                minutes_url = c2_url

        video_url = ""
        if len(self._row_cells) > 3:
            cell3 = self._row_cells[3]
            c3_url = cell3.get("href", "")
            c3_text = cell3.get("text", "").strip()
            if c3_url and c3_text and c3_text not in ("&nbsp;", " ", "", " ", "Video", "Audio"):
                video_url = c3_url

        self.meetings.append({
            "meeting_date": _normalize_text_date(date_raw) or date_raw,
            "body_name": body_name,
            "body_slug": slug,
            "body_code": code,
            "meeting_type": extract_meeting_type(body_name),
            "meeting_id": self._extract_seq(href),
            "meeting_seq": self._extract_seq(href),
            "agenda_url": href,
            "minutes_url": minutes_url,
            "results_url": "",
            "video_url": video_url,
        })

    def _resolve_body(self, body_name: str) -> tuple[str, str]:
        key = body_name.lower().strip()
        for pattern, (slug, code) in self.body_map.items():
            if pattern in key:
                return slug, code
        for slug, code in self.body_map.values():
            if "-cc" in code:
                return slug, code
        return "unknown", "unknown"

    @staticmethod
    def _extract_seq(url: str) -> str:
        m = re.search(r"seq=(\d+)", url)
        return m.group(1) if m else ""


def parse_meetings(html: str, body_map: dict[str, tuple[str, str]]) -> list[dict]:
    """Parse Destiny meeting-list HTML into structured meeting dicts.

    Return format:
      {meeting_date, body_name, body_slug, body_code, meeting_type,
       meeting_id, meeting_seq, agenda_url}
    """
    parser = MeetingListParser(body_map)
    parser.feed(html)
    return parser.meetings


# ── Agenda item parser ──


class AgendaItemParser(HTMLParser):
    """Parse Destiny agenda detail page <tr class='top'> rows."""

    def __init__(self, meeting_seq: str):
        super().__init__()
        self.meeting_seq = meeting_seq
        self.items: list[dict] = []
        self.sort_order = 0
        self._row_cells: list[str] = []
        self._row_hrefs: list[str] = []  # href of first <a> in each cell
        self._buf: list[str] = []
        self._current_href = ""
        self._in_row = False
        self._in_title_link = False

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        if tag == "tr" and "top" in cls.split():
            self._in_row = True
            self._row_cells = []
            self._row_hrefs = []
            self._buf = []
            self._current_href = ""
            return
        if self._in_row and tag == "a":
            href = attrs_dict.get("href", "")
            if href:
                self._current_href = urllib.parse.urljoin(BASE_URL, href)

    def handle_data(self, data: str):
        if self._in_row:
            self._buf.append(data)

    def handle_endtag(self, tag: str):
        if not self._in_row:
            return
        if tag == "td":
            text = "".join(self._buf).strip()
            text = html_module.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            self._row_cells.append(text)
            self._row_hrefs.append(self._current_href)
            self._buf = []
            self._current_href = ""
            return
        if tag == "a":
            self._in_title_link = False
        if tag == "tr":
            # Flush remaining buffer
            if self._buf:
                text = "".join(self._buf).strip()
                text = html_module.unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                self._row_cells.append(text)
                self._row_hrefs.append(self._current_href)
                self._buf = []
            self._emit_item()
            self._in_row = False

    def _emit_item(self):
        if len(self._row_cells) < 4:
            return

        # Find the item number (N. or a.) in any cell, and any agenda memo URL
        item_number = None
        agenda_url = ""
        for i, cell in enumerate(self._row_cells):
            m = re.match(r"^([\d\w.-]+)\.$", cell.strip())
            if m:
                item_number = m.group(1)
                # Title and URL come from cells after the number
                title_parts = [c for c in self._row_cells[i+1:] if c and len(c) > 3]
                title = title_parts[0] if title_parts else ""
                # Agenda memo URL is the href from the title cell
                href_idx = self._row_cells.index(title) if title else -1
                if href_idx >= 0 and href_idx < len(self._row_hrefs):
                    h = self._row_hrefs[href_idx]
                    if h and ("dsp=agm" in h or "dsp=ag" in h):
                        agenda_url = h
                break

        if not item_number:
            return

        self.sort_order += 1
        self.items.append({
            "meeting_id": self.meeting_seq,
            "agenda_item_number": item_number,
            "agenda_item_title": title,
            "agenda_item_text": "",
            "item_type": "",
            "sort_order": self.sort_order,
            "source_url": agenda_url or "",
            "agenda_item_url": agenda_url or "",
        })


def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse Destiny agenda detail page into item dicts.

    Return format:
      {meeting_id, agenda_item_number, agenda_item_title, ...}
    """
    parser = AgendaItemParser(meeting_seq)
    parser.feed(html)
    return parser.items

# ── Supporting document extraction from agenda memo pages ──


def fetch_agenda_memo_docs(memo_url: str, timeout: int = 30) -> list[dict]:
    """Fetch a Destiny agenda memo page and extract supporting document links.

    Memo pages use ``onclick="popupAttachments('/path/to/doc.pdf', ...)"``
    to surface attachments.  We extract the PDF path and reconstruct the
    full URL.

    Returns a list of dicts:
      {document_title, document_url, file_extension}
    """
    import html as html_module

    try:
        html_text = fetch_page(memo_url, timeout=timeout)
    except Exception as e:
        log.warning("Failed to fetch memo %s: %s", memo_url, e)
        return []

    docs: list[dict] = []
    seen: set[str] = set()

    # Pattern: onclick="popupAttachments('/path/to/doc.pdf','ATTACHMENTS')"
    popup_pat = re.compile(
        # Match onclick="popupAttachments('...','ATTACHMENTS')"
        r"onclick=.*?popupAttachments\([\"']"
        r"([^\"']+?)"
        r"[\"']\s*,\s*[\"']ATTACHMENTS[\"']\s*\)",
        re.DOTALL,
    )
    for m in popup_pat.finditer(html_text):
        path = m.group(1)
        path = html_module.unescape(path)
        if path in seen:
            continue
        seen.add(path)

        doc_url = urllib.parse.urljoin(BASE_URL, path) if path.startswith("/") else path

        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""

        # Find the document title: text inside the same <a> after onclick
        title = ""
        after = html_text[m.end(): m.end() + 200]
        title_m = re.search(r'>([^<]+?)</a>', after)
        if title_m:
            title = title_m.group(1).strip()
        if not title:
            fname = path.rsplit("/", 1)[-1] if "/" in path else path
            title = fname.rsplit(".", 1)[0] if "." in fname else fname

        docs.append({
            "document_title": title,
            "document_url": doc_url,
            "file_extension": ext,
        })

    return docs

