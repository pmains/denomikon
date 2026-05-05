#!/usr/bin/env python3
"""
Inspect the Maricopa agenda database.

Usage:
  python scripts/inspect_db.py meetings
  python scripts/inspect_db.py counts
  python scripts/inspect_db.py agenda 4667
  python scripts/inspect_db.py search "CALL TO THE PUBLIC"
  python scripts/inspect_db.py search "SETTLEMENT" --text --limit 20
  python scripts/inspect_db.py item 4667 85
  python scripts/inspect_db.py docs 4449
  python scripts/inspect_db.py docs 4449 5
  python scripts/inspect_db.py status
  python scripts/inspect_db.py failed
  python scripts/inspect_db.py meeting 4449
  python scripts/inspect_db.py meeting 4657
"""

import argparse
import re
import sys
import textwrap
from collections import Counter
from pathlib import Path

# Ensure scripts/ is on the path so we can import db
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import (
    get_session, Meeting, AgendaItem, SupportingDocument,
    Supervisor, MeetingSupervisor, AgendaItemVote, SupervisorVote,
    Case, CaseEvent,
)
from sqlalchemy import or_, select, func, and_


def cmd_meetings(args):
    """List all meetings with item counts, ordered by date."""
    session = get_session()
    rows = session.execute(
        select(
            Meeting.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            Meeting.display_name,
            func.count(AgendaItem.id).label("item_count"),
        )
        .outerjoin(AgendaItem, AgendaItem.meeting_id == Meeting.meeting_id)
        .group_by(Meeting.meeting_id, Meeting.meeting_date, Meeting.meeting_type, Meeting.display_name)
        .order_by(Meeting.meeting_date, Meeting.meeting_id)
    ).all()

    if not rows:
        print("No meetings in database.")
        return

    print(f"{'ID':>6}  {'Date':<12}  {'Type':<14}  {'Items':>5}  {'Display Name'}")
    print(f"{'------':>6}  {'------------':<12}  {'--------------':<14}  {'-----':>5}  {'------------'}")
    for row in rows:
        display = row.display_name or "(not normalized)"
        print(f"{row.meeting_id:>6}  {row.meeting_date:<12}  {row.meeting_type:<14}  {row.item_count:>5}  {display}")
    print(f"\n{len(rows)} meeting(s)")
    session.close()


def cmd_counts(args):
    """Show item counts per meeting, ordered by date."""
    session = get_session()
    rows = session.execute(
        select(
            Meeting.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            func.count(AgendaItem.id).label("item_count"),
        )
        .outerjoin(AgendaItem, AgendaItem.meeting_id == Meeting.meeting_id)
        .group_by(Meeting.meeting_id, Meeting.meeting_date, Meeting.meeting_type)
        .order_by(Meeting.meeting_date, Meeting.meeting_id)
    ).all()

    if not rows:
        print("No meetings in database.")
        return

    total_items = 0
    print(f"{'ID':>6}  {'Date':<12}  {'Type':<14}  {'Items':>5}")
    print(f"{'------':>6}  {'------------':<12}  {'--------------':<14}  {'-----':>5}")
    for row in rows:
        total_items += row.item_count
        print(f"{row.meeting_id:>6}  {row.meeting_date:<12}  {row.meeting_type:<14}  {row.item_count:>5}")
    print(f"\n{len(rows)} meeting(s), {total_items} total items")
    session.close()


def cmd_agenda(args):
    """Show all agenda items for a meeting."""
    session = get_session()
    meeting = session.execute(
        select(Meeting).where(Meeting.meeting_id == args.meeting_id)
    ).scalar_one_or_none()

    if not meeting:
        print(f"Meeting '{args.meeting_id}' not found.")
        session.close()
        return

    items = session.execute(
        select(AgendaItem)
        .where(AgendaItem.meeting_id == args.meeting_id)
        .order_by(AgendaItem.agenda_item_number)
    ).scalars().all()

    if not items:
        print(f"No agenda items for meeting {args.meeting_id}.")
        session.close()
        return

    print(f"{'=' * 70}")
    print(f"{meeting.meeting_id}  {meeting.meeting_date}  {meeting.meeting_type}  {meeting.meeting_title}")
    print(f"{len(items)} items")
    print(f"{'=' * 70}")
    for item in items:
        print(f"  {item.agenda_item_number:>4}.  {item.agenda_item_title}")
    session.close()


