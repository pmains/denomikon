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

from sqlalchemy import text

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


def _connect(engine):
    """Return a SQLAlchemy connection (works on SQLite and PostgreSQL)."""
    return engine.connect()


def check_granicus(engine) -> int:
    """Check Granicus RSS minutes feeds for newly-published minutes.

    Returns the number of meetings updated with a minutes_url.
    """
    found = 0
    conn = _connect(engine)

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

            row = conn.execute(
                text("SELECT body, meeting_id FROM meetings "
                     "WHERE meeting_id = :mid AND minutes_url IS NULL"),
                {"mid": event_id}
            ).fetchone()
            if row:
                body_code, mid = row
                conn.execute(
                    text("UPDATE meetings SET minutes_url = :url "
                         "WHERE body = :body AND meeting_id = :mid"),
                    {"url": link, "body": body_code, "mid": mid}
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

    conn = _connect(engine)
    rows = conn.execute(
        text("SELECT body, meeting_id FROM meetings "
             "WHERE body LIKE 'chandler-%' AND minutes_url IS NULL "
             "AND meeting_date >= '2024-01-01' "
             "ORDER BY meeting_date DESC LIMIT 500")
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    log.info("  Checking %d Chandler meetings for minutes...", len(rows))
    found = 0
    for body, meeting_id in rows:
        # Parse date from a meeting row lookup
        conn2 = _connect(engine)
        date_row = conn2.execute(
            text("SELECT meeting_date FROM meetings WHERE body = :body AND meeting_id = :mid"),
            {"body": body, "mid": meeting_id}
        ).fetchone()
        conn2.close()
        if not date_row:
            continue
        meeting_date = date_row[0]

        try:
            att_url = build_attachments_url(meeting_id, meeting_date)
            att_html = fetch_attachments_page(att_url)
            if att_html:
                pdfs = parse_attachments_for_minutes(att_html, meeting_id)
                if pdfs:
                    conn3 = _connect(engine)
                    conn3.execute(
                        text("UPDATE meetings SET minutes_url = :url WHERE body = :body AND meeting_id = :mid"),
                        {"url": pdfs[0], "body": body, "mid": meeting_id}
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

    # Only check integer meeting_ids; use SQLAlchemy's CAST syntax
    dialect = engine.dialect.name
    if dialect == "postgresql":
        cast_expr = "CAST(meeting_id AS INTEGER) > 0"
    else:
        cast_expr = "CAST(meeting_id AS INTEGER) > 0"

    conn = _connect(engine)
    rows = conn.execute(
        text(f"SELECT body, meeting_id, meeting_date, meeting_type FROM meetings "
             f"WHERE body = 'tempe-cc' AND minutes_url IS NULL "
             f"AND {cast_expr} "
             f"AND meeting_date >= '2025-01-01' "
             f"ORDER BY meeting_date DESC LIMIT 200")
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
                    minutes_url = (
                        f"https://tempe.hylandcloud.com/"
                        f"{TEMPE_CONFIG.base_path}/MeetingAccess.aspx?"
                        f"MeetingId={mid}&DocType=3&Filename={name}"
                    )
                    conn2 = _connect(engine)
                    conn2.execute(
                        text("UPDATE meetings SET minutes_url = :url WHERE body = :body AND meeting_id = :mid"),
                        {"url": minutes_url, "body": body, "mid": meeting_id}
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
