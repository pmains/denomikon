"""
Minutes check pass — re-visits completed meetings to discover
minutes/votes that were published after the initial sync.

Each platform has its own way of exposing minutes URLs:

  Granicus   → ViewPublisherRSS.php?mode=minutes  (re-check RSS by clip_id)
  Legistar   → MeetingDetail.aspx                  (check for minutes attachments)
  OnBase     → MeetingView.aspx                    (check for summary/supplemental docs)
  AgendaQuick → DailyCalendarView.aspx             (check for minutes report links)
"""

from __future__ import annotations

import logging
import re
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ── Granicus ──
# Granicus posts minutes links to the RSS feed after they're published.
# We re-fetch the feed and match by event_id / clip_id.

GRANICUS_INSTANCES: list[tuple[str, int, list[str]]] = [
    ("buckeyeaz.granicus.com", 1, [
        "buckeye-cc", "buckeye-pz", "buckeye-cfd", "buckeye-youth",
        "buckeye-community-services", "buckeye-arts-culture",
        "buckeye-psprs-police", "buckeye-psprs-fire", "buckeye-pollution-control",
        "buckeye-airport", "buckeye-library", "buckeye-water-rate",
    ]),
    ("surpriseaz.granicus.com", 1, ["surprise-cc", "surprise-pz"]),
    ("goodyearaz.granicus.com", 1, ["goodyear-cc"]),
    ("avondaleaz.granicus.com", 1, ["avondale-cc", "avondale-pz", "avondale-boa"]),
]

# ── Legistar ──
# Legistar meeting detail pages have a "minutes" attachment link.
# We can re-check by body (department_id) + meeting_id.

LEGISTAR_INSTANCES: list[tuple[str, str, str]] = [
    # (base_url, calendar_key, meeting_url_pattern)
    ("phoenix.legistar.com", "phoenix", "https://phoenix.legistar.com/MeetingDetail.aspx"),
    ("mesa.legistar.com", "mesa", "https://mesa.legistar.com/MeetingDetail.aspx"),
    ("glendaleaz.legistar.com", "glendale", "https://glendaleaz.legistar.com/MeetingDetail.aspx"),
]


def check_granicus(engine) -> int:
    """Check Granicus RSS minutes feeds for newly-published minutes.

    Returns the number of meetings updated with a minutes_url.
    """
    found = 0
    conn = engine.raw_connection()

    for host, view_id, body_codes in GRANICUS_INSTANCES:
        url = f"https://{host}/ViewPublisherRSS.php?view_id={view_id}&mode=minutes"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                rss = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.debug("  RSS fetch failed for %s: %s", host, e)
            continue

        for item in re.findall(r"<item>(.*?)</item>", rss, re.DOTALL):
            link_m = re.search(r"<link>(.*?)</link>", item)
            if not link_m or "MinutesViewer" not in link_m.group(1):
                continue
            link = link_m.group(1).strip()
            m = re.search(r"clip_id=(\d+)", link)
            event_id = m.group(1) if m else ""
            if not event_id:
                continue

            cursor = conn.execute(
                "SELECT body, meeting_id FROM meetings "
                "WHERE meeting_id = ? AND minutes_url IS NULL",
                (event_id,)
            )
            row = cursor.fetchone()
            if row:
                body_code, mid = row
                conn.execute(
                    "UPDATE meetings SET minutes_url = ? "
                    "WHERE body = ? AND meeting_id = ?",
                    (link, body_code, mid)
                )
                found += 1
                log.info("   minutes: %s %s → %s", body_code, event_id, link[:60])

    conn.commit()
    conn.close()
    return found