def cmd_search(args):
    """Search agenda items by title and optional text."""
    session = get_session()
    query = args.query

    like_pattern = f"%{query}%"

    rows = session.execute(
        select(
            AgendaItem.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            AgendaItem.agenda_item_number,
            AgendaItem.agenda_item_title,
        )
        .join(Meeting, Meeting.meeting_id == AgendaItem.meeting_id)
        .where(
            AgendaItem.agenda_item_title.ilike(like_pattern)
            | AgendaItem.agenda_item_text.ilike(like_pattern)
        )
        .order_by(Meeting.meeting_date, Meeting.meeting_id, AgendaItem.agenda_item_number)
        .limit(args.limit)
    ).all()

    if not rows:
        print(f"No results for '{query}'.")
        session.close()
        return

    # Deduplicate — ILIKE matching both title and text produces duplicate rows
    seen = set()
    deduped = []
    for row in rows:
        key = (row.meeting_id, row.agenda_item_number)
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    rows = deduped

    print(f"Search results for '{query}' ({len(rows)} rows):")
    print()

    for row in rows:
        if args.text:
            text_val = session.execute(
                select(AgendaItem.agenda_item_text).where(
                    AgendaItem.meeting_id == row.meeting_id,
                    AgendaItem.agenda_item_number == row.agenda_item_number,
                )
            ).scalar_one_or_none()
            text_preview = textwrap.shorten(text_val or "", width=80, placeholder="...")
            print(f"  {row.meeting_id} {row.meeting_date} #{row.agenda_item_number}")
            print(f"  Title: {row.agenda_item_title}")
            print(f"  Text:  {text_preview}")
            print()
        else:
            print(f"  {row.meeting_id:>6}  {row.meeting_date:<12}  #{row.agenda_item_number:>4}  {row.agenda_item_title}")

    if len(rows) >= args.limit:
        print(f"[Reached limit of {args.limit}; refine your query for more results]")
    session.close()


def cmd_item(args):
    """Show the full record for one agenda item."""
    session = get_session()
    result = session.execute(
        select(AgendaItem, Meeting.meeting_date, Meeting.meeting_type)
        .join(Meeting, Meeting.meeting_id == AgendaItem.meeting_id)
        .where(
            AgendaItem.meeting_id == args.meeting_id,
            AgendaItem.agenda_item_number == args.agenda_item_number,
        )
    ).one_or_none()

    if not result:
        print(f"Item {args.meeting_id} #{args.agenda_item_number} not found.")
        session.close()
        return

    item, meeting_date, meeting_type = result

    print(f"{'=' * 70}")
    print(f"  Item:     {item.meeting_id} #{item.agenda_item_number}")
    print(f"  Date:     {meeting_date}  {meeting_type}")
    print(f"  ID:       {item.agenda_item_id}")
    print(f"  Title:    {item.agenda_item_title}")
    c_num = item.c_number or ""
    if c_num:
        print(f"  C-number: {c_num}")
    else:
        print(f"  C-number: (none)")
    print(f"  Vote:     {item.vote_or_action or '(none)'}")
    print(f"  URL:      {item.agenda_item_url}")
    print(f"  Source:   {item.source_url}")
    print(f"{'=' * 70}")
    print()
    print(item.agenda_item_text)

    # Show supporting documents
    docs = session.execute(
        select(SupportingDocument).where(
            SupportingDocument.meeting_id == item.meeting_id,
            SupportingDocument.agenda_item_number == item.agenda_item_number,
        )
        .order_by(SupportingDocument.id)
    ).scalars().all()
    if docs:
        print()
        print(f"{'─' * 70}")
        print(f"  Supporting documents:")
        for doc in docs:
            print(f"    {doc.document_title}")
            print(f"    URL: {doc.document_url}")
            if doc.file_extension:
                print(f"    Type: {doc.file_extension.upper()}")
            print()

    session.close()


