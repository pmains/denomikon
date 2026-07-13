"""
Valley Metro (Regional Transit Authority) meeting and agenda extraction.

Valley Metro's Drupal site is behind Cloudflare, requiring a browser
(Playwright) for page navigation and HTML parsing. However, PDF
documents hosted on DigitalOcean Spaces can be downloaded directly
via HTTP without the browser.

Calendar URL pattern:
  https://www.valleymetro.org/event?category={category}&to={date}&page=N

Meeting detail URL pattern:
  https://www.valleymetro.org/event/{body-slug}/{year}/{month}/{day}

PDF document URLs:
  https://vulcan-production.nyc3.cdn.digitaloceanspaces.com/events/downloads/{filename}
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, date, timedelta
from typing import Optional

from scraper.utils import get_async_playwright

log = logging.getLogger(__name__)

JURISDICTION_ID = 25
SOURCE_SYSTEM = "valleymetro-drupal"
SOURCE_INSTANCE_URL = "https://www.valleymetro.org"

BASE_URL = "https://www.valleymetro.org"

# ── Category map ──
# Valley Metro uses category filters on /event pages.
# category → (display_name, default_body_code)
CATEGORIES: dict[str, tuple[str, str]] = {
    "board-meetings": ("Board Meetings", "valley-metro-bod"),
    "management-committees": ("Management Committees", "valley-metro-management"),
    "public-meetings-hearings": ("Public Meetings & Hearings", "valley-metro-public"),
}

# BODY_MAP: meeting title/event keywords → (slug, body_code)
BODY_MAP: dict[str, tuple[str, str]] = {
    "board of directors": ("valley-metro-board-of-directors", "valley-metro-bod"),
    "board meeting": ("valley-metro-board-of-directors", "valley-metro-bod"),
    "procurement": ("valley-metro-procurement", "valley-metro-procurement"),
    "procurement & business practices": ("valley-metro-procurement", "valley-metro-procurement"),
    "joint boards": ("valley-metro-joint-boards", "valley-metro-joint-boards"),
    "joint boards subcommittee": ("valley-metro-joint-boards", "valley-metro-joint-boards"),
    "management committee": ("valley-metro-management-committee", "valley-metro-management"),
    "operations committee": ("valley-metro-operations-committee", "valley-metro-operations"),
    "planning committee": ("valley-metro-planning-committee", "valley-metro-planning"),
    "finance committee": ("valley-metro-finance-committee", "valley-metro-finance"),
    "public hearing": ("valley-metro-public-hearing", "valley-metro-public"),
    "public meeting": ("valley-metro-public-meeting", "valley-metro-public"),
    "community meeting": ("valley-metro-community-meeting", "valley-metro-public"),
}

DEFAULT_BODY_SLUGS = [
    "valley-metro-board-of-directors",
    "valley-metro-procurement",
    "valley-metro-joint-boards",
]


def _resolve_body(title: str) -> tuple[str, str]:
    """Match a meeting title to our slug and body_code."""
    key = title.lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "valley-metro-board-of-directors", "valley-metro-bod"


def _parse_date_from_title(title: str) -> Optional[str]:
    """Try to extract a date from event title like 'Board Meeting - January 15, 2026'."""
    import re
    months = (
        r"(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December)"
    )
    m = re.search(rf"({months})\s+(\d{{1,2}}),?\s*(\d{{4}})?", title)
    if m:
        month_name = m.group(1)
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        month_num = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }.get(month_name.lower(), 1)
        return f"{year:04d}-{month_num:02d}-{day:02d}"
    return None


async def fetch_events_via_browser(
    category: str,
    start_date: str,
    end_date: str,
    headed: bool = False,
) -> list[dict]:
    """Fetch event listing pages for a given category using Playwright browser.

    Valley Metro uses Cloudflare, so standard HTTP requests won't work.
    We use Playwright to render the Drupal event listing page and extract
    event entries.

    Returns list of event dicts with keys:
      - event_url      : Full URL to event detail page
      - title          : Event title
      - date           : Event date (YYYY-MM-DD) or empty string
      - time           : Start time or empty string
      - location       : Location or empty string
      - body_slug      : Resolved body slug
      - body_code      : Resolved body code
      - category       : The event category
    """
    events_by_url: dict[str, dict] = {}

    async_playwright = get_async_playwright()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(60000)

        to_date = end_date
        page_num = 1

        while True:
            cal_url = (
                f"{BASE_URL}/event"
                f"?category={category}"
                f"&to={to_date}"
                f"&page={page_num}"
            )
            log.info(f"Fetching Valley Metro events: category={category}, page={page_num}")

            try:
                await page.goto(cal_url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                log.warning(f"Failed to load page {cal_url}: {e}")
                break

            # Check for "No results" message
            no_results = await page.query_selector(".view-empty, .empty, .no-results")
            if no_results:
                log.info(f"No more results for category={category}")
                break

            # Extract events from the listing page
            events = await _extract_events_from_listing(page, category)
            if not events:
                log.info(f"No events found on page {page_num}")
                # Check: do we have pagination?
                current_page_el = await page.query_selector(
                    ".pagination .page-item.active, .pager .is-active, .pagination .active"
                )
                if current_page_el:
                    page_num += 1
                    continue
                break

            for ev in events:
                url = ev.get("event_url", "")
                if url and url not in events_by_url:
                    events_by_url[url] = ev

            # Check if there's a "next" page link
            next_link = await page.query_selector(
                "a[rel='next'], .pagination .next a, .pager--next a, a:has-text('Next')"
            )
            if not next_link:
                # Also check page links: if current page < max page
                page_links = await page.query_selector_all(
                    ".pagination .page-item a, .pager__item a"
                )
                page_numbers = []
                for pl in page_links:
                    txt = await pl.inner_text()
                    txt = txt.strip()
                    try:
                        n = int(txt)
                        page_numbers.append(n)
                    except ValueError:
                        continue
                if page_numbers and page_num >= max(page_numbers):
                    break
                elif not page_links:
                    break
                else:
                    page_num += 1
            else:
                page_num += 1

        await browser.close()

    events = list(events_by_url.values())
    log.info(f"Total events for category={category}: {len(events)}")
    return events


async def _extract_events_from_listing(
    page,
    category: str,
) -> list[dict]:
    """Parse event listing page HTML to extract individual event entries.

    Drupal event listing pages typically structure events in article
    elements or div.event-teaser containers.
    """
    js = """() => {
        const events = [];

        // Method 1: Look for article.event-teaser or div.event-teaser elements
        const teasers = document.querySelectorAll(
            'article.event-teaser, div.event-teaser, .views-row, .node--type-event'
        );

        teasers.forEach((teaser) => {
            // Title and link
            const titleLink = teaser.querySelector('h2 a, h3 a, .event-title a, .node__title a, a[rel="bookmark"]');
            if (!titleLink) return;
            const title = (titleLink.textContent || '').trim();
            const href = titleLink.getAttribute('href') || '';
            const eventUrl = href.startsWith('http') ? href : 'https://www.valleymetro.org' + href;

            // Date
            const dateEl = teaser.querySelector(
                '.event-date, .datetime, time, .field--name-field-event-date .field__item, .date-display-single'
            );
            const dateText = dateEl ? (dateEl.textContent || dateEl.getAttribute('datetime') || '').trim() : '';

            // Time
            const timeEl = teaser.querySelector('.event-time, .field--name-field-event-time .field__item');
            const timeText = timeEl ? (timeEl.textContent || '').trim() : '';

            // Location
            const locEl = teaser.querySelector('.event-location, .field--name-field-location .field__item');
            const locText = locEl ? (locEl.textContent || '').trim() : '';

            events.push({
                title: title,
                event_url: eventUrl,
                date_text: dateText,
                time: timeText,
                location: locText,
            });
        });

        // Method 2: Fallback — scan all links that look like event paths
        if (events.length === 0) {
            const allLinks = document.querySelectorAll('a[href*="/event/"]');
            const seen = new Set();
            allLinks.forEach((link) => {
                const href = link.getAttribute('href') || '';
                // Filter for event detail pages (not listing pages)
                if (!href.match(/\\/event\\/[^\\/]+\\/\\d{4}\\/\\d{2}\\/\\d{2}/)) return;
                if (seen.has(href)) return;
                seen.add(href);
                const title = (link.textContent || '').trim();
                const eventUrl = href.startsWith('http') ? href : 'https://www.valleymetro.org' + href;

                // Find parent container for date info
                let parent = link.closest('tr, li, .views-row, article, div');
                const dateEl = parent ? parent.querySelector('time, .datetime, .date-display-single') : null;
                const dateText = dateEl ? (dateEl.textContent || dateEl.getAttribute('datetime') || '').trim() : '';

                events.push({
                    title: title,
                    event_url: eventUrl,
                    date_text: dateText,
                    time: '',
                    location: '',
                });
            });
        }

        return JSON.stringify(events);
    }"""

    raw = await page.evaluate(js)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Failed to parse event listing JSON from browser")
        return []

    results = []
    for ev in parsed:
        date_val = ev.get("date_text", "")
        # Try to extract date from the event URL as fallback
        if not date_val:
            url_date = _extract_date_from_url(ev.get("event_url", ""))
            if url_date:
                date_val = url_date

        # Try to parse the date_text into YYYY-MM-DD
        parsed_date = _normalize_date(ev.get("date_text", ""))
        if not parsed_date:
            # Try from title
            parsed_date = _parse_date_from_title(ev.get("title", ""))

        slug, code = _resolve_body(ev.get("title", ""))

        results.append({
            "event_url": ev.get("event_url", ""),
            "title": ev.get("title", ""),
            "date": parsed_date or date_val,
            "time": ev.get("time", ""),
            "location": ev.get("location", ""),
            "body_slug": slug,
            "body_code": code,
            "category": category,
        })

    return results


def _extract_date_from_url(url: str) -> str:
    """Extract date from event URL like /event/board-of-directors/2026/01/15."""
    m = re.search(r"/event/[^/]+/(\d{4})/(\d{2})/(\d{2})", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _normalize_date(date_text: str) -> str:
    """Parse various date text formats to YYYY-MM-DD.

    Handles formats like:
      - January 15, 2026
      - 2026-01-15
      - 01/15/2026
      - Jan 15, 2026
    """
    if not date_text:
        return ""

    # Already ISO format
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_text)
    if m:
        return date_text[:10]

    # MM/DD/YYYY
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", date_text)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"

    # Named month
    months = (
        r"(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December|"
        r"Jan\.?|Feb\.?|Mar\.?|Apr\.?|May|Jun\.?|Jul\.?|Aug\.?|"
        r"Sep\.?|Oct\.?|Nov\.?|Dec\.?)"
    )
    m = re.search(rf"({months})\s+(\d{{1,2}}),?\s*(\d{{4}})?", date_text)
    if m:
        month_str = m.group(1).replace(".", "").strip()
        day = int(m.group(2))
        year_str = m.group(3) or str(datetime.now().year)
        year = int(year_str)
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month_num = month_map.get(month_str.lower(), 1)
        return f"{year:04d}-{month_num:02d}-{day:02d}"

    return ""


async def fetch_event_detail_via_browser(
    event_url: str,
    headed: bool = False,
) -> dict:
    """Navigate to an event detail page and extract description and document links.

    Returns dict with keys:
      - description      : Event description text (HTML stripped)
      - meeting_packet_url : URL to the meeting packet PDF (or empty)
      - agenda_url       : URL to the agenda PDF (or empty)
      - minutes_url      : URL to the minutes PDF (or empty)
      - supporting_docs  : List of (title, url) tuples for Info & Resources section
      - video_url        : YouTube embed URL (or empty)
    """
    result = {
        "description": "",
        "meeting_packet_url": "",
        "agenda_url": "",
        "minutes_url": "",
        "supporting_docs": [],
        "video_url": "",
    }

    async_playwright = get_async_playwright()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            await page.goto(event_url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"Failed to load event detail page {event_url}: {e}")
            await browser.close()
            return result

        # Extract detail page content via JavaScript
        js = """() => {
            const data = {
                description: '',
                meeting_packet_url: '',
                agenda_url: '',
                minutes_url: '',
                supporting_docs: [],
                video_url: '',
            };

            // Description — from the event body
            const descEl = document.querySelector(
                '.event-description, .field--name-body, .node__content, ' +
                '.field--name-field-description, main article .content, ' +
                '.layout-content .field--text'
            );
            if (descEl) {
                data.description = (descEl.textContent || '').trim();
            }

            // Info & Resources section — find links with downloadable PDFs
            // Look for sections/headings that contain 'Info & Resources' or 'Meeting Materials'
            const headings = document.querySelectorAll('h2, h3, h4');
            let foundResources = false;
            headings.forEach((h) => {
                const text = (h.textContent || '').toLowerCase();
                if (text.includes('info') || text.includes('resource') ||
                    text.includes('meeting material') || text.includes('download') ||
                    text.includes('documents') || text.includes('attachments')) {
                    // Look at the sibling element (usually a div or ul) after this heading
                    let sibling = h.nextElementSibling;
                    if (!sibling) return;
                    // Try to find all links in this section
                    const sectionLinks = sibling.querySelectorAll('a[href]');
                    if (sectionLinks.length === 0) {
                        // The content might be in a parent div
                        const parent = h.closest('.field__item, div');
                        if (parent) {
                            const parentLinks = parent.querySelectorAll('a[href]');
                            foundResources = true;
                            parentLinks.forEach((link) => {
                                const href = link.getAttribute('href') || '';
                                const title = (link.textContent || '').trim();
                                if (href && !href.startsWith('#')) {
                                    data.supporting_docs.push({title: title, url: href});
                                }
                            });
                        }
                        foundResources = true;
                        return;
                    }
                    foundResources = true;
                    sectionLinks.forEach((link) => {
                        const href = link.getAttribute('href') || '';
                        const title = (link.textContent || '').trim();
                        if (href && !href.startsWith('#')) {
                            data.supporting_docs.push({title: title, url: href});
                        }
                    });
                }
            });

            // Fallback: scan ALL links on the page for PDFs
            if (!foundResources) {
                const allLinks = document.querySelectorAll('a[href]');
                allLinks.forEach((link) => {
                    const href = link.getAttribute('href') || '';
                    const title = (link.textContent || '').trim();
                    // DigitalOcean Spaces PDFs
                    if (href.includes('digitaloceanspaces.com') && href.toLowerCase().endsWith('.pdf')) {
                        data.supporting_docs.push({title: title, url: href});
                    }
                });
            }

            // Categorize known document types
            const knownTypes = {
                'meeting packet': 'meeting_packet_url',
                'agenda packet': 'meeting_packet_url',
                'agenda': 'agenda_url',
                'minutes': 'minutes_url',
                'meeting minutes': 'minutes_url',
            };

            data.supporting_docs.forEach((doc) => {
                const t = doc.title.toLowerCase().trim();
                for (const [keyword, field] of Object.entries(knownTypes)) {
                    if (t.includes(keyword) && !data[field]) {
                        data[field] = doc.url;
                        break;
                    }
                }
            });

            // YouTube embed
            const ytIframe = document.querySelector(
                'iframe[src*="youtube.com"], iframe[src*="youtu.be"]'
            );
            if (ytIframe) {
                data.video_url = ytIframe.getAttribute('src') || '';
            }

            return JSON.stringify(data);
        }"""

        raw = await page.evaluate(js)
        try:
            parsed = json.loads(raw)
            result.update(parsed)
        except (json.JSONDecodeError, TypeError) as e:
            log.warning(f"Failed to parse event detail JSON for {event_url}: {e}")

        await browser.close()

    return result


def download_document(pdf_url: str, timeout: int = 30) -> Optional[bytes]:
    """Download a PDF from DigitalOcean Spaces directly via HTTP.

    These PDFs are hosted on DO Spaces (not behind Cloudflare), so
    they can be fetched directly without the browser.
    """
    import urllib.request

    if not pdf_url.startswith("http"):
        pdf_url = urllib.parse.urljoin(BASE_URL, pdf_url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    req = urllib.request.Request(pdf_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.warning(f"Failed to download document {pdf_url[:80]}: {e}")
        return None


async def search_valley_metro_meetings(
    start_date: str,
    end_date: str,
    categories: Optional[list[str]] = None,
    headed: bool = False,
) -> list[dict]:
    """Search Valley Metro meetings for a given date range across categories.

    Returns list of meeting dicts compatible with replace_meeting_data_safe:
      - meeting_id       : URL-based ID (slugified event_url path)
      - meeting_date     : YYYY-MM-DD
      - meeting_type     : e.g. "Regular Meeting"
      - meeting_title    : Event title
      - body_name        : Event title
      - body_slug        : Normalized body slug
      - body_code        : Short body code
      - source_url       : Event detail URL
      - canceled         : False
    """
    if categories is None:
        categories = ["board-meetings"]

    all_meetings: list[dict] = []
    seen_ids: set[str] = set()

    for cat in categories:
        events = await fetch_events_via_browser(cat, start_date, end_date, headed=headed)
        for ev in events:
            # Generate a stable meeting_id from the URL path
            url_path = urllib.parse.urlparse(ev["event_url"]).path.rstrip("/")
            meeting_id = url_path.replace("/event/", "").replace("/", "-")

            if meeting_id in seen_ids:
                continue
            seen_ids.add(meeting_id)

            meeting_date = ev.get("date", "")
            meeting_type = "Regular Meeting"

            all_meetings.append({
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": ev["title"],
                "body_name": ev["title"],
                "body_slug": ev["body_slug"],
                "body_code": ev["body_code"],
                "source_url": ev["event_url"],
                "canceled": False,
            })

    log.info(f"Found {len(all_meetings)} Valley Metro meeting(s) for date range {start_date} to {end_date}")
    return all_meetings


def extract_meeting_type(title: str) -> str:
    """Extract meeting type from the event title."""
    tl = title.lower()
    if "cancellation" in tl or "canceled" in tl or "cancelled" in tl:
        return "Cancelled"
    if "special meeting" in tl:
        return "Special Meeting"
    if "public hearing" in tl:
        return "Public Hearing"
    if "workshop" in tl or "work session" in tl:
        return "Work Session"
    if "executive session" in tl:
        return "Executive Session"
    return "Regular Meeting"


def extract_agenda_items_from_packet(pdf_url: str, meeting_id: str = "vm") -> list[dict]:
    """Download a meeting packet PDF, extract text, and parse into agenda items.

    Args:
        pdf_url: URL of the packet PDF
        meeting_id: Used to generate unique agenda_item_id per meeting
                     (default: "vm") to avoid constraint violations.

    Returns a list of agenda_item dicts compatible with replace_meeting_data_safe:
        agenda_item_number: str
        agenda_item_id: str (unique per meeting, derived from meeting_id + number)
        agenda_item_title: str
        vote_or_action: str
    """
    import re

    pdf_bytes = download_document(pdf_url)
    if not pdf_bytes:
        log.warning(f"Could not download packet: {pdf_url[:80]}")
        return []

    text = _pdf_bytes_to_text(pdf_bytes)
    if not text:
        log.warning(f"Could not extract text from packet: {pdf_url[:80]}")
        return []

    return _parse_valley_metro_items(text, meeting_id)


def _pdf_bytes_to_text(pdf_bytes: bytes) -> str | None:
    """Extract text from PDF bytes using fitz or pdftotext or OCR."""
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        import fitz
        doc = fitz.open(tmp_path)
        text = "\n".join(page.get_text() or "" for page in doc)
        doc.close()
        if text.strip():
            os.unlink(tmp_path)
            return text
    except Exception:
        pass

    try:
        import subprocess
        r = subprocess.run(["pdftotext", "-layout", tmp_path, "-"],
                          capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            os.unlink(tmp_path)
            return r.stdout
    except Exception:
        pass

    try:
        import fitz, pytesseract
        from PIL import Image
        import io
        doc = fitz.open(tmp_path)
        text = "\n".join(
            pytesseract.image_to_string(
                Image.open(io.BytesIO(page.get_pixmap(dpi=300).tobytes("png"))),
                lang="eng"
            ) for page in doc
        )
        doc.close()
        os.unlink(tmp_path)
        return text
    except Exception:
        pass

    os.unlink(tmp_path)
    return None


def _parse_valley_metro_items(text: str, meeting_id: str = "vm") -> list[dict]:
    """Parse Valley Metro packet PDF text into agenda item dicts.

    The PDF contains multiple meeting agendas in one file (Joint Boards,
    RPTA, Rail). We extract the first contiguous block of numbered items,
    which is the primary meeting agenda.
    """
    import re

    # Find ALL item headers in the document
    item_re = re.compile(r"^(\d+[A-Z]?)\.\s+(.+)$", re.MULTILINE)
    action_re = re.compile(r"^(\d+[A-Z]?)\.\s+For\s+(action|information)\s*$", re.IGNORECASE | re.MULTILINE)

    # Find action type lines and index by item number
    actions: dict[str, str] = {}
    for m in action_re.finditer(text):
        actions[m.group(1)] = m.group(2).lower()  # "action" or "information"

    # Find all item headers, skip page numbers and "For" continuations
    all_items: list[tuple[str, str, int]] = []  # (number, title, position)
    for m in item_re.finditer(text):
        num, rest = m.group(1), m.group(2).strip()
        if num.isdigit() and int(num) > 999:
            continue  # page numbers
        if rest.lower().startswith("for "):
            continue  # "1. For Information" — matches action, not header
        if rest.startswith("\x0c"):
            continue
        all_items.append((num, rest, m.start()))

    if not all_items:
        return []

    # Find the longest contiguous block starting from position 0.
    # This handles the "multiple agendas in one PDF" structure.
    # We look for items where numbers ascend naturally.
    # The first block is usually the primary meeting agenda.

    def _is_next(a: str, b: str) -> bool:
        """Check if b follows a in the item numbering scheme."""
        # Handle 4A -> 4B, 9 -> 10, 4 -> 5, 4F -> 5
        a_parts = re.match(r"(\d+)([A-Z]?)", a)
        b_parts = re.match(r"(\d+)([A-Z]?)", b)
        if not a_parts or not b_parts:
            return False
        a_num, a_let = int(a_parts.group(1)), a_parts.group(2)
        b_num, b_let = int(b_parts.group(1)), b_parts.group(2)
        if a_num == b_num and a_let and b_let:
            return ord(b_let) - ord(a_let) == 1  # 4A -> 4B
        if a_num == b_num and not a_let and b_let:
            return b_let == 'A'  # 4 -> 4A (within same number)
        if b_num - a_num == 1:
            return True  # 4 -> 5, 9 -> 10
        return False

    # Find the first block — from item[0] until the sequence breaks
    block = [all_items[0]]
    for i in range(1, len(all_items)):
        prev_num = all_items[i - 1][0]
        curr_num = all_items[i][0]
        if _is_next(prev_num, curr_num) or prev_num == curr_num:
            block.append(all_items[i])
        else:
            break  # sequence broke — this is a new agenda

    if len(block) < 3:
        block = all_items[:12]  # fallback: take first 12 items

    # Build item dicts from the block
    items: list[dict] = []
    seen: set[str] = set()
    for num, title, pos in block:
        if num in seen:
            continue
        seen.add(num)
        action = actions.get(num, "").capitalize() if actions.get(num) else ""
        desc = f"For {action}" if action else ""
        items.append({
            "agenda_item_number": num,
            "agenda_item_id": f"{meeting_id}-{num}",
            "agenda_item_title": title,
            "vote_or_action": desc,
        })

    return items


def main() -> None:
    """CLI entry point for testing."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "events":
        category = sys.argv[2] if len(sys.argv) > 2 else "board-meetings"
        start = sys.argv[3] if len(sys.argv) > 3 else "2026-01-01"
        end = sys.argv[4] if len(sys.argv) > 4 else "2026-06-30"
        headed = "--headed" in sys.argv

        import asyncio
        events = asyncio.run(fetch_events_via_browser(category, start, end, headed=headed))
        print(f"\nFound {len(events)} events for category={category}:")
        for ev in events:
            print(f"  {ev['date']:12s} | {ev['body_code']:30s} | {ev['title'][:60]}")
            print(f"            URL: {ev['event_url']}")
        print()

    elif len(sys.argv) > 1 and sys.argv[1] == "detail":
        url = sys.argv[2]
        headed = "--headed" in sys.argv

        import asyncio
        detail = asyncio.run(fetch_event_detail_via_browser(url, headed=headed))
        print(f"\nEvent detail for {url}:")
        print(f"  Description: {detail['description'][:200]}...")
        print(f"  Agenda URL: {detail.get('agenda_url', '')}")
        print(f"  Packet URL: {detail.get('meeting_packet_url', '')}")
        print(f"  Minutes URL: {detail.get('minutes_url', '')}")
        print(f"  Documents ({len(detail['supporting_docs'])}):")
        for doc in detail["supporting_docs"]:
            print(f"    - {doc['title']}: {doc['url'][:100]}")
        print()

    elif len(sys.argv) > 1 and sys.argv[1] == "meetings":
        start = sys.argv[2] if len(sys.argv) > 2 else "2026-01-01"
        end = sys.argv[3] if len(sys.argv) > 3 else "2026-06-30"
        headed = "--headed" in sys.argv

        import asyncio
        meetings = asyncio.run(
            search_valley_metro_meetings(
                start, end,
                categories=list(CATEGORIES.keys()),
                headed=headed,
            )
        )
        print(f"\nFound {len(meetings)} Valley Metro meeting(s):")
        for m in meetings:
            print(f"  {m['meeting_date']:12s} | {m['body_code']:35s} | {m['meeting_title'][:55]}")
        print()

    else:
        print("Usage:")
        print("  python -m scraper.valley_metro events [category] [start] [end] [--headed]")
        print("  python -m scraper.valley_metro detail <event_url> [--headed]")
        print("  python -m scraper.valley_metro meetings [start] [end] [--headed]")
        print()
        print("Categories:")
        for cat, (name, _) in CATEGORIES.items():
            print(f"  {cat:35s} - {name}")


if __name__ == "__main__":
    main()
