#!/usr/bin/env python3
"""
Chandler Destiny Doc Checker — re-sync meetings that now have supporting docs.

Chandler uses the Destiny/AgendaQuick platform. When meetings are first synced,
their supporting documents may not yet be published. This checker finds recent
Chandler meetings that have agenda items but no downloaded documents, fetches
each item's detail page looking for popupAttachments, and triggers a re-sync
when new documents are available.

Design mirrors doc_check.py (the Tempe OnBase equivalent).
"""

import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("destiny_doc_check")

BASE_URL = "https://public.destinyhosted.com"
CHANDLER_ID = "24263"

# Bodies on the Chandler Destiny platform that CAN have supporting documents
# via popupAttachments on item detail (dsp=agm) pages.
CHECKABLE_BODIES = frozenset({
    "chandler-cc",
    "chandler-pz",
    "chandler-drc",
    "chandler-boa",
    "chandler-hpc",
    "chandler-hhsc",
    "chandler-ida",
    "chandler-nac",
    "chandler-mvc",
    "chandler-arts",
    "chandler-tc",
    "chandler-eda",
    "chandler-cf",
    "chandler-mf",
    "chandler-hcc",
    "chandler-pha",
    "chandler-cpr",
    "chandler-dvc",
    "chandler-hrc",
    "chandler-prb",
    "chandler-lb",
    "chandler-yc",
    "chandler-pdc",
    "chandler-air",
    "chandler-psprs-f",
    "chandler-psprs-p",
    "chandler-hct",
    "chandler-wct",
})

# Skip meeting types that never have documents
SKIP_TYPES = frozenset({
    "executive session",
    "executive",
    "cancelled",
    "canceled",
    "quorum notice",
})

# Max days past meeting date before we stop checking
MAX_DAYS_BACK = 90


def meeting_should_never_have_docs(meeting_type: str) -> bool:
    mt = (meeting_type or "").lower().strip()
    for skip in SKIP_TYPES:
        if skip in mt:
            return True
    return False