def cmd_revisions(args):
    """List all base C-numbers with revision counts."""
    session = get_session()
    from sqlalchemy import func as sa_func

    rows = session.execute(
        select(
            AgendaItem.c_number_base,
            sa_func.count(AgendaItem.id).label("total"),
            sa_func.count(sa_func.distinct(AgendaItem.c_number_revision)).label("revisions"),
            sa_func.count(sa_func.distinct(AgendaItem.meeting_id)).label("meetings"),
        )
        .where(AgendaItem.c_number_base != "")
        .group_by(AgendaItem.c_number_base)
        .order_by(sa_func.count(AgendaItem.id).desc())
    ).all()

    if not rows:
        print("No C-numbers in database. Sync some meetings first.")
        session.close()
        return

    print(f"{'Base C-number':<40}  {'Items':>5}  {'Revisions':>5}  {'Meetings':>5}")
    print(f"{'─' * 39}  {'─' * 5}  {'─' * 5}  {'─' * 5}")
    for row in rows:
        print(f"{row.c_number_base:<40}  {row.total:>5}  {row.revisions:>5}  {row.meetings:>5}")
    print(f"\n{len(rows)} unique base C-number(s)")
    session.close()


def cmd_revision(args):
    """Show all agenda items sharing the same c_number_base."""
    session = get_session()
    query = args.c_number_or_base

    # Derive the base: if the input has more than 4 dash-separated segments
    # after C-XX-XX-XXX, strip the last segment to get the base
    c_number_match = re.match(r"(C-\d{2}-\d{2}-\d{3}(?:-[A-Z0-9]{1,3})+)", query, re.I)
    if not c_number_match:
        print(f"'{query}' does not look like a C-number or base.")
        session.close()
        return

    full = c_number_match.group(1)
    # Derive base: everything before the last dash segment
    last_dash = full.rfind("-")
    if last_dash >= 0:
        derived_base = full[:last_dash]
    else:
        derived_base = full

    # Normalize: the input itself might be the base or a specific C-number
    # Try matching on both c_number_base and c_number
    from sqlalchemy import or_

    items = session.execute(
        select(
            AgendaItem.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            AgendaItem.agenda_item_number,
            AgendaItem.agenda_item_title,
            AgendaItem.c_number,
            AgendaItem.c_number_base,
            AgendaItem.c_number_revision,
        )
        .join(Meeting, Meeting.meeting_id == AgendaItem.meeting_id)
        .where(
            or_(
                AgendaItem.c_number_base == full,
                AgendaItem.c_number_base == derived_base,
                AgendaItem.c_number == full,
            )
        )
        .order_by(Meeting.meeting_date, Meeting.meeting_id, AgendaItem.c_number_revision)
    ).all()

    if not items:
        print(f"No items found for C-number base '{derived_base}'.")
        session.close()
        return

    print(f"C-number base: {derived_base} ({len(items)} items)")
    print(f"{'=' * 70}")
    for row in items:
        rev = row.c_number_revision or ""
        print(f"  {row.meeting_id} {row.meeting_date} #{row.agenda_item_number:>4}  rev={rev:>4}  {row.agenda_item_title[:50]}")
    print()

    # Group by revision
    from collections import Counter

    by_revision = Counter(row.c_number_revision or "" for row in items)
    for rev, count in sorted(by_revision.items()):
        label = f"revision {rev}" if rev else "no revision"
        print(f"  {count} item(s) with {label}")
    session.close()


def cmd_docs(args):
    """List supporting documents for a meeting (and optionally an item)."""
    session = get_session()

    stmt = select(SupportingDocument).where(
        SupportingDocument.meeting_id == args.meeting_id
    )
    if args.agenda_item_number is not None:
        stmt = stmt.where(
            SupportingDocument.agenda_item_number == args.agenda_item_number
        )
    stmt = stmt.order_by(
        SupportingDocument.agenda_item_number, SupportingDocument.id
    )

    docs = session.execute(stmt).scalars().all()

    if not docs:
        if args.agenda_item_number is not None:
            print(f"No supporting documents for {args.meeting_id} item #{args.agenda_item_number}.")
        else:
            print(f"No supporting documents for meeting {args.meeting_id}.")
        session.close()
        return

    print(f"Supporting documents for {args.meeting_id}")
    if args.agenda_item_number is not None:
        print(f"Item #{args.agenda_item_number}")
    print(f"{'=' * 70}")
    for doc in docs:
        print(f"  Item #{doc.agenda_item_number}")
        print(f"  Title: {doc.document_title}")
        print(f"  URL:   {doc.document_url}")
        if doc.file_extension:
            print(f"  Type:  {doc.file_extension.upper()}")
        if doc.c_number:
            print(f"  C:     {doc.c_number}")
        print()
    print(f"{len(docs)} document(s)")
    session.close()


