#!/usr/bin/env python3
"""Re-parse all Maricopa BOS Formal meeting votes using DOM extractor.

Replaces regex-parsed votes with DOM-extracted votes for all Formal
meetings from 2023-2026.  The DOM extractor finds more items with votes,
correctly detects absent supervisors, fixes false positives on ceremonial
items, and handles sub-items.

Usage:
    DATABASE_URL='sqlite:///.../data/maricopa.sqlite' \
    PYTHONPATH=. python3 backfill_dom_votes.py [--dry-run] [--meeting ID]

Options:
    --dry-run    Extract votes but don't persist to database
    --meeting ID Only process a single meeting
"""

import asyncio
import sys
import os
from pathlib import Path

# Ensure scripts/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright
from sqlalchemy import text
from db import get_session, Meeting, AgendaItem, AgendaItemVote, SupervisorVote
from scraper.summary_dom import extract_votes_from_summary_dom


async def process_meeting(page, meeting_id: str, dry_run: bool = False) -> dict:
    """Extract and optionally persist votes for a single meeting.

    Returns dict with stats for reporting.
    """
    session = get_session()

    # Load meeting
    meeting = session.query(Meeting).filter(Meeting.meeting_id == meeting_id).first()
    if not meeting:
        session.close()
        return {"error": "not_found", "meeting_id": meeting_id}

    summary_url = meeting.source_url.replace("doctype=1", "doctype=3")

    # Load agenda items
    items = (
        session.query(AgendaItem)
        .filter(AgendaItem.meeting_id == meeting_id)
        .order_by(AgendaItem.agenda_item_number)
        .all()
    )
    if not items:
        session.close()
        return {"error": "no_items", "meeting_id": meeting_id}

    vote_items = [
        {"agenda_item_number": it.agenda_item_number, "c_number": it.c_number or ""}
        for it in items
    ]

    # Extract votes via DOM
    try:
        supervisors, votes = await extract_votes_from_summary_dom(
            page, summary_url, vote_items
        )
    except Exception as e:
        session.close()
        return {"error": str(e), "meeting_id": meeting_id}

    # Count stats
    vote_item_nums = set(v["agenda_item_number"] for v in votes)
    total_yes = sum(
        len([sv for sv in v["supervisor_votes"] if sv["vote"] == "yes"]) for v in votes
    )
    total_no = sum(
        len([sv for sv in v["supervisor_votes"] if sv["vote"] == "no"]) for v in votes
    )
    total_abs = sum(
        len([sv for sv in v["supervisor_votes"] if sv["vote"] == "absent"]) for v in votes
    )
    sub_items = sum(
        1 for v in votes
        if sum(1 for v2 in votes if v2["agenda_item_number"] == v["agenda_item_number"]) > 1
    )

    result = {
        "meeting_id": meeting_id,
        "date": str(meeting.meeting_date),
        "items": len(items),
        "supervisors": len(supervisors),
        "votes": len(votes),
        "voted_items": len(vote_item_nums),
        "yes": total_yes,
        "no": total_no,
        "absent": total_abs,
    }

    if dry_run:
        session.close()
        return result

    # Persist: delete old votes, insert new
    from db import persist_votes

    # Delete existing votes for this meeting
    session.execute(
        text(
            "DELETE FROM supervisor_votes WHERE agenda_item_vote_id IN "
            "(SELECT id FROM agenda_item_votes WHERE meeting_id = :mid)"
        ),
        {"mid": meeting_id},
    )
    session.execute(
        text("DELETE FROM agenda_item_votes WHERE meeting_id = :mid"),
        {"mid": meeting_id},
    )
    session.commit()

    # Persist new votes
    count = persist_votes(session, "bos", meeting_id, supervisors, votes)
    session.commit()
    result["persisted"] = count

    session.close()
    return result