def fetch_item_detail_page(item_url: str) -> str:
    """Fetch a Destiny item detail (dsp=agm) page.

    Returns the HTML text, or empty string on failure.
    """
    req = urllib.request.Request(item_url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("Failed to fetch item detail: %s", e)
        return ""


def has_documents(html: str) -> int:
    """Check if the item detail page has popupAttachments references.

    Returns the count of attachments found.
    """
    # Match: popupAttachments('/path/to/doc.pdf','ATTACHMENTS')
    count = len(re.findall(
        r"popupAttachments\([\"']([^\"']+)[\"']\s*,\s*[\"']ATTACHMENTS[\"']\s*\)",
        html,
    ))
    return count


def check_meetings(dry_run: bool = True, limit: int = 0) -> dict:
    """Main check loop for Chandler Destiny meetings.

    Parameters
    ----------
    dry_run : bool
        If True, report findings but do NOT re-sync.
    limit : int
        Max meetings to check. 0 = unlimited.

    Returns a dict with summary stats.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    from db import get_session
    from db.models import Meeting as MeetingModel
    from sqlalchemy import select, func
    from scraper.destiny_common import fetch_agenda_memo_docs, BASE_URL as DESTINY_BASE

    now = datetime.now(timezone.utc)
    today = date.today()
    cutoff = today - timedelta(days=MAX_DAYS_BACK)
    session = get_session()

    # Find Chandler meetings that have items but no supporting docs
    rows = session.execute(
        select(MeetingModel)
        .where(
            MeetingModel.body.in_(CHECKABLE_BODIES),
            MeetingModel.items_extracted == True,  # noqa: E712
            MeetingModel.supporting_docs_extracted == False,  # noqa: E712
            MeetingModel.sync_status.in_(["complete", "pending"]),
            MeetingModel.meeting_date >= cutoff.isoformat(),
        )
        .order_by(MeetingModel.meeting_date.desc())
        .limit(limit or None)
    ).scalars().all()

    if not rows:
        log.info("No Chandler meetings need doc checking.")
        return {"checked": 0, "docs_found": 0, "resynced": 0, "errors": 0}

    log.info("Checking %d Chandler meeting(s) for document availability...", len(rows))

    stats = {"checked": 0, "docs_found": 0, "resynced": 0, "errors": 0}

    for meeting in rows:
        meeting_id = meeting.meeting_id
        body_code = meeting.body
        meeting_type = meeting.meeting_type or ""

        # Skip types that never have documents
        if meeting_should_never_have_docs(meeting_type):
            log.info("  %s/%s: skipping (type=%s)", body_code, meeting_id, meeting_type)
            continue

        stats["checked"] += 1

        # Fetch the agenda page to find item detail (dsp=agm) URLs
        try:
            source_url = meeting.source_url or ""
            if not source_url:
                log.debug("  %s/%s: no source URL", body_code, meeting_id)
                continue

            req = urllib.request.Request(source_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                agenda_html = resp.read().decode("utf-8", errors="replace")

            # Find all item detail page links
            item_links = re.findall(r'href="([^"]*dsp=agm[^"]*)"', agenda_html)
            item_links = [urllib.parse.urljoin(BASE_URL, l.replace("&amp;", "&"))
                         for l in item_links]

            if not item_links:
                log.debug("  %s/%s: no item detail links on agenda page", body_code, meeting_id)
                continue

            # Check each item detail page for popupAttachments
            total_docs = 0
            for item_url in item_links[:20]:  # Safety cap
                try:
                    detail_html = fetch_item_detail_page(item_url)
                    if not detail_html:
                        continue
                    count = has_documents(detail_html)
                    total_docs += count
                except Exception:
                    continue

            if total_docs > 0 and not dry_run:
                # Documents found! Trigger a re-sync
                # Re-fetch the agenda and re-extract everything
                from scraper.chandler import parse_agenda_items, fetch_page
                from db.persist import replace_meeting_data_safe

                stats["docs_found"] += total_docs
                log.info("  ✅ %s/%s: %d doc(s) found — re-syncing...",
                         body_code, meeting_id, total_docs)

                try:
                    meeting_dict = {
                        "meeting_id": meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                        "meeting_title": meeting.meeting_title or meeting_type,
                        "source_url": source_url,
                    }
                    items_html = fetch_page(source_url, timeout=20)
                    items = parse_agenda_items(items_html, meeting_id)

                    supp_docs = []
                    seen_urls = set()
                    for it in items:
                        memo_url = it.get("agenda_item_url", "") or it.get("source_url", "")
                        if memo_url and memo_url not in seen_urls:
                            seen_urls.add(memo_url)
                            docs = fetch_agenda_memo_docs(memo_url, timeout=15)
                            for doc in docs:
                                an = it.get("agenda_item_number", "")
                                doc["agenda_item_id"] = 0
                                doc["agenda_item_number"] = an
                                supp_docs.append(doc)

                    agenda_item_dicts = []
                    for it in items:
                        an = it.get("agenda_item_number", "")
                        agenda_item_dicts.append({
                            "agenda_item_id": body_code + "-" + meeting_id + "_" + an,
                            "meeting_id": meeting_id,
                            "agenda_item_number": an,
                            "agenda_item_title": it.get("agenda_item_title", ""),
                            "agenda_item_text": it.get("agenda_item_text", ""),
                            "agenda_item_url": it.get("agenda_item_url", "") or it.get("source_url", ""),
                            "vote_or_action": "",
                            "sort_order": it.get("sort_order", 0),
                        })

                    replace_meeting_data_safe(
                        session, body_code, meeting_id, meeting_dict,
                        agenda_item_dicts, supporting_doc_dicts=supp_docs,
                    )
                    session.commit()
                    stats["resynced"] += 1
                    log.info("     Re-sync done: %d items, %d docs",
                             len(items), len(supp_docs))
                except Exception as e:
                    session.rollback()
                    log.warning("     Re-sync failed for %s/%s: %s", body_code, meeting_id, e)
                    stats["errors"] += 1

            elif total_docs > 0:
                stats["docs_found"] += total_docs
                log.info("  🟡 %s/%s: %d doc(s) available (dry run)",
                         body_code, meeting_id, total_docs)
            else:
                log.info("  ❌ %s/%s: no documents yet", body_code, meeting_id)

        except Exception as e:
            log.warning("  Error checking %s/%s: %s", body_code, meeting_id, e)
            stats["errors"] += 1

    if not dry_run:
        session.commit()
    session.close()

    log.info("Check complete: %d checked, %d docs found, %d re-synced, %d errors",
             stats["checked"], stats["docs_found"], stats["resynced"], stats["errors"])
    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Check Chandler Destiny meetings for newly-published supporting documents"
    )
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only, don't re-sync (default)")
    parser.add_argument("--apply", action="store_false", dest="dry_run",
                        help="Actually re-sync meetings with new docs")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max meetings to check (0 = unlimited)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stats = check_meetings(dry_run=args.dry_run, limit=args.limit)

    print(f"\n=== Chandler Doc Check Summary ===")
    print(f"  Checked:       {stats['checked']}")
    print(f"  Docs found:    {stats['docs_found']}")
    print(f"  Re-synced:     {stats['resynced']}")
    print(f"  Errors:        {stats['errors']}")
    if args.dry_run:
        print(f"\n  (dry run — no changes applied)")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
