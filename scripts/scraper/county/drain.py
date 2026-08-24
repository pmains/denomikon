"""Drainage Review Board (DRB/Drain) meeting and agenda extraction.

Drainage Review Board uses the same AgendaCenter system as PZ and ADJ
(CID=19 instead of CID=9/3), hosted on a different domain
(mcdot.maricopa.gov instead of www.maricopa.gov).

Year-tab pagination and catAgendaRow structure are identical to PZ/ADJ.

Key differences from PZ/ADJ:
- Overview pages use <div class="item level1"><span class="title"> instead of <h1 class="title">
- Some meetings have "(No Agenda)" — simple overview with no items/staff reports
- Some meetings have "(PDF)" — direct PDF without overview page
- Board existed 2011-2013, now defunct
- Domain: mcdot.maricopa.gov
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Optional

from scraper.common.html_utils import _parse_html, _find_all, _clean_html_text, _node_text
from scraper.common.io_utils import _normalize_text_date
from scraper.common.models import Meeting
from scraper.common.utils import PZ_SEARCH_BASE, PZ_AGENDA_BASE
from scraper.common.utils import CASE_PATTERN

# ── Constants ──

DRAIN_CID = "19,"
DRAIN_BASE_DOMAIN = "https://mcdot.maricopa.gov"
DRAIN_SEARCH_BASE = "https://mcdot.maricopa.gov/AgendaCenter/Search/"
DRAIN_AGENDA_BASE = "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/"


# ── URL building ──


def build_drain_search_url(start_date: str, end_date: str) -> str:
    """Build AgendaCenter search URL for Drainage Review Board meetings.

    Uses mcdot.maricopa.gov domain with CID=19.
    """
    params = {
        "term": "",
        "CIDs": DRAIN_CID,
        "startDate": start_date,
        "endDate": end_date,
        "dateRange": "",
        "dateSelector": "",
    }
    qs = urllib.parse.urlencode(params)
    return f"{DRAIN_SEARCH_BASE}?{qs}"


# ── Meeting extraction ──


async def extract_drain_meetings(page, search_url: str) -> list[Meeting]:
    """Extract Drainage Review Board meetings from AgendaCenter search results.

    Same year-tab clicking pattern as PZ/ADJ. The AgendaCenter only shows one
    year's meetings in the initial HTML; other years load via AJAX when the
    year tab (changeYear(...)) is clicked.
    """
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    all_meetings: list[Meeting] = []
    seen_ids: set[str] = set()

    # Collect from the initial (default year) HTML
    html = await page.content()
    initial_meetings = parse_drain_meetings_from_html(html, search_url)
    all_meetings.extend(initial_meetings)
    for m in initial_meetings:
        seen_ids.add(m.meeting_id)

    # Find available year tabs (e.g. changeYear(2023, 19, ...), changeYear(2024, 19, ...))
    year_tabs = await page.evaluate(
        r"""
        () => {
            const seen = new Set();
            const years = [];
            for (const a of document.querySelectorAll('a[href*="changeYear"]')) {
                const m = (a.getAttribute('href') || '').match(/changeYear\((\d{4})/);
                if (m && !seen.has(m[1])) {
                    seen.add(m[1]);
                    years.push(parseInt(m[1], 10));
                }
            }
            return years.sort((a, b) => a - b);
        }
        """
    )

    loaded_years = {int(m.meeting_date[:4]) for m in all_meetings}

    for year in year_tabs:
        if year in loaded_years:
            continue

        # Click the year tab to trigger the AJAX load via changeYear()
        try:
            tab_locator = page.locator(f'a[href*="changeYear({year}"]').first
            await tab_locator.evaluate("el => el.click()")
        except Exception:
            continue

        # Wait for AJAX response to populate
        await page.wait_for_timeout(2500)

        html = await page.content()
        year_meetings = parse_drain_meetings_from_html(html, search_url)

        for m in year_meetings:
            if m.meeting_id not in seen_ids:
                all_meetings.append(m)
                seen_ids.add(m.meeting_id)

    return all_meetings


def parse_drain_meetings_from_html(html: str, base_url: str) -> list[Meeting]:
    """Parse Drainage Review Board meetings from AgendaCenter search HTML.

    Same catAgendaRow structure as PZ/ADJ. body="drain", meeting_type="Drainage Review Board".
    """
    root = _parse_html(html)
    meetings: list[Meeting] = []

    rows = _find_all(root, "tr")
    for row in rows:
        classes = (row.attrs.get("class") or "").split()
        if "catAgendaRow" not in classes:
            continue

        cells = _find_all(row, "td")
        if not cells:
            continue

        first_cell = cells[0]
        cell_text = _clean_html_text(_node_text(first_cell))

        # Extract meeting date from the <strong> in the <h3>
        meeting_date = ""
        for h3 in _find_all(first_cell, "h3"):
            for strong in _find_all(h3, "strong"):
                aria = strong.attrs.get("aria-label", "")
                if aria:
                    dm = re.search(r"(\w+ \d{1,2},? \d{4})", aria)
                    if dm:
                        date_str = dm.group(1)
                        meeting_date = _normalize_text_date(date_str)
                        break
                abbr = _find_all(strong, "abbr")
                date_text = _clean_html_text(_node_text(strong))
                dm = re.search(r"(\w{3,9})\s+(\d{1,2}),?\s+(\d{4})", date_text)
                if dm:
                    meeting_date = _normalize_text_date(f"{dm.group(1)} {dm.group(2)}, {dm.group(3)}")
                    break
            if meeting_date:
                break
        if not meeting_date:
            continue

        # Find agenda link — drain meetings may have multiple URLs per date
        # (different IDs). Deduplicate by agenda URL.
        agenda_url = ""
        meeting_title = ""
        for a in _find_all(first_cell, "a"):
            href = a.attrs.get("href", "")
            if "/Agenda/" in href or "ViewFile/Agenda" in href:
                agenda_url = urllib.parse.urljoin(base_url, href)
                meeting_title = _clean_html_text(_node_text(a))
                break

        if not agenda_url:
            continue

        meetings.append(Meeting(
            meeting_date=meeting_date,
            meeting_time="",
            meeting_title=meeting_title,
            meeting_type="Drainage Review Board",
            body="drain",
            row_text=_clean_html_text(_node_text(row)),
            detail_url=agenda_url,
            agenda_url=agenda_url,
        ))

    return meetings


# ── Agenda overview / PDF extraction ──


async def extract_drain_agenda_items(page, meeting_url: str) -> list[dict]:
    """Extract agenda items from a Drainage Review Board AgendaCenter meeting.

    Unlike PZ/ADJ, drain overview pages may use <div class="item level1"> with
    <span class="title"> instead of <h1 class="title">. Some meetings have
    "(No Agenda)" and return an empty list gracefully.
    """
    # If the URL is a direct PDF (no ?html=true, mcdot domain), skip Playwright
    # and download the PDF directly — Playwright's page.goto triggers a
    # "Download is starting" error on PDF URLs.
    is_direct_pdf = "?html=true" not in meeting_url

    html = ""
    base_for_url = urllib.parse.urljoin(meeting_url, "/")
    overview = None

    if not is_direct_pdf:
        try:
            await page.goto(meeting_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            html = await page.content()
            # Parse overview: try h1.title first (PZ/ADJ style), then div.item.level1
            overview = parse_drain_overview(html, meeting_url, base_for_url)
        except Exception:
            # Page load failed (usually "Download is starting" for PDF URLs)
            # — fall through to the PDF download path below
            pass

    agenda_pdf_url = ""

    if overview and overview.get("agenda_pdf_url"):
        agenda_pdf_url = overview.get("agenda_pdf_url", "")
    staff_report_files: list[dict] = []
    if overview:
        agenda_pdf_url = overview.get("agenda_pdf_url", "")
        staff_report_files = overview.get("staff_report_files", [])

    if not agenda_pdf_url:
        # Try h1.title based overview (PZ/ADJ style)
        from scraper.county.pz import parse_pz_overview
        pz_overview = parse_pz_overview(html, meeting_url, base_for_url)
        if pz_overview:
            agenda_pdf_url = pz_overview.get("agenda_pdf_url", "")
            staff_report_files = pz_overview.get("staff_report_files", [])
            if agenda_pdf_url:
                # Rewrite domain from www.maricopa.gov to mcdot.maricopa.gov if needed
                agenda_pdf_url = _rewrite_drain_domain(agenda_pdf_url)
                for srf in staff_report_files:
                    srf["document_url"] = _rewrite_drain_domain(srf.get("document_url", ""))
                print(f"    Found agenda overview via h1.title fallback")

    if not agenda_pdf_url:
        # Check for "No Agenda" — simple overview with no items
        if re.search(r"No\s+Agenda", html, re.I):
            print(f"    Meeting has no agenda (No Agenda indicator)")
            return []

        # Fallback: scan the page for any link with "agenda" in href or text,
        # looking on the mcdot domain
        for m in re.finditer(
            r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.I
        ):
            href = m.group(1)
            link_text = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            if re.search(r"agenda", href + " " + link_text, re.I) and "ViewFile/Item/" in href:
                agenda_pdf_url = urllib.parse.urljoin(base_for_url, href)
                break
        if not agenda_pdf_url:
            # Final fallback: try the direct PDF URL
            # Some meetings have a direct ViewFile/Agenda/{id} PDF without ?html=true
            base_id_match = re.search(r'/ViewFile/Agenda/([^?]+)', meeting_url)
            if base_id_match:
                direct_pdf_url = urllib.parse.urljoin(
                    base_for_url,
                    f"{DRAIN_AGENDA_BASE}{base_id_match.group(1)}"
                )
                # Try to see if this is a PDF by fetching just the head
                try:
                    pdf_req = urllib.request.Request(
                        direct_pdf_url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
                        method="HEAD",
                    )
                    with urllib.request.urlopen(pdf_req, timeout=15) as pdf_resp:
                        content_type = pdf_resp.headers.get("Content-Type", "")
                        if "pdf" in content_type.lower():
                            agenda_pdf_url = direct_pdf_url
                            print(f"    Found direct PDF agenda via HEAD check")
                except Exception:
                    pass

        found_count = len(staff_report_files) if staff_report_files else 0
        if not agenda_pdf_url:
            if found_count > 0:
                print(f"    No agenda PDF found in overview (found {found_count} staff reports)")
            else:
                print(f"    No agenda PDF, staff reports, or items found")
            return []

    if not agenda_pdf_url:
        print(f"    No agenda PDF found on overview page")
        return []

    # Download the agenda PDF directly (not via Playwright click)
    pdf_path_str = f"/tmp/drain_agenda_{Path(meeting_url).stem}.pdf"
    pdf_path = Path(pdf_path_str)
    try:
        pdf_req = urllib.request.Request(
            agenda_pdf_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
        )
        with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
            pdf_path.write_bytes(pdf_resp.read())
        if not pdf_path.exists() or pdf_path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded file is {pdf_path.stat().st_size} bytes, too small")
    except Exception as e:
        print(f"    Failed to download agenda PDF: {e}")
        Path(pdf_path_str).unlink(missing_ok=True)
        return []

    # Parse the agenda PDF for real items
    pdf_items = parse_drain_agenda_pdf(pdf_path_str)
    pdf_path.unlink(missing_ok=True)

    if not pdf_items:
        print(f"    Agenda PDF parse returned no items (procedural/minimal agenda)")
        return []

    # Build real agenda item dicts from PDF data
    items: list[dict] = []
    for pi in pdf_items:
        item_num = pi.get("agenda_item_number", 0)
        case_number = (pi.get("case_number") or "").strip()
        title = (pi.get("project_name") or f"Case {case_number}" if case_number else f"Agenda Item #{item_num}")

        item = {
            "source_body": "Drainage Review Board",
            "meeting_id": "",
            "meeting_date": "",
            "meeting_type": "Drainage Review Board",
            "agenda_item_number": str(item_num),
            "agenda_item_id": "",
            "agenda_item_title": title,
            "agenda_item_text": f"Case: {case_number}" if case_number else "",
            "agenda_item_url": meeting_url,
            "vote_or_action": "",
            "source_url": meeting_url,
            "c_number": "",
            "c_number_base": "",
            "c_number_revision": None,
            "case_number": case_number,
            "supporting_doc_dicts": [],
            "staff_report_url": None,
        }
        items.append(item)

    # Build (all_case_numbers) -> staff report file lookups
    file_case_to_files: dict[str, list[dict]] = {}
    for srf in staff_report_files:
        all_cases = srf.get("all_case_numbers", [])
        primary_case = (srf.get("c_number") or "").upper()
        if primary_case and primary_case not in all_cases:
            all_cases.append(primary_case)
        if not all_cases and primary_case:
            all_cases = [primary_case]
        for cn in all_cases:
            file_case_to_files.setdefault(cn.upper(), []).append(srf)
        if primary_case and primary_case not in all_cases:
            file_case_to_files.setdefault(primary_case, []).append(srf)

    # Match staff report files to items by case number
    for it in items:
        it_case = (it.get("case_number") or "").upper()
        if it_case and it_case in file_case_to_files:
            for srf in file_case_to_files[it_case]:
                sd = {
                    "agenda_item_number": int(it["agenda_item_number"]),
                    "agenda_item_id": int(it["agenda_item_number"]),
                    "c_number": srf.get("c_number"),
                    "document_title": srf.get("document_title", ""),
                    "document_url": srf.get("document_url", ""),
                    "document_type": "PDF",
                    "file_name": f"{srf.get('document_title', '')}.pdf",
                    "file_extension": "pdf",
                }
                existing_urls = {d["document_url"] for d in it["supporting_doc_dicts"]}
                if sd["document_url"] not in existing_urls:
                    it["supporting_doc_dicts"].append(sd)
                if it.get("staff_report_url") is None:
                    it["staff_report_url"] = srf.get("document_url", "")

    return items


def parse_drain_overview(html: str, source_url: str, base_url: str) -> dict | None:
    """Parse a Drainage Review Board AgendaCenter overview/document-index page.

    Drain overview pages use <div class="item level1"> with <span class="title">
    instead of <h1 class="title"> used by PZ/ADJ.

    Structure:
      <div class="item level1">
          <span class="title">May 11, 2011 Drainage Board Agenda</span>
          <span class="file"><a class="file" href="/AgendaCenter/ViewFile/Item/1234">Agenda.pdf</a></span>
      </div>
      <div class="item level1">
          <span class="title">Item 1 - Some Case (D2011001)</span>
          <span class="file"><a class="file" href="/AgendaCenter/ViewFile/Item/1235">Staff Report.pdf</a></span>
      </div>

    Returns:
      {
        "agenda_pdf_url": str | "",
        "agenda_title": str | "",
        "staff_report_files": list[dict],
      }
    Returns None if no item.level1 structure found.
    """
    item_blocks: list[tuple[str, str]] = []

    # Match <div class="item level1"> blocks with <span class="title">
    for m in re.finditer(
        r'<div[^>]*class="[^"]*\bitem\b[^"]*\blevel1\b[^"]*"[^>]*>'
        r'(.*?)</div>',
        html, re.DOTALL | re.I
    ):
        block_html = m.group(1)

        # Find the title span
        title_m = re.search(
            r'<span[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</span>',
            block_html, re.DOTALL | re.I
        )
        if not title_m:
            continue
        title_html = title_m.group(1).strip()
        section = block_html[title_m.end():].strip()

        item_blocks.append((title_html, section))

    if not item_blocks:
        # Try a simpler regex if the more precise one failed
        for m in re.finditer(
            r'<span[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</span>',
            html, re.DOTALL | re.I
        ):
            title_html = m.group(1).strip()
            # Get content after this span until next span.title or end
            rest = html[m.end():]
            next_title = re.search(r'<span[^>]*class="[^"]*\btitle\b[^"]*"', rest)
            if next_title:
                section = rest[:next_title.start()].strip()
            else:
                section = rest.strip()
            item_blocks.append((title_html, section))

    if not item_blocks:
        return None

    agenda_pdf_url = ""
    agenda_title = ""
    staff_report_files: list[dict] = []

    for title_html, section in item_blocks:
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = unescape(re.sub(r"\s+", " ", title)).strip()
        if not title:
            continue

        # Skip GoToWebinar
        if re.search(r"GoToWebinar|Webinar User Guide", title, re.I):
            continue

        # Extract ALL case numbers from the title
        title_cases: list[str] = []
        for c_m in CASE_PATTERN.finditer(title):
            cn = c_m.group(1).upper()
            if cn not in title_cases:
                title_cases.append(cn)

        # Find ALL document links in this section
        doc_urls: list[tuple[str, str]] = []
        for a_m in re.finditer(
            r'<a[^>]*class="[^"]*file[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            section, re.DOTALL | re.I
        ):
            href = a_m.group(1)
            link_text = re.sub(r"<[^>]+>", " ", a_m.group(2))
            link_text = re.sub(r"\s+", " ", link_text).strip()
            if "/ViewFile/Item/" in href:
                doc_url = urllib.parse.urljoin(base_url, href)
                doc_url = _rewrite_drain_domain(doc_url)
                doc_urls.append((doc_url, link_text))

        if not doc_urls:
            continue

        # Identify the agenda document vs staff reports
        has_no_case = not title_cases
        is_agenda = (
            bool(re.search(r"agenda", title, re.I))
            or (has_no_case and bool(re.search(
                r"(january|february|march|april|may|june|july|august|september|october|november|december)",
                title, re.I | re.X
            )))
        )

        if is_agenda:
            agenda_pdf_url = doc_urls[0][0]
            agenda_title = title
        else:
            for doc_url, link_text in doc_urls:
                file_case = ""
                dc = CASE_PATTERN.search(doc_url + " " + link_text)
                if dc:
                    file_case = dc.group(1).upper()
                doc_case = file_case or (title_cases[0] if title_cases else "")

                staff_report_files.append({
                    "document_title": link_text.replace(".pdf", "").replace(".PDF", "").strip() or title,
                    "document_url": doc_url,
                    "c_number": doc_case if doc_case else None,
                    "all_case_numbers": title_cases.copy(),
                })

    return {
        "agenda_pdf_url": agenda_pdf_url,
        "agenda_title": agenda_title,
        "staff_report_files": staff_report_files,
    }


def parse_drain_agenda_pdf(filepath: str) -> list[dict]:
    """Parse a Drainage Review Board Agenda PDF and extract structured fields.

    DRB agenda PDF format (similar to ADJ but simpler):
        1. D2011001 - Project Name
           Location: APN ...
           Description: ...
           Staff Recommendation: ...

    Early years (2011-2013) may have minimal or no substantive items.
    Returns empty list if no items found.
    """
    if not filepath or not Path(filepath).exists():
        return []

    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            txt_path = f.name
        subprocess.run(
            ["pdftotext", "-layout", filepath, txt_path],
            capture_output=True, timeout=30,
        )
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        Path(txt_path).unlink(missing_ok=True)
    except Exception:
        return []

    lines = text.split("\n")
    items: list[dict] = []
    current: dict | None = None
    last_set_field: str | None = None

    FIELD_PATTERNS = {
        "request": re.compile(r"(Description|Request)\s*:\s*(.*)", re.I),
        "location": re.compile(r"Location\s*:\s*(.*)", re.I),
        "recommendation": re.compile(r"(Staff\s+)?Recommendation\s*:?\s*(.*)", re.I),
        "presented_by": re.compile(r"Presented\s+by\s*:?\s*(.*)", re.I),
    }

    # Pattern for item start with inline case number (drain format):
    # "1. D2011001 - Project Name"  or  "1. D2011001 Project Name"
    ITEM_START = re.compile(
        r"^\s*(\d+)\.\s+(D\d{4,7})\s*[-–—]?\s*(.*?)(?:\s+District\s+\d+)?\s*$",
        re.I,
    )

    # Pattern for simple numbered items (no case number):
    # "1.  Call to Order" or "1. Agenda Item"
    ITEM_SIMPLE = re.compile(r"^\s*(\d+)\.\s+(.*)", re.I)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip section headings and boilerplate
        if re.match(
            r"^(Continuance|Consent|Regular|Study|Public Hearing|Other\s+Matters|Adjournment|"
            r"DRB\s*&?\s*BOA\s+Agenda|Page\s+\d+\s+of)",
            stripped, re.I,
        ):
            last_set_field = None
            continue

        # Detect numbered item start with inline case number
        item_m = ITEM_START.match(stripped)
        if item_m:
            if current:
                items.append(current)
            item_num = int(item_m.group(1))
            case_number = item_m.group(2).upper()
            project_name = item_m.group(3).strip() or None
            current = {
                "agenda_item_number": item_num,
                "case_number": case_number,
                "project_name": project_name,
                "request": None,
                "location": None,
                "recommendation": None,
                "presented_by": None,
            }
            last_set_field = "project_name"
            continue

        # Detect simple numbered items (no case number)
        if current is None:
            simple_m = ITEM_SIMPLE.match(stripped)
            if simple_m:
                if current:
                    items.append(current)
                item_num = int(simple_m.group(1))
                project_name = simple_m.group(2).strip()
                current = {
                    "agenda_item_number": item_num,
                    "case_number": "",
                    "project_name": project_name,
                    "request": None,
                    "location": None,
                    "recommendation": None,
                    "presented_by": None,
                }
                last_set_field = "project_name"
                continue

        if current is None:
            continue

        # Match field patterns
        matched = False
        for field, pattern in FIELD_PATTERNS.items():
            if current.get(field):
                continue
            m = pattern.search(stripped)
            if m:
                # Determine group: Description/Request use group(2), Recommendation uses group(2)
                val = m.group(2) if m.lastindex >= 2 else m.group(1)
                val = (val or "").strip()
                if val:
                    current[field] = val
                    last_set_field = field
                matched = True
                break

        if not matched:
            # Continuation text: append to the most recently set field
            if last_set_field and current.get(last_set_field):
                existing = current[last_set_field]
                current[last_set_field] = (existing + " " + stripped).strip()

    if current:
        items.append(current)

    return items


def _rewrite_drain_domain(url: str) -> str:
    """Rewrite www.maricopa.gov URLs to mcdot.maricopa.gov for drain resources.

    The AgendaCenter base href sometimes points to az-maricopacounty.civicplus.com
    or www.maricopa.gov. For drain resources, the actual content is on
    mcdot.maricopa.gov.
    """
    if not url:
        return url
    # Replace www.maricopa.gov or az-maricopacounty.civicplus.com with mcdot.maricopa.gov
    url = re.sub(r'https?://(?:www\.)?maricopa\.gov', DRAIN_BASE_DOMAIN, url, count=1)
    url = re.sub(r'https?://az-maricopacounty\.civicplus\.com', DRAIN_BASE_DOMAIN, url, count=1)
    return url


def _extract_drain_year_tabs_from_html(html: str) -> list[int]:
    """Extract unique year numbers from AgendaCenter year-tab links.

    Returns sorted list of years found in changeYear() anchor hrefs,
    e.g. [2011, 2012, 2013] for CID=19 (Drain).
    """
    seen: set[int] = set()
    for m in re.finditer(r'changeYear\((\d{4})', html):
        year = int(m.group(1))
        if year not in seen:
            seen.add(year)
    return sorted(seen)


def _format_mm_dd_yyyy(date_iso: str) -> str | None:
    """Convert YYYY-MM-DD to MM/DD/YYYY. Returns None if input is empty."""
    if not date_iso:
        return None
    try:
        d = dt.date.fromisoformat(date_iso)
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    except (ValueError, TypeError):
        # Already in MM/DD/YYYY? Return as-is.
        if re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_iso):
            return date_iso
        return date_iso