def cmd_status(args):
    """Show counts by sync_status and totals."""
    from db import get_session, get_sync_status_summary
    session = get_session()
    summary = get_sync_status_summary(session)
    session.close()
    print(f"{'Status':<14}  {'Count':>6}")
    print(f"{'─' * 14}  {'─' * 6}")
    for status in ["complete", "partial", "manual_review", "failed", "pending"]:
        print(f"{status:<14}  {summary.get(status, 0):>6}")
    print(f"{'─' * 14}  {'─' * 6}")
    print(f"{'Total':<14}  {summary['total']:>6}")
    print(f"\nItems: {summary['total_items']}  Supporting docs: {summary['total_docs']}")


def cmd_insp_failed(args):
    """List meetings with issues (failed/partial, not manual_review)."""
    from db import get_session, get_failed_meetings
    session = get_session()
    meetings = get_failed_meetings(session)
    session.close()
    if not meetings:
        print("No meetings with issues.")
        return
    print(f"{'ID':>6}  {'Date':<12}  {'Status':<12}  {'Retries':>7}  {'Error'}")
    print(f"{'─' * 6}  {'─' * 12}  {'─' * 12}  {'─' * 7}  {'─' * 40}")
    for m in meetings:
        err = (m.last_error or "")[:60]
        print(f"{m.meeting_id:>6}  {m.meeting_date:<12}  {m.sync_status:<12}  {m.retry_count:>7}  {err}")


def cmd_meeting(args):
    """Show sync metadata for one meeting."""
    from db import get_session, Meeting
    from sqlalchemy import select
    session = get_session()
    m = session.execute(
        select(Meeting).where(Meeting.meeting_id == args.meeting_id)
    ).scalar_one_or_none()
    session.close()
    if not m:
        print(f"Meeting '{args.meeting_id}' not found.")
        return
    print(f"Meeting: {m.meeting_id}")
    print(f"  Date:         {m.meeting_date}")
    print(f"  Display Name: {m.display_name or '(not normalized)'}")
    print(f"  Type:         {m.meeting_type}")
    print(f"  Context:      {m.meeting_context or '(none)'}")
    print(f"  Body:         {m.meeting_body or '(none)'}")
    print(f"  Raw Title:    {m.meeting_title_raw or m.meeting_title or '(none)'}")
    print(f"  Status:       {m.sync_status}")
    print(f"  Items:    expected={m.item_count_expected or '?'}  actual={m.item_count_actual or '?'}")
    print(f"  Docs:     {m.supporting_doc_count}")
    print(f"  Extracted:  items={m.items_extracted}  docs={m.supporting_docs_extracted}")
    print(f"  Retries:  {m.retry_count}")
    print(f"  Last synced:   {m.last_synced_at}")
    print(f"  Last attempted: {m.last_attempted_at}")
    error = m.last_error
    if error:
        print(f"  Error:    {error}")


def cmd_supervisors(args):
    """List all known supervisors."""
    session = get_session()
    rows = session.execute(
        select(Supervisor).order_by(Supervisor.name)
    ).scalars().all()
    session.close()

    if not rows:
        print("No supervisors in database.")
        return

    print(f"{'ID':>4}  {'Name':<30}  {'District':<10}  {'Active From':<14}  {'Active To':<14}")
    print(f"{'─' * 4}  {'─' * 30}  {'─' * 10}  {'─' * 14}  {'─' * 14}")
    for s in rows:
        active_from = s.active_from.isoformat() if s.active_from else ""
        active_to = s.active_to.isoformat() if s.active_to else ""
        print(f"{s.id:>4}  {s.name:<30}  {s.district or '':<10}  {active_from:<14}  {active_to:<14}")
    print(f"\n{len(rows)} supervisor(s)")