def check_chandler_agendaquick(engine) -> int:
    """Check Chandler AgendaQuick attachments pages for minutes PDFs.

    Chandler's AgendaQuick platform posts minutes PDFs separately
    on the attachments (dsp=atf) page. We re-check meetings that
    have a matching meeting_id (= meeting_seq).
    """
    try:
        from scraper.chandler import (
            build_attachments_url, fetch_attachments_page,
            parse_attachments_for_minutes,
        )
    except ImportError:
        log.warning("  Chandler scraper not available — skipping")
        return 0

    conn = engine.raw_connection()
    rows = conn.execute(
        "SELECT body, meeting_id FROM meetings "
        "WHERE body LIKE 'chandler-%' AND minutes_url IS NULL "
        "AND meeting_date >= '2024-01-01' "
        "ORDER BY meeting_date DESC LIMIT 500"
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    log.info("  Checking %d Chandler meetings for minutes...", len(rows))
    found = 0
    for body, meeting_id in rows:
        meeting_seq = meeting_id
        # Parse date from a meeting row lookup
        conn2 = engine.raw_connection()
        date_row = conn2.execute(
            "SELECT meeting_date FROM meetings WHERE body = ? AND meeting_id = ?",
            (body, meeting_id)
        ).fetchone()
        conn2.close()
        if not date_row:
            continue
        meeting_date = date_row[0]

        try:
            att_url = build_attachments_url(meeting_seq, meeting_date)
            att_html = fetch_attachments_page(att_url)
            if att_html:
                pdfs = parse_attachments_for_minutes(att_html, meeting_seq)
                if pdfs:
                    conn3 = engine.raw_connection()
                    conn3.execute(
                        "UPDATE meetings SET minutes_url = ? WHERE body = ? AND meeting_id = ?",
                        (pdfs[0], body, meeting_id)
                    )
                    conn3.commit()
                    conn3.close()
                    found += 1
                    log.debug("   minutes: %s %s → %s", body, meeting_id, pdfs[0][:60])
        except Exception as e:
            log.debug("  check failed for %s %s: %s", body, meeting_id, e)
            continue

    log.info("  ✅ %d Chandler meetings now have minutes_url", found)
    return found


def check_tempe_onbase(engine) -> int:
    """Check Tempe OnBase meetings for Legal Action Summary (minutes) PDFs.

    Tempe's Legal Action Summary is essentially the approved minutes.
    We re-check meetings that have a numeric meeting_id (OnBase ID).
    """
    try:
        from scraper.tempe_summary import _summary_document_names
        from scraper.onbase import TEMPE_CONFIG, download_document
    except ImportError:
        log.warning("  Tempe scraper not available — skipping")
        return 0

    conn = engine.raw_connection()
    rows = conn.execute(
        "SELECT body, meeting_id, meeting_date, meeting_type FROM meetings "
        "WHERE body = 'tempe-cc' AND minutes_url IS NULL "
        "AND CAST(meeting_id AS INTEGER) > 0 "
        "AND meeting_date >= '2025-01-01' "
        "ORDER BY meeting_date DESC LIMIT 200"
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    log.info("  Checking %d Tempe Council meetings for minutes...", len(rows))
    found = 0
    for body, meeting_id, meeting_date, meeting_type in rows:
        try:
            mid = int(meeting_id)
            candidates = _summary_document_names(mid, meeting_date, meeting_type or "")
            for name in candidates:
                pdf_bytes = download_document(TEMPE_CONFIG, mid, name, doc_type=3)
                if pdf_bytes and len(pdf_bytes) >= 1000:
                    # Construct a viewable URL
                    minutes_url = (
                        f"https://tempe.hylandcloud.com/"
                        f"{TEMPE_CONFIG.base_path}/MeetingAccess.aspx?"
                        f"MeetingId={mid}&DocType=3&Filename={name}"
                    )
                    conn2 = engine.raw_connection()
                    conn2.execute(
                        "UPDATE meetings SET minutes_url = ? WHERE body = ? AND meeting_id = ?",
                        (minutes_url, body, meeting_id)
                    )
                    conn2.commit()
                    conn2.close()
                    found += 1
                    log.debug("   minutes: tempe-cc %s → %s", meeting_id, minutes_url[:60])
                    break
        except (ValueError, Exception):
            continue

    log.info("  ✅ %d Tempe meetings now have minutes_url", found)
    return found


def check_legistar(engine) -> int:
    """Check Legistar meeting detail pages for minutes links.

    Not yet implemented — Legistar attachment discovery requires
    Playwright or parsing the MeetingDetail.aspx page.
    """
    return 0


def check_all(engine) -> int:
    """Run all minutes checks and return total updated."""
    total = 0
    total += check_granicus(engine)
    total += check_chandler_agendaquick(engine)
    total += check_tempe_onbase(engine)
    total += check_legistar(engine)
    return total