async def main():
    dry_run = "--dry-run" in sys.argv
    single_id = None
    for i, arg in enumerate(sys.argv):
        if arg == "--meeting" and i + 1 < len(sys.argv):
            single_id = sys.argv[i + 1]

    session = get_session()

    if single_id:
        meeting_obj = session.query(Meeting).filter(Meeting.meeting_id == single_id).first()
        if not meeting_obj:
            print(f"Meeting {single_id} not found")
            session.close()
            return
        meetings = [meeting_obj]
    else:
        # All Formal BOS meetings with items
        from sqlalchemy import func

        meetings = (
            session.query(Meeting)
            .filter(
                Meeting.meeting_type == "Formal",
                Meeting.source_url.like("https://mccobagenda%"),
                Meeting.meeting_id.in_(
                    session.query(AgendaItem.meeting_id).distinct()
                ),
            )
            .order_by(Meeting.meeting_date)
            .all()
        )

    session.close()

    if not meetings:
        print("No meetings to process")
        return

    mode = "DRY RUN" if dry_run else "PERSIST"
    print(f"Re-parsing {len(meetings)} Formal BOS meetings ({mode})")
    print(f"{'='*70}")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        results = []
        errors = []
        for i, meeting in enumerate(meetings):
            mid = meeting.meeting_id
            date_str = str(meeting.meeting_date)
            print(f"[{i+1}/{len(meetings)}] {mid} ({date_str}) ... ", end="", flush=True)

            r = await process_meeting(page, str(mid), dry_run=dry_run)
            results.append(r)

            if "error" in r:
                errors.append(r)
                print(f"ERROR: {r['error']}")
            else:
                print(
                    f"{r['votes']} votes, {r['voted_items']}/{r['items']} items, "
                    f"{r['yes']}Y/{r['no']}N/{r['absent']}A"
                )

        await browser.close()

    # Summary
    print(f"\n{'='*70}")
    print(f"SUMMARY ({mode})")
    print(f"  Meetings processed: {len(results)}")
    print(f"  Errors: {len(errors)}")
    if errors:
        for e in errors:
            print(f"    {e['meeting_id']}: {e['error']}")

    ok = [r for r in results if "error" not in r]
    if ok:
        total_votes = sum(r["votes"] for r in ok)
        total_yes = sum(r["yes"] for r in ok)
        total_no = sum(r["no"] for r in ok)
        total_abs = sum(r["absent"] for r in ok)
        total_items = sum(r["items"] for r in ok)
        total_voted = sum(r["voted_items"] for r in ok)
        avg_coverage = total_voted / max(total_items, 1) * 100

        print(f"  Total votes: {total_votes}")
        print(f"  Vote breakdown: {total_yes}Y / {total_no}N / {total_abs}A")
        print(f"  Items with votes: {total_voted}/{total_items} ({avg_coverage:.1f}%)")
        print(f"  Absence records: {total_abs} (were 0 with regex parser)")

        # Scope of improvement
        if not dry_run and total_abs > 0:
            print(f"\n  ✓ Absences now detected — {total_abs} records")
        if not dry_run:
            # Count fixable false positives
            session = get_session()
            fp_count = session.execute(
                text(
                    "SELECT COUNT(*) FROM agenda_item_votes aiv "
                    "JOIN agenda_items ai ON ai.agenda_item_number = aiv.agenda_item_number "
                    "AND ai.meeting_id = aiv.meeting_id "
                    "WHERE (ai.agenda_item_title LIKE '%ROLL CALL%' "
                    "OR ai.agenda_item_title LIKE '%PLEDGE OF ALLEGIANCE%' "
                    "OR ai.agenda_item_title LIKE '%INVOCATION%' "
                    "OR ai.agenda_item_title LIKE '%PET SHOWCASE%')"
                )
            ).scalar()
            session.close()
            if fp_count == 0:
                print(f"  ✓ Ceremonial false positives eliminated (were 3)")


if __name__ == "__main__":
    asyncio.run(main())