def cmd_votes_summary(args):
    """Show vote summary for all items in a meeting."""
    session = get_session()
    rows = session.execute(
        select(AgendaItemVote)
        .where(AgendaItemVote.meeting_id == args.meeting_id)
        .order_by(AgendaItemVote.agenda_item_number)
    ).scalars().all()
    session.close()

    if not rows:
        print(f"No vote records for meeting {args.meeting_id}.")
        return

    print(f"Vote summary for meeting {args.meeting_id}")
    print(f"{'─' * 70}")
    print(f"{'#':>4}  {'C-number':<25}  {'Result':<16}")
    print(f"{'─' * 4}  {'─' * 25}  {'─' * 16}")
    for r in rows:
        c = r.c_number or ""
        print(f"{r.agenda_item_number:>4}  {c:<25}  {r.motion_result or '':<16}")
    print(f"\n{len(rows)} item(s) with votes")


def cmd_vote_detail(args):
    """Show detail for one item's vote."""
    session = get_session()
    aiv = session.execute(
        select(AgendaItemVote).where(
            AgendaItemVote.meeting_id == args.meeting_id,
            AgendaItemVote.agenda_item_number == args.agenda_item_number,
        )
    ).scalar_one_or_none()

    if not aiv:
        print(f"No vote record for {args.meeting_id} item #{args.agenda_item_number}.")
        session.close()
        return

    # Get supervisor votes
    sv_rows = session.execute(
        select(SupervisorVote, Supervisor.name)
        .join(Supervisor, Supervisor.id == SupervisorVote.supervisor_id)
        .where(SupervisorVote.agenda_item_vote_id == aiv.id)
        .order_by(SupervisorVote.id)
    ).all()
    session.close()

    print(f"{'=' * 70}")
    print(f"  Meeting:   {aiv.meeting_id}")
    print(f"  Item #:    {aiv.agenda_item_number}")
    print(f"  C-number:  {aiv.c_number or '(none)'}")
    print(f"  Result:    {aiv.motion_result or '(unknown)'}")
    print(f"{'=' * 70}")
    if sv_rows:
        print()
        print(f"  {'Supervisor':<35}  {'Vote':<10}")
        print(f"  {'─' * 35}  {'─' * 10}")
        for sv, name in sv_rows:
            print(f"  {name:<35}  {sv.vote:<10}")
    print()
    if aiv.vote_text:
        print(f"  Vote text (excerpt):")
        print(f"  {aiv.vote_text[:600]}")


def cmd_votes_by_supervisor(args):
    """Show votes cast by a supervisor."""
    session = get_session()
    name_query = args.name

    # Find supervisor by name - prefer shortest name match
    matches = session.execute(
        select(Supervisor).where(
            or_(
                Supervisor.name.ilike(f"%{name_query}%"),
                Supervisor.normalized_name.ilike(f"%{name_query}%"),
            )
        )
        .order_by(func.length(Supervisor.name))
    ).scalars().all()

    # Pick the best match - shortest name is likely the real name (no noise)
    sup = None
    for candidate in matches:
        # Only consider names that aren't obviously noise
        if len(candidate.name) < 40 and not re.search(r"\d", candidate.name):
            sup = candidate
            break

    if not sup:
        print(f"Supervisor '{name_query}' not found.")
        session.close()
        return

    rows = session.execute(
        select(
            SupervisorVote.vote,
            SupervisorVote.raw_vote_text,
            AgendaItemVote.meeting_id,
            AgendaItemVote.agenda_item_number,
            AgendaItemVote.c_number,
            AgendaItemVote.motion_result,
        )
        .join(AgendaItemVote, AgendaItemVote.id == SupervisorVote.agenda_item_vote_id)
        .where(SupervisorVote.supervisor_id == sup.id)
        .order_by(AgendaItemVote.meeting_id, AgendaItemVote.agenda_item_number)
    ).all()
    session.close()

    if not rows:
        print(f"No votes found for {sup.name}.")
        return

    print(f"Votes by {sup.name} (District {sup.district or '?'}): {len(rows)} vote(s)")
    print(f"{'─' * 70}")
    for r in rows:
        c = r.c_number or ""
        print(f"  {r.meeting_id}  #{r.agenda_item_number:>4}  {c:<25}  {r.vote:<8}  ({r.motion_result or '?'})")


