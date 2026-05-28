"""
City of Phoenix meeting extraction via Legistar.

Phoenix uses the Legistar agenda management system at ``phoenix.legistar.com``.
Same platform as Mesa (``mesa.legistar.com``).

Council meetings, Planning Commission, and subcommittees are all available
through the Calendar.aspx page.
"""

from __future__ import annotations
import logging
import re
import urllib.parse
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──

PUBLIC_BODY_CODE = "phoenix-cc"
DEFAULT_BODY_SLUGS = ["phoenix-city-council"]

BASE_URL = "https://phoenix.legistar.com"
CALENDAR_URL = f"{BASE_URL}/Calendar.aspx"
SOURCE_INSTANCE_URL = BASE_URL
SOURCE_SYSTEM = "legistar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Body name → (slug, code)
BODY_SLUG_MAP: dict[str, tuple[str, str]] = {
    "city council formal meeting": ("phoenix-city-council", "phoenix-cc"),
    "city council policy session": ("phoenix-city-council", "phoenix-cc"),
    "city council special meeting": ("phoenix-city-council", "phoenix-cc"),
    "city council work study session": ("phoenix-city-council", "phoenix-cc"),
    "planning commission": ("phoenix-planning-commission", "phoenix-pc"),
    "community services and education subcommittee": ("phoenix-community-services-sub", "phoenix-cs"),
    "economic development and the arts subcommittee": ("phoenix-economic-dev-sub", "phoenix-ed"),
    "public safety and justice subcommittee": ("phoenix-public-safety-sub", "phoenix-ps"),
    "transportation, infrastructure, and planning subcommittee": ("phoenix-transportation-sub", "phoenix-ti"),
    "general information packet": ("phoenix-general-packet", "phoenix-gp"),
    "subcommittee general information packet": ("phoenix-sub-packet", "phoenix-sp"),
    "virtual community budget hearing": ("phoenix-budget-hearing", "phoenix-bh"),
}

DEFAULT_BODY_SLUGS = ["phoenix-city-council"]


def _resolve_body(body_name: str) -> tuple[str, str, str]:
    """Resolve a Phoenix Legistar body name to (slug, code, meeting_type)."""
    lower = body_name.lower().strip()
    for pattern, (slug, code) in BODY_SLUG_MAP.items():
        if lower == pattern or lower.startswith(pattern):
            return slug, code, body_name.strip()
    return "phoenix-city-council", "phoenix-cc", body_name.strip()


# ── HTTP helpers ──

def fetch_page(url: str, timeout: int = 30) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def _extract_aspnet_form_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                  "__PREVIOUSFOCUSED", "__CT100"]:
        m = re.search(r'id=["\']' + name + r'["\'][^>]*value=["\']([^"\']*)["\']', html)
        if m:
            fields[name] = m.group(1)
        # Also try alternate format
        m2 = re.search(r'name=["\']' + name + r'["\'][^>]*value=["\']([^"\']*)["\']', html)
        if m2 and name not in fields:
            fields[name] = m2.group(1)
    return fields


def _build_year_client_state(year: str) -> str:
    """Build the RadComboBox ClientState for year selection."""
    return (
        '{"logEntries":[],"value":"","text":"' + year + '",'
        '"enabled":true,"checkedIndices":[],"checkedItemsTextOverflows":false}'
    )


def _parse_meetings_from_html(html: str) -> list[dict]:
    """Parse the Legistar RadGrid table for meeting rows."""
    meetings: list[dict] = []
    rows = re.findall(
        r'<tr[^>]*class="rgRow[^"]*"[^>]*>(.*?)</tr>',
        html, re.DOTALL
    )
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 2:
            continue

        body_name = re.sub(r"<[^>]+>", " ", cells[0]).strip()
        date_raw = re.sub(r"<[^>]+>", " ", cells[1]).strip()
        details_html = cells[3] if len(cells) > 3 else ""

        if not body_name or not date_raw:
            continue

        slug, code, mtype = _resolve_body(body_name)

        # Parse date (MM/DD/YYYY or other formats)
        meeting_date = ""
        for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
            try:
                from datetime import datetime
                meeting_date = datetime.strptime(date_raw, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if not meeting_date:
            meeting_date = date_raw

        # Extract meeting ID from the Details link
        meeting_id = ""
        details_link = re.search(r'href="([^"]*)"[^>]*>Details', details_html)
        if details_link:
            m = re.search(r'ID=(\d+)', details_link.group(1))
            if m:
                meeting_id = m.group(1)

        meetings.append({
            "meeting_id": meeting_id or f"{body_name}-{date_raw}",
            "meeting_date": meeting_date,
            "meeting_type": mtype,
            "meeting_title": body_name,
            "body_slug": slug,
            "body_code": code,
            "source_url": f"{BASE_URL}/Calendar.aspx",
        })

    return meetings


# ── Search ──

def search_phoenix_meetings(year: int, body_slugs: Optional[list[str]] = None) -> list[dict]:
    """Search Phoenix Legistar for meetings in a given year."""
    import urllib.parse

    year_label = str(year)

    # GET the Calendar page to extract form fields
    html = fetch_page(CALENDAR_URL)
    fields = _extract_aspnet_form_fields(html)
    client_state = _build_year_client_state(year_label)

    # POST with year selection
    form_data = [
        ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
        ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
        ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
        ("ctl00_ContentPlaceHolder1_lstYears_ClientState", client_state),
        ("ctl00_ContentPlaceHolder1_lstYears_Input", year_label),
        ("ctl00_ContentPlaceHolder1_txtSearch", ""),
        ("__EVENTTARGET", "ctl00$ContentPlaceHolder1$lstYears"),
        ("__EVENTARGUMENT", ""),
    ]

    data = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(
        CALENDAR_URL, data=data,
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to POST year search for %s: %s", year_label, e)
        raise

    meetings = _parse_meetings_from_html(result_html)

    # Filter by body slugs if specified
    if body_slugs:
        meetings = [m for m in meetings if m["body_slug"] in body_slugs]

    return meetings
