#!/usr/bin/env python3
"""
MAG (Maricopa Association of Governments) scraper.

MAG's site (azmag.gov) runs DotNetNuke behind Cloudflare. The JSON API and
event detail pages are behind Cloudflare, requiring a browser session.

However:
  - Direct PDF URLs at /Portals/0/Committee-Meetings/* work without Cloudflare
  - The calendar page (Kendo Scheduler) renders event data client-side

This scraper navigates to the calendar page via browser, extracts the rendered
scheduler HTML containing event data and direct PDF URLs, then downloads and
parses PDFs directly via HTTP.
"""
from __future__ import annotations

import sys
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Committee map ──
# cid → (code, slug, display_name, body_type)
COMMITTEES: dict[int, tuple[str, str, str, str]] = {
    2:    ("mag-mc",         "management-committee",           "Management Committee", "committee"),
    5:    ("mag-rc",         "regional-council",               "Regional Council", "council"),
    36:   ("mag-rcec",       "regional-council-exec",          "Regional Council Executive Committee", "committee"),
    37:   ("mag-tpc",        "transportation-policy",           "Transportation Policy Committee", "committee"),
    40:   ("mag-atc",        "active-transportation",           "Active Transportation Committee", "committee"),
    41:   ("mag-bcc",        "building-codes",                  "Building Codes Committee", "committee"),
    43:   ("mag-epdtc",      "elderly-disabled-transportation", "Elderly and Persons with Disabilities Transportation Committee", "committee"),
    44:   ("mag-hstc",       "human-services-technical",        "Human Services Technical Committee", "committee"),
    45:   ("mag-itsc",       "its",                             "Intelligent Transportation Systems Committee", "committee"),
    46:   ("mag-ptac",       "population-technical",            "Population Technical Advisory Committee", "committee"),
    48:   ("mag-rdvc",       "regional-domestic-violence",      "Regional Domestic Violence Council", "council"),
    49:   ("mag-swac",       "solid-waste-advisory",            "Solid Waste Advisory Committee", "committee"),
    50:   ("mag-ssdc",       "standard-specs-details",          "Standard Specifications & Details Committee", "committee"),
    51:   ("mag-stc",        "street",                          "Street Committee", "committee"),
    53:   ("mag-trc",        "transportation-review",           "Transportation Review Committee", "committee"),
    54:   ("mag-tsc",        "transportation-safety",           "Transportation Safety Committee", "committee"),
    55:   ("mag-wqac",       "water-quality-advisory",          "Water Quality Advisory Committee", "committee"),
    129:  ("mag-transit",    "transit",                          "Transit Committee", "committee"),
    3928: ("mag-cocb",       "continuum-of-care",               "Maricopa Regional Continuum of Care Board", "board"),
}

MAG_JURISDICTION_SLUG = "mag"
MAG_BODY_CODES = [v[0] for v in COMMITTEES.values()]

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# ── Browser helpers ──

def _browser_navigate(url: str) -> None:
    subprocess.run(
        ["openclaw", "browser", "navigate", url],
        capture_output=True, text=True, timeout=20,
    )