def cmd_votes_search(args):
    """Search voted items by query in C-number or title."""
    session = get_session()
    query = args.query
    like = f"%{query}%"

    rows = session.execute(
        select(
            AgendaItemVote.meeting_id,
            AgendaItemVote.agenda_item_number,
            AgendaItemVote.c_number,
            AgendaItemVote.motion_result,
            AgendaItem.agenda_item_title,
        )
        .outerjoin(AgendaItem, and_(
            AgendaItem.meeting_id == AgendaItemVote.meeting_id,
            AgendaItem.agenda_item_number == AgendaItemVote.agenda_item_number,
        ))
        .where(
            or_(
                AgendaItemVote.c_number.ilike(like),
                AgendaItemVote.motion_result.ilike(like),
                AgendaItem.agenda_item_title.ilike(like),
            )
        )
        .order_by(AgendaItemVote.meeting_id, AgendaItemVote.agenda_item_number)
        .limit(args.limit)
    ).all()
    session.close()

    if not rows:
        print(f"No results for '{query}'.")
        return

    print(f"Vote search results for '{query}' ({len(rows)} rows):")
    print()
    for r in rows:
        c = r.c_number or ""
        title_preview = (r.agenda_item_title or "")[:50]
        print(f"  {r.meeting_id:>6}  #{r.agenda_item_number:>4}  {c:<25}  {r.motion_result or '':<12}  {title_preview}")
    if len(rows) >= args.limit:
        print(f"\n[Reached limit of {args.limit}]")


def cmd_cases(args):
    """List all cases with event counts."""
    session = get_session()
    rows = session.execute(
        select(
            Case.case_number,
            Case.case_type,
            Case.description,
            func.count(CaseEvent.id).label("event_count"),
            func.count(func.distinct(CaseEvent.meeting_id)).label("meeting_count"),
        )
        .outerjoin(CaseEvent, CaseEvent.case_id == Case.id)
        .group_by(Case.id, Case.case_number, Case.case_type, Case.description)
        .order_by(Case.case_number)
    ).all()

    if not rows:
        print("No cases in database.")
        session.close()
        return

    print(f"{'Case':<30}  {'Type':<6}  {'Events':>6}  {'Meetings':>6}  {'Description'}")
    print(f"{'─' * 29}  {'─' * 6}  {'─' * 6}  {'─' * 6}  {'─' * 40}")
    for row in rows:
        desc = (row.description or "")[:50]
        print(f"{row.case_number:<30}  {row.case_type:<6}  {row.event_count:>6}  {row.meeting_count:>6}  {desc}")
    print(f"\n{len(rows)} case(s)")
    session.close()


def cmd_case(args):
    """Show full detail for a single case."""
    session = get_session()
    case = session.execute(
        select(Case).where(Case.case_number == args.case_number.upper())
    ).scalar_one_or_none()

    if not case:
        print(f"Case '{args.case_number}' not found.")
        session.close()
        return

    print(f"Case: {case.case_number}")
    print(f"Type: {case.case_type}")
    print(f"Description: {case.description or '(none)'}")
    print(f"Normalized: {case.normalized_case_number}")
    print()

    events = session.execute(
        select(CaseEvent, Meeting.meeting_date, Meeting.meeting_type)
        .outerjoin(Meeting, Meeting.meeting_id == CaseEvent.meeting_id)
        .where(CaseEvent.case_id == case.id)
        .order_by(CaseEvent.event_date, CaseEvent.id)
    ).all()

    if events:
        print(f"Events ({len(events)}):")
        print(f"  {'Date':<14}  {'Source':<6}  {'Type':<12}  {'Meeting Type':<20}  {'Meeting ID'}")
        print(f"  {'─' * 13}  {'─' * 5}  {'─' * 11}  {'─' * 19}  {'─' * 10}")
        for ev, mdate, mtype in events:
            meeting_type = (mtype or ev.source or "")
            print(f"  {ev.event_date:<14}  {ev.source:<6}  {ev.event_type:<12}  {meeting_type:<20}  {ev.meeting_id}")
    session.close()


