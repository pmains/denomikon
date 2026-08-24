"""
Shared Granicus PDF agenda parsing for Paradise Valley and Queen Creek.

Granicus agenda PDFs have a consistent format: numbered items (1., 2., 3.)
with lettered sub-items (A., B., C.) under consent/regular agenda sections.

Usage:
    from scraper.platforms.granicus_common import fetch_and_parse_agenda
    items = fetch_and_parse_agenda(agenda_viewer_url, meeting_id)
"""

import logging
import re
import subprocess
import tempfile
import os
import ssl
import urllib.request

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Some Granicus PDFs are served from S3 with certificate issues
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def fetch_pdf(agenda_url: str) -> bytes | None:
    """Follow Granicus redirect and download the actual PDF."""
    try:
        req = urllib.request.Request(agenda_url, headers=HEADERS)
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as resp:
            redirect_url = resp.url  # Follow redirect
        
        # Some Granicus instances redirect through Google Docs viewer
        # Extract the actual PDF URL from the gview URL
        pdf_url = redirect_url
        if "docs.google.com/gview" in redirect_url:
            import urllib.parse as _up
            parsed = _up.urlparse(redirect_url)
            qs = _up.parse_qs(parsed.query)
            pdf_url = qs.get("url", [redirect_url])[0]
        
        req2 = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req2, context=_ssl_ctx, timeout=30) as resp2:
            return resp2.read()
    except Exception as e:
        log.warning("Failed to download PDF from %s: %s", agenda_url, e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Extract text from PDF bytes using pdftotext."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.warning("pdftotext failed: %s", e)
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def parse_items(text: str) -> list[dict]:
    """Parse numbered agenda items from a Granicus agenda PDF."""
    items: list[dict] = []
    sort_order = 0
    lines = text.split("\n") if text else []

    # Patterns
    top_item = re.compile(r"^\s*(\d+)\.\s+(.+)")
    sub_item = re.compile(r"^\s*([A-Z])\.\s+(.+)")

    current_top_num = ""
    current_top_title = ""
    current_body: list[str] = []

    def flush():
        nonlocal current_top_num, current_top_title, current_body
        if current_top_num:
            sort_order = len(items)
            body_text = "\n".join(current_body).strip()
            items.append({
                "agenda_item_number": current_top_num,
                "item_type_category": "item",
                "agenda_item_title": current_top_title[:200],
                "agenda_item_text": body_text if body_text else current_top_title,
                "sort_order": sort_order,
            })
        current_top_num = ""
        current_top_title = ""
        current_body = []

    for line in lines:
        s = line.strip()

        # Skip boilerplate
        if not s or len(s) < 3:
            current_body.append(s)
            continue
        if re.match(r"^Page\s+\d+|^\d+\s*of\s+\d+", s):
            continue

        # Top-level numbered item: "1.   CALL TO ORDER"
        tm = top_item.match(s)
        if tm:
            flush()
            current_top_num = tm.group(1)
            current_top_title = tm.group(2).strip()
            current_body = [s]
            continue

        # Sub-item: "A.   Consideration and possible approval..."
        sm = sub_item.match(s)
        if sm and current_top_num:
            # Sub-items append to the current top item's body
            current_body.append(s)
            continue

        # Continuation of current item
        if current_top_num:
            current_body.append(s)

    flush()
    return items


def fetch_and_parse_agenda(agenda_url: str, meeting_id: str, body_code: str = "") -> list[dict]:
    """Download, parse, and return agenda items from a Granicus agenda."""
    pdf_bytes = fetch_pdf(agenda_url)
    if not pdf_bytes:
        return []
    text = extract_pdf_text(pdf_bytes)
    if not text:
        return []
    items = parse_items(text)
    for item in items:
        an = item.get("agenda_item_number", "") or ""
        item["meeting_id"] = meeting_id
        item["agenda_item_id"] = f"{body_code}-{meeting_id}_{an}" if body_code else f"g-{meeting_id}_{an}"
        item["source_body"] = body_code if body_code else "granicus"
        item["source_url"] = agenda_url
    return items


# ── Legistar Agenda Packet download (Paradise Valley) ──

_LEGISTAR_CALENDAR_CACHE: dict | None = None


def _fetch_legistar_calendar() -> str | None:
    """Fetch the Paradise Valley Legistar calendar page with initial ViewState."""
    url = "https://paradisevalleyaz.legistar.com/Calendar.aspx"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch Legistar calendar: %s", e)
        return None


def search_legistar_meeting(body_name: str, date_str: str) -> tuple[str, str] | None:
    """Search Paradise Valley Legistar for a meeting by body name and date.

    Uses the Legistar ASP.NET calendar search with ViewState handling.
    Returns (meeting_id, guid) tuple or None if not found.
    """
    html = _fetch_legistar_calendar()
    if not html:
        return None

    viewstate = re.search(r'id="__VIEWSTATE"\s+value="([^"]*)"', html)
    validation = re.search(r'__EVENTVALIDATION" value="([^"]*)"', html)
    viewgen = re.search(r'__VIEWSTATEGENERATOR" value="([^"]*)"', html)
    if not viewstate:
        return None

    form_data = {
        "__VIEWSTATE": viewstate.group(1),
        "__EVENTVALIDATION": validation.group(1) if validation else "",
        "__VIEWSTATEGENERATOR": viewgen.group(1) if viewgen else "",
        "ctl00$ContentPlaceHolder1$txtSearch": body_name,
        "ctl00$ContentPlaceHolder1$btnSearch": "Search",
    }
    data = urllib.parse.urlencode(form_data).encode()
    url = "https://paradisevalleyaz.legistar.com/Calendar.aspx"
    try:
        req = urllib.request.Request(url, data=data, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Legistar calendar search: %s", e)
        return None

    # Find all MeetingDetail links with their surrounding context
    # The context before the link contains the meeting date
    for m in re.finditer(r'href="MeetingDetail\.aspx\?ID=(\d+)&amp;GUID=([^"]+)"', result):
        mid, guid = m.group(1), m.group(2)
        # Look for date in the surrounding 1000 chars before the link
        ctx_start = max(0, m.start() - 1000)
        ctx = result[ctx_start:m.end()]
        date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', ctx)
        if not date_match:
            continue
        # Compare dates (normalize both to M/D/YYYY)
        from datetime import datetime as _dt
        try:
            dt = _dt.strptime(date_str, "%Y-%m-%d")
            target = dt.strftime("%-m/%-d/%Y")
            if date_match.group(1) == target:
                return (mid, guid.split("&")[0])
        except ValueError:
            continue

    return None


def download_agenda_packet(meeting_id: str, guid: str) -> bytes | None:
    """Download the agenda packet PDF from Legistar.

    The agenda packet contains all supporting documents (staff reports, exhibits,
    attachments) combined into a single PDF.
    """
    url = f"https://paradisevalleyaz.legistar.com/View.ashx?M=PA&ID={meeting_id}&GUID={guid}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=60) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Failed to download agenda packet: %s", e)
        return None


# ── Doc check probe ───────────────────────────────────────────────────────

# Granicus instances and their body code prefixes
_GRANICUS_INSTANCES: dict[str, str] = {
    "buckeye": "https://buckeyeaz.granicus.com",
    "surprise": "https://surpriseaz.granicus.com",
    "goodyear": "https://goodyearaz.granicus.com",
    "avondale": "https://avondaleaz.granicus.com",
}


def check_meeting_docs_granicus(meeting) -> "DocCheckResult":
    """Granicus doc check probe.

    Checks whether a Granicus meeting has published document attachments.
    Fetches the meeting's AgendaViewer page and looks for PDF/doc links.

    Returns a DocCheckResult.
    """
    from scraper.common.doc_check import DocCheckResult

    source_url = meeting.source_url or ""
    if "granicus.com" not in source_url:
        return DocCheckResult(error="No Granicus source_url")

    req = urllib.request.Request(
        source_url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return DocCheckResult(error=f"Fetch failed: {e}")

    # Look for document links on the page
    doc_patterns = re.findall(
        r'(?i)(?:href=[\"\']?)([^\"\'>]+(?:\.pdf|\.doc|\.docx))',
        html,
    )
    # Also look for AgendaPacket or StaffReport download links
    attachment_links = re.findall(
        r'(?i)(?:downloadDocument|DownloadFile|AttachmentHandler|getDocument)',
        html,
    )

    has_docs = bool(doc_patterns) or bool(attachment_links)
    return DocCheckResult(docs_available=has_docs)