def _browser_evaluate(js: str, timeout: int = 15) -> str:
    r = subprocess.run(
        ["openclaw", "browser", "evaluate", "--fn", js],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Browser eval error: {r.stderr[:300]}")
    return r.stdout


# ── Step 1: Fetch events from calendar ──

def fetch_mag_events(cid: int, start_date: str, end_date: str) -> list[dict]:
    """Navigate to calendar page, wait for scheduler render, extract events.

    Returns list of event dicts with keys:
      eventID, title, start (ISO date), committeeId, Agenda (URL), Minutes (URL),
      hasAttachments, isCanceled, description
    """
    cal_url = (
        f"https://azmag.gov/About-Us/Calendar/cid/{cid}"
        f"?StartDate={start_date}&EndDate={end_date}"
    )
    _browser_navigate(cal_url)
    time.sleep(2.5)

    # Extract rendered event data from the Kendo scheduler
    js = """() => {
        var rows = document.querySelectorAll('.k-scheduler-table tr[role="row"]');
        var events = [];
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var cells = row.querySelectorAll('td');
            if (cells.length < 3) continue;

            // Date from first cell
            var dayEl = cells[0].querySelector('.k-scheduler-agendaday');
            var weekEl = cells[0].querySelector('.k-scheduler-agendaweek');
            var dateEl = cells[0].querySelector('.k-scheduler-agendadate');
            if (!dayEl || !dateEl) continue;
            var dateText = (dateEl.textContent || '').trim();
            var day = (dayEl.textContent || '').trim();

            // Time from second cell
            var timeText = (cells[1].textContent || '').trim();

            // Event details from third cell
            var taskDiv = cells[2].querySelector('.k-task');
            if (!taskDiv) continue;
            var title = taskDiv.getAttribute('title') || '';
            var uid = taskDiv.getAttribute('data-uid') || '';
            var isCanceled = taskDiv.querySelector('.k-event-cancel') ? true : false;

            // Event detail link
            var link = taskDiv.querySelector('a.k-event-detailLink');
            var eventUrl = link ? link.getAttribute('href') || '' : '';
            var eventId = '';
            var m = eventUrl.match(/\\/Event\\/(\\d+)/);
            if (m) eventId = m[1];

            // Document links
            var agendaLink = '',
                minutesLink = '',
                agendaDocUrl = '',
                minutesDocUrl = '';
            var al = taskDiv.querySelector('a.agenda-link');
            if (al) {
                agendaLink = al.textContent.trim();
                agendaDocUrl = al.getAttribute('href') || '';
            }
            var ml = taskDiv.querySelector('a.minutes-link');
            if (ml) {
                minutesLink = ml.textContent.trim();
                minutesDocUrl = ml.getAttribute('href') || '';
            }

            events.push({
                eventID: eventId,
                title: title.replace(' (Canceled)', ''),
                dateText: dateText + ' ' + day,
                timeText: timeText,
                isCanceled: isCanceled,
                uid: uid,
                AgendaUrl: agendaDocUrl,
                MinutesUrl: minutesDocUrl,
                eventUrl: eventUrl,
            });
        }
        return events;
    }"""

    raw = _browser_evaluate(js, timeout=15)
    events = json.loads(raw)

    # Normalize dates
    for e in events:
        try:
            dt = datetime.strptime(e["dateText"], "%B, %Y %d")
            e["start"] = dt.strftime("%Y-%m-%d")
        except ValueError:
            e["start"] = ""

    return events


# ── Step 2: Extract document URLs from event detail page ──

def fetch_mag_event_docs(event_url: str) -> dict:
    """Navigate to the MAG event detail page and extract document URLs.

    Returns dict with keys:
      agenda_url (str): URL of the agenda PDF (or empty)
      packet_url (str): URL of the agenda packet PDF (or empty)
      minutes_url (str): URL of the minutes PDF (or empty)
    """
    if not event_url.startswith("http"):
        event_url = urllib.parse.urljoin("https://azmag.gov", event_url)

    _browser_navigate(event_url)
    time.sleep(2)

    js = """() => {
        var links = document.querySelectorAll('a');
        var agendaUrl = '', packetUrl = '', minutesUrl = '';
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href') || '';
            var text = (links[i].textContent || '').trim();
            // Match agenda links — but NOT agenda packet links
            if (text.match(/Agenda$/i) && !text.match(/Packet/i) && href.indexOf('LinkClick.aspx') >= 0) {
                agendaUrl = href;
            }
            // Match agenda packet links
            if (text.match(/Agenda Packet/i) && href.indexOf('LinkClick.aspx') >= 0) {
                packetUrl = href;
            }
            // Match minutes links
            if (text.match(/Minutes/i) && href.indexOf('LinkClick.aspx') >= 0) {
                minutesUrl = href;
            }
        }
        return { agenda_url: agendaUrl, packet_url: packetUrl, minutes_url: minutesUrl };
    }"""

    raw = _browser_evaluate(js, timeout=10)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Failed to parse event docs for %s", event_url)
        return {"agenda_url": "", "packet_url": "", "minutes_url": ""}


# ── Step 3: Download PDF directly (no browser) ──

def download_pdf_via_browser(pdf_url: str, referer: str = "") -> Optional[bytes]:
    """Download a MAG PDF via browser XMLHttpRequest (bypasses Cloudflare)."""
    if not pdf_url.startswith("http"):
        pdf_url = urllib.parse.urljoin("https://azmag.gov", pdf_url)
    escaped_url = json.dumps(pdf_url)
    escaped_referer = json.dumps(referer or "https://azmag.gov/About-Us/Calendar")

    js = f"""() => {{
        var xhr = new XMLHttpRequest();
        xhr.open('GET', {escaped_url}, false);
        xhr.overrideMimeType('text/plain; charset=x-user-defined');
        xhr.setRequestHeader('Referer', {escaped_referer});
        try {{ xhr.send(null); }} catch(e) {{ return 'ERROR:' + e.message; }}
        if (xhr.status !== 200) return 'ERROR:HTTP ' + xhr.status;
        var raw = xhr.responseText;
        var binary = '';
        for (var i = 0; i < raw.length; i++)
            binary += String.fromCharCode(raw.charCodeAt(i) & 0xff);
        return 'DATA:' + btoa(binary);
    }}"""

    raw = _browser_evaluate(js, timeout=30)
    if raw.startswith('"DATA:'):
        b64 = raw[6:-1]  # Remove quotes and 'DATA:' prefix
        return base64.b64decode(b64)
    log.debug("download_pdf: %s", raw[:100])
    return None


# ── Step 4: Download a document and save to disk (for supporting docs) ──

def _save_pdf_to_disk(pdf_bytes: bytes, meeting_id: str, label: str, output_dir: str) -> Optional[str]:
    """Write PDF bytes to a local file and return the relative path.

    Args:
        pdf_bytes: Raw PDF content
        meeting_id: Meeting identifier used in filename
        label: Short label like 'agenda' or 'packet'
        output_dir: Directory to write to (typically data/pdfs/)
    Returns:
        Relative path string, or None on failure
    """
    filename = f"mag-{meeting_id}-{label}.pdf"
    os.makedirs(output_dir, exist_ok=True)
    dest = os.path.join(output_dir, filename)
    try:
        with open(dest, "wb") as f:
            f.write(pdf_bytes)
        return dest
    except OSError as e:
        log.warning("Failed to save PDF to %s: %s", dest, e)
        return None


# ── Step 3: Parse agenda items from PDF text ──

def parse_mag_pdf_items(pdf_bytes: bytes, source_url: str = "") -> list[dict]:
    """Extract numbered agenda items from a MAG agenda PDF.

    MAG PDFs use items like:
       1.   Call to Order
       4A.  Regional Transportation Systems Management and Operations Plan

    Returns list of item dicts compatible with replace_meeting_data_safe.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = f.name
        f.write(pdf_bytes)

    text = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            txt_path = f.name
        subprocess.run(
            ["pdftotext", "-layout", pdf_path, txt_path],
            capture_output=True, timeout=30,
        )
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        Path(txt_path).unlink(missing_ok=True)
    except Exception:
        pass
    finally:
        Path(pdf_path).unlink(missing_ok=True)

    if not text:
        return []

    items: list[dict] = []
    seen: set[str] = set()

    def _make(num: str, title: str) -> dict:
        return {
            "source_body": "mag",
            "agenda_item_number": num,
            "agenda_item_title": title[:200],
            "agenda_item_text": title[:500],
            "agenda_item_url": source_url,
            "supporting_doc_dicts": [],
        }

    # Match numbered items: "1." or "4A." or "4B." at line start
    for m in re.finditer(
        r"^\s*(\d+[A-Z]?\.)\s+([A-Za-z0-9\"'\[(][^\n]{3,200}?)$",
        text, re.MULTILINE,
    ):
        num = m.group(1).rstrip(".")
        title = m.group(2).strip()
        if num not in seen and len(title) > 5:
            seen.add(num)
            items.append(_make(num, title))

    return items


# ── Step 4: Persist to database ──

def ensure_mag_public_bodies(session) -> dict[str, int]:
    """Register MAG as a jurisdiction and all committees as public_bodies.

    Returns dict mapping body_code → public_body_id.
    """
    from sqlalchemy import select
    from db.models import PublicBody, Jurisdiction

    # Create MAG jurisdiction
    jur = session.execute(
        select(Jurisdiction).where(Jurisdiction.slug == MAG_JURISDICTION_SLUG)
    ).scalar_one_or_none()
    if not jur:
        jur = Jurisdiction(
            name="Maricopa Association of Governments (MAG)",
            slug=MAG_JURISDICTION_SLUG,
            state="AZ",
        )
        session.add(jur)
        session.flush()

    pb_map: dict[str, int] = {}
    for cid, (code, slug, name, body_type) in COMMITTEES.items():
        existing = session.execute(
            select(PublicBody).where(PublicBody.body_code == code)
        ).scalar_one_or_none()
        if existing:
            pb_map[code] = existing.id
        else:
            pb = PublicBody(
                jurisdiction_id=jur.id,
                name=name,
                slug=slug,
                body_code=code,
                body_type=body_type,
            )
            session.add(pb)
            session.flush()
            pb_map[code] = pb.id

    return pb_map


def sync_mag_committee(
    cid: int,
    start_date: str,
    end_date: str,
    db_map: dict,
    force: bool = False,
    skip_downloads: bool = False,
) -> tuple[int, int]:
    """Sync all meetings for one MAG committee within a date range.

    Returns (meetings_synced, items_found).
    """
    from db.models import Meeting as MeetingModel
    from sqlalchemy import select
    from db.persist import replace_meeting_data_safe, update_sync_status

    committee = COMMITTEES.get(cid)
    if not committee:
        return 0, 0

    body_code, _, display_name, _ = committee

    log.info("%s (cid=%d): fetching events %s to %s", display_name, cid, start_date, end_date)
    events = fetch_mag_events(cid, start_date, end_date)
    if not events:
        log.info("  no events found")
        return 0, 0

    log.info("  found %d events", len(events))

    synced = 0
    items_found = 0

    for event in events:
        event_id = event.get("eventID", "")
        meeting_date = event.get("start", "")
        title = event.get("title", "")
        agenda_url = event.get("AgendaUrl", "")
        event_url = event.get("eventUrl", "")
        is_canceled = event.get("isCanceled", False)

        if not meeting_date or not title:
            continue

        meeting_id = f"{event_id}" if event_id else f"mag-{meeting_date}"

        meeting_dict = {
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "meeting_type": display_name,
            "meeting_title": title,
            "source_url": urllib.parse.urljoin("https://azmag.gov", event_url) if event_url else "",
        }

        session = None
        try:
            from db.core import get_session
            session = get_session()

            # Check if already synced
            db_m = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()

            if db_m and db_m.sync_status in ("complete", "no_agenda") and not force:
                log.debug("  %s %s: %s (skip)", meeting_id, meeting_date, db_m.sync_status)
                session.close()
                continue

            if is_canceled or not agenda_url:
                log.info("  %s %s: canceled or no agenda (no_agenda)", meeting_id, meeting_date)
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                session.commit()
                session.close()
                synced += 1
                continue

            supporting_doc_dicts = []

            # Download the agenda PDF
            if not skip_downloads:
                pdf = download_pdf_via_browser(agenda_url)
                if not pdf:
                    log.warning("  %s %s: PDF download failed", meeting_id, meeting_date)
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    session.commit()
                    session.close()
                    synced += 1
                    continue

                items = parse_mag_pdf_items(pdf, source_url=agenda_url)

                # Also fetch and download the agenda packet from the event detail page
                if event_url:
                    try:
                        docs = fetch_mag_event_docs(event_url)
                        packet_url = docs.get("packet_url", "")
                        if packet_url:
                            packet_pdf = download_pdf_via_browser(packet_url, referer=event_url)
                            if packet_pdf:
                                supporting_doc_dicts.append({
                                    "document_title": f"{title} — Agenda Packet",
                                    "document_url": urllib.parse.urljoin(
                                        "https://azmag.gov", packet_url
                                    ),
                                    "document_type": "agenda_packet",
                                    "file_name": f"mag-{meeting_id}-packet.pdf",
                                })
                                log.info("  %s %s: packet downloaded (%d bytes)",
                                         meeting_id, meeting_date, len(packet_pdf))
                            else:
                                log.warning("  %s %s: packet PDF download failed",
                                            meeting_id, meeting_date)
                        else:
                            log.debug("  %s %s: no packet URL found", meeting_id, meeting_date)
                    except Exception as e:
                        log.warning("  %s %s: error fetching packet: %s",
                                    meeting_id, meeting_date, e)
            else:
                items = []

            # Normalize items
            for it in items:
                it["meeting_id"] = meeting_id
                it["agenda_item_id"] = f"{meeting_id}-{it.get('agenda_item_number', '0')}"
                it["source_body"] = body_code
                it["meeting_type"] = display_name
                it["meeting_date"] = meeting_date

            replace_meeting_data_safe(
                session, body_code, meeting_id, meeting_dict, items,
                supporting_doc_dicts=supporting_doc_dicts,
            )
            session.commit()
            log.info("  %s %s: %d items", meeting_id, meeting_date, len(items))
            items_found += len(items)
            synced += 1

        except Exception as e:
            log.error("  %s %s: error - %s", meeting_id, meeting_date, e)
            if session:
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
                    session.commit()
                except Exception:
                    session.rollback()
        finally:
            if session:
                session.close()

    return synced, items_found


# ── CLI ──

def list_committees() -> None:
    for cid in sorted(COMMITTEES.keys()):
        code, slug, name, btype = COMMITTEES[cid]
        print(f"  cid={cid:5d} {code:20s}  {name}")


def main() -> int:
    from argparse import ArgumentParser
    p = ArgumentParser(description="MAG committee scraper")
    p.add_argument("--sync", action="store_true")
    p.add_argument("--cid", type=int, default=2)
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--all-years", action="store_true")
    p.add_argument("--start-date", help="YYYY-MM-DD")
    p.add_argument("--end-date", help="YYYY-MM-DD")
    p.add_argument("--force", action="store_true")
    p.add_argument("--skip-downloads", action="store_true")
    p.add_argument("--list-committees", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.list_committees:
        list_committees()
        return 0

    if args.sync:
        if args.cid not in COMMITTEES:
            log.error("Unknown cid=%d. Use --list-committees", args.cid)
            return 1

        # Determine date range
        if args.start_date and args.end_date:
            start_date = args.start_date
            end_date = args.end_date
        elif args.year:
            start_date = f"{args.year}-01-01"
            end_date = f"{args.year}-12-31"
        elif args.all_years:
            start_date = "2022-01-01"
            end_date = datetime.now().strftime("%Y-%m-%d")
        else:
            start_date = datetime.now().strftime("%Y-%m-%d")
            end_date = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")

        # Format for MAG calendar API (MM/DD/YYYY)
        api_start = datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d/%Y")
        api_end = datetime.strptime(end_date, "%Y-%m-%d").strftime("%m/%d/%Y")

        # Register bodies in DB
        from db.core import get_session
        session = get_session()
        db_map = ensure_mag_public_bodies(session)
        session.commit()
        session.close()

        synced, items = sync_mag_committee(
            args.cid, api_start, api_end, db_map,
            force=args.force, skip_downloads=args.skip_downloads,
        )
        log.info("Done: %d meetings synced, %d items", synced, items)
        return 0

    log.warning("No action specified. Use --sync or --list-committees")
    return 0


if __name__ == "__main__":
    sys.exit(main())