def cmd_case_history(args):
    """Alias for cmd_case."""
    cmd_case(args)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Inspect Maricopa agenda database")
    sub = p.add_subparsers(dest="command", required=True)

    # meetings
    sub.add_parser("meetings", help="List all meetings with item counts")

    # counts
    sub.add_parser("counts", help="Show item counts per meeting")

    # agenda <meeting_id>
    ap_agenda = sub.add_parser("agenda", help="Show agenda items for a meeting")
    ap_agenda.add_argument("meeting_id", help="Meeting ID (e.g. 4667)")

    # search <query>
    ap_search = sub.add_parser("search", help="Search agenda item titles and text")
    ap_search.add_argument("query", help="Search term")
    ap_search.add_argument("--limit", type=int, default=50, help="Max results (default 50)")
    ap_search.add_argument("--text", action="store_true", help="Show agenda_item_text snippets")

    # item <meeting_id> <agenda_item_number>
    ap_item = sub.add_parser("item", help="Show full record for one agenda item")
    ap_item.add_argument("meeting_id", help="Meeting ID")
    ap_item.add_argument("agenda_item_number", type=int, help="Item number")

    # revisions
    sub.add_parser("revisions", help="List all base C-numbers with revision counts")

    # revision <c_number_or_base>
    ap_revision = sub.add_parser("revision", help="Show all agenda items sharing the same c_number_base")
    ap_revision.add_argument("c_number_or_base", help="C-number or base (e.g. C-86-25-040-X-00 or C-86-25-040-X)")

    # docs [meeting_id] [agenda_item_number]
    ap_docs = sub.add_parser("docs", help="List supporting documents for a meeting or item")
    ap_docs.add_argument("meeting_id", help="Meeting ID")
    ap_docs.add_argument("agenda_item_number", nargs="?", type=int, default=None, help="Optional item number")

    # meeting <meeting_id>
    ap_meeting = sub.add_parser("meeting", help="Show sync metadata for a meeting")
    ap_meeting.add_argument("meeting_id", help="Meeting ID")

    # status
    sub.add_parser("status", help="Show counts by sync_status and totals")

    # failed
    sub.add_parser("failed", help="List failed/partial meetings with errors")

    # supervisors
    sub.add_parser("supervisors", help="List all known supervisors")

    # votes <meeting_id>
    ap_votes = sub.add_parser("votes", help="Show vote summary for all items in a meeting")
    ap_votes.add_argument("meeting_id", help="Meeting ID")

    # vote <meeting_id> <item_number>
    ap_vote = sub.add_parser("vote", help="Show vote detail for one item")
    ap_vote.add_argument("meeting_id", help="Meeting ID")
    ap_vote.add_argument("agenda_item_number", type=int, help="Item number")

    # votes-by-supervisor <name>
    ap_vbs = sub.add_parser("votes-by-supervisor", help="Show votes cast by a supervisor")
    ap_vbs.add_argument("name", help="Supervisor name (partial match)")

    # votes-search <query>
    ap_vs = sub.add_parser("votes-search", help="Search voted items")
    ap_vs.add_argument("query", help="Search term (C-number, result, or title)")
    ap_vs.add_argument("--limit", type=int, default=50, help="Max results (default 50)")

    # cases
    sub.add_parser("cases", help="List all cases with event counts")

    # case <case_number>
    ap_case = sub.add_parser("case", help="Show full detail for a single case")
    ap_case.add_argument("case_number", help="Case number (e.g. CPA2024001)")

    # case-history <case_number>
    ap_case_hist = sub.add_parser("case-history", help="Show event history for a case (alias for case)")
    ap_case_hist.add_argument("case_number", help="Case number (e.g. CPA2024001)")

    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    dispatch = {
        "meetings": cmd_meetings,
        "counts": cmd_counts,
        "agenda": cmd_agenda,
        "search": cmd_search,
        "item": cmd_item,
        "revisions": cmd_revisions,
        "revision": cmd_revision,
        "docs": cmd_docs,
        "status": cmd_status,
        "failed": cmd_insp_failed,
        "meeting": cmd_meeting,
        "supervisors": cmd_supervisors,
        "votes": cmd_votes_summary,
        "vote": cmd_vote_detail,
        "votes-by-supervisor": cmd_votes_by_supervisor,
        "votes-search": cmd_votes_search,
        "cases": cmd_cases,
        "case": cmd_case,
        "case-history": cmd_case_history,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
