from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
import time
from pathlib import Path

from scraper.utils import (
    log, setup_logger, SOURCE_PAGE, SEARCH_BASE, REQUIRED_BODY, REQUIRED_TYPES,
    ROOT, AGENDAS_ROOT, SUPPORT_ROOT, AGENDA_ITEMS_ROOT, AGENDA_ITEMS_CSV,
    RAW_AGENDA_ITEMS_CSV, REJECTED_RAW_BLOCKS_CSV, DISCOVERY_CSV, LOGS_ROOT,
    get_async_playwright, retry_with_backoff, CASE_PATTERN, C_NUMBER_PATTERN,
    _extract_c_number, parse_c_number_parts, parse_metadata_from_page_data,
    extract_meeting_metadata_from_page, is_image_based_agenda,
    get_page_state_summary,
)
from scraper.models import Meeting
from scraper.cli import parse_args, parse_date
from scraper.search import parse_search_results_html, build_search_url, extract_meetings
from scraper.io_utils import (
    slugify, normalize_meeting_date, _normalize_text_date,
    month_dir_for_date, month_metadata_path, ensure_dir, csv_row,
    read_existing_rows, write_download_row, write_discovery_row,
    write_agenda_item_row, write_structured_agenda_item_row,
    write_raw_agenda_item_row, write_rejected_raw_block_row,
    debug_agenda_html_path,
    url_ext, infer_extension, download_url,
    existing_paths_present, row_paths_present, read_existing_agenda_urls,
    read_existing_discovery_keys, read_agenda_metadata_rows,
    filter_agenda_metadata_rows, read_existing_agenda_item_keys,
    read_existing_raw_block_keys, read_existing_rejected_block_keys,
    read_existing_structured_item_keys, write_discovery_rows,
    iter_discovery_documents,
)
from scraper.agenda_items import (
    parse_agenda_items_from_html, parse_raw_agenda_blocks_html,
    split_bilingual_title,
    _raw_block_boilerplate_reason, validate_raw_block, split_raw_block_into_items,
    splitter_self_test, split_raw_agenda_blocks_to_structured,
    _clean_line, _looks_like_boilerplate, _looks_like_item_heading,
    _looks_like_section_heading, _detect_vote_or_action, _build_item_url,
    _clean_lnk_title, _find_item_tables, _extract_lnk_from_table,
    extract_agenda_item_titles,
)
from scraper.supporting_docs import (
    _extract_supporting_docs_from_table, extract_supporting_documents_from_items,
    extract_supporting_documents_dynamic, extract_supporting_documents_dynamic_concurrent,
    _click_and_extract_item,
)
from scraper.pz import (
    _format_mm_dd_yyyy,
    _normalize_pz_meeting_title,
    build_pz_search_url,
    extract_pz_meetings,
    extract_pz_agenda_items,
)
from scraper.votes import extract_votes_from_summary

async def write_agenda_debug_files(page, meeting: dict[str, str]) -> None:
    ensure_dir(LOGS_ROOT)
    meeting_id = (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting"
    html_path = debug_agenda_html_path(meeting_id, ".html")
    txt_path = debug_agenda_html_path(meeting_id, ".txt")
    selectors_path = debug_agenda_html_path(meeting_id, "_selectors.txt")

    html_path.write_text(await page.content(), encoding="utf-8")
    body_text = await page.locator("body").inner_text(timeout=60000)
    txt_path.write_text(body_text, encoding="utf-8")

    selector_report = await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\s+/g, ' ').trim();
          const describe = el => {
            if (!el) return 'unknown';
            const tag = el.tagName ? el.tagName.toLowerCase() : 'element';
            const id = el.id ? `#${el.id}` : '';
            const cls = el.className && typeof el.className === 'string'
              ? '.' + el.className.trim().split(/\s+/).filter(Boolean).slice(0, 4).join('.')
              : '';
            return `${tag}${id}${cls}`;
          };
          const text = el => clean(el?.innerText || el?.textContent || '');
          const candidates = Array.from(document.querySelectorAll('main, section, article, table, tbody, thead, tr, div, ul, ol, body'))
            .filter(el => text(el).length > 0)
            .slice(0, 50);
          return candidates.map((el, idx) => {
            const t = text(el);
            const rows = el.querySelectorAll('tr, li, p').length;
            const links = el.querySelectorAll('a[href]').length;
            const numbered = t.includes('1.') || t.includes('2.') || t.includes('3.');
            return {
              index: idx + 1,
              selector: describe(el),
              rows,
              links,
              numbered,
              text: t.slice(0, 500),
            };
          });
        }
        """
    )

    with selectors_path.open("w", encoding="utf-8") as f:
        for row in selector_report:
            f.write(
                f"Selector: {row['selector']}\n"
                f"Child rows/items: {row['rows']}\n"
                f"Link count: {row['links']}\n"
                f"Contains numbered items: {row['numbered']}\n"
                f"Text (first 500 chars): {row['text']}\n"
                f"---\n"
            )



async def extract_agenda_items_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    # Wait for all network activity to settle (including the AJAX call that
    # populates the agenda).  Using networkidle ensures the page is fully
    # rendered, not a stale JS scaffold with unloaded data.
    await page.goto(source_url, wait_until="networkidle", timeout=60000)
    html = await page.content()
    normalized_meeting = {
        "meeting_id": (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting",
        "meeting_date": (meeting.get("record_date") or meeting.get("meeting_date") or "").strip(),
        "meeting_type": (meeting.get("meeting_type") or "").strip(),
    }
    return parse_agenda_items_from_html(html, source_url, normalized_meeting)



async def extract_raw_agenda_blocks_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    await page.goto(source_url, wait_until="load")
    return parse_raw_agenda_blocks_html(await page.content(), meeting)



async def extract_raw_agenda_blocks_from_metadata(page, meeting_rows: Optional[list[dict[str, str]]] = None) -> int:
    if meeting_rows is None:
        meeting_rows = read_agenda_metadata_rows()
    if not meeting_rows:
        print("No agenda metadata rows found for raw block extraction.")
        return 0

    existing_keys = read_existing_raw_block_keys(RAW_AGENDA_ITEMS_CSV)
    ensure_dir(RAW_AGENDA_ITEMS_CSV.parent)
    wrote = 0

    for meeting in meeting_rows:
        blocks = await extract_raw_agenda_blocks_for_meeting(page, meeting)
        for block in blocks:
            key = (block["meeting_id"], block["raw_block_index"])
            if key in existing_keys:
                continue
            write_raw_agenda_item_row(block)
            existing_keys.add(key)
            wrote += 1
    return wrote



async def extract_agenda_items_from_metadata(
    page,
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
    limit: Optional[int] = None,
) -> int:
    meeting_rows = filter_agenda_metadata_rows(
        read_agenda_metadata_rows(), start_date, end_date, limit
    )
    if not meeting_rows:
        print("No agenda metadata rows matched the selected date range/limit.")
        return 0

    existing_keys = read_existing_agenda_item_keys(AGENDA_ITEMS_CSV)
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    wrote = 0

    for meeting in meeting_rows:
        meeting_id = (meeting.get("record_id") or meeting.get("meeting_id") or "").strip() or "meeting"
        try:
            items = await extract_agenda_items_for_meeting(page, meeting)
        except Exception as e:
            log.error("extract_agenda_items_for_meeting failed meeting_id=%s error=%s", meeting_id, str(e)[:300])
            continue
        for item in items:
            key = (item["meeting_id"], item["agenda_item_id"])
            if key in existing_keys:
                continue
            write_agenda_item_row(item)
            existing_keys.add(key)
            wrote += 1
    return wrote



async def count_agenda_items_for_meeting(page, meeting_url: str) -> int:
    """Visit an agenda HTML page and count the number of numbered agenda items."""
    items = await extract_agenda_item_titles(page, meeting_url)
    return len(items)



async def main() -> int:
    setup_logger()
    args = parse_args()

    # Backward compatibility: --sync-pz with legacy --pz-* flags
    if getattr(args, 'sync_pz', False):
        print("WARNING: --sync-pz is deprecated. Use: pz --sync", file=sys.stderr)
        args.source = "pz"
        # Map legacy PZ flags to the new unified args
        if getattr(args, 'pz_start_date', None):
            args.start_date = args.pz_start_date
        if getattr(args, 'pz_end_date', None):
            args.end_date = args.pz_end_date
        if getattr(args, 'pz_limit', None) is not None:
            args.limit = args.pz_limit
        args.sync = True

    if getattr(args, 'self_test_splitter', False):
        return 0 if splitter_self_test(verbose=True) else 1

    if args.init_db:
        from db import init_db

        init_db()
        print("Database tables created.")
        return 0


    if args.source == "tempe" and args.sync:
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe, persist_votes
        from db import Supervisor, PublicBody
        from scraper.tempe_summary import fetch_and_parse_summary
        from scraper.tempe import (
            search_tempe_meetings,
            JURISDICTION_ID,
            PUBLIC_BODY_CODE,
        )
        from scraper.onbase import TEMPE_CONFIG
        import datetime as _dt

        from db import Meeting as MeetingModel
        from db import PublicBody as PublicBodyModel
        from sqlalchemy import select

        init_db()

        # Format dates for OnBase (MM/DD/YYYY)
        now = _dt.date.today()
        if args.start_date:
            d = _dt.date.fromisoformat(args.start_date)
            pz_start = f"{d.month:02d}/{d.day:02d}/{d.year}"
        else:
            three_months_ago = now - _dt.timedelta(days=90)
            pz_start = f"{three_months_ago.month:02d}/01/{three_months_ago.year}"

        if args.end_date:
            d = _dt.date.fromisoformat(args.end_date)
            pz_end = f"{d.month:02d}/{d.day:02d}/{d.year}"
        else:
            pz_end = f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"

        print(f"Tempe search: {pz_start} to {pz_end}")
        body_group = getattr(args, "bodies", None) or "all"
        meetings = await search_tempe_meetings(None, pz_start, pz_end, body_group=body_group)
        if not meetings:
            print("No Tempe meetings found.")
            return 0

        if args.limit:
            meetings = meetings[:args.limit]

        print(f"Found {len(meetings)} Tempe meeting(s)")
        if len(meetings) >= 100:
            print(f"  ⚠  OnBase returned the maximum number of results. Meetings at the")
            print(f"     start of the date range may have been silently truncated.")
            print(f"     Consider syncing one year at a time (e.g. --year=2024 --year=2025).")

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)

        # Pre-resolve public_body_id to avoid per-meeting DB lookups
        pb_map = {}
        for pb in session.execute(select(PublicBodyModel)).scalars().all():
            pb_map[pb.body_code] = pb

        # Pre-resolve jurisdiction_id
        from db import Jurisdiction as JurisdictionModel
        jur = session.execute(
            select(JurisdictionModel).where(JurisdictionModel.slug == "tempe")
        ).scalar_one_or_none()
        jur_id = jur.id if jur else JURISDICTION_ID

        def _ensure_tempe_members(session, sup_list):
            """Ensure Tempe council members have BodyMembership rows.
            persist_votes creates the Supervisor rows; this ensures
            BodyMembership records exist with correct roles."""
            pb = session.execute(
                select(PublicBody).where(PublicBody.slug == "tempe-city-council")
            ).scalar_one_or_none()
            if not pb:
                return
            titler_map = {"woods": "Mayor", "garlid": "Vice Mayor"}
            from db import BodyMembership, _ensure_membership
            for sup in sup_list:
                norm = sup.get("normalized_name", "").strip().lower()
                if not norm:
                    continue
                existing = session.execute(
                    select(Supervisor).where(Supervisor.normalized_name == norm)
                ).scalar_one_or_none()
                role = titler_map.get(norm, "Councilmember")
                name = sup.get("name", norm.capitalize())
                person_id = None
                if existing:
                    person_id = existing.id
                else:
                    new_person = Supervisor(
                        name=name,
                        normalized_name=norm,
                    )
                    session.add(new_person)
                    session.flush()
                    person_id = new_person.id

                if person_id:
                    membership = _ensure_membership(session, person_id, "tempe-cc")
                    if membership and role:
                        membership.role = role
            session.flush()

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m.get("meeting_date", "")
            meeting_title = m.get("meeting_title", "")
            meeting_type = m.get("meeting_type", "")
            body_code = m.get("body", PUBLIC_BODY_CODE)

            # Check if already synced (skip complete/no_agenda unless --force)
            db_m = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()

            if db_m and db_m.sync_status in ("complete", "no_agenda") and not args.force:
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: {db_m.sync_status} (skip)")
                if db_m.sync_status == "complete":
                    total_items += db_m.item_count_actual or 0
                continue

            # Canceled meetings — skip immediately
            if m.get("canceled"):
                if db_m:
                    db_m.sync_status = "no_agenda"
                    db_m.last_error = "Meeting was canceled"
                    db_m.last_attempted_at = None
                    db_m.updated_at = dt.datetime.now(dt.timezone.utc)
                else:
                    pb = pb_map.get(body_code)
                    meeting_row = MeetingModel(
                        body=body_code,
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                        meeting_type=meeting_type,
                        meeting_title=meeting_title,
                        source_url=TEMPE_CONFIG.build_meeting_view_url(int(meeting_id)),
                        sync_status="no_agenda",
                        last_error="Meeting was canceled",
                        jurisdiction_id=pb.jurisdiction_id if pb else None,
                        public_body_id=pb.id if pb else None,
                    )
                    session.add(meeting_row)
                session.commit()
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: canceled (no_agenda)")
                continue

            # Ensure meeting row exists
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": meeting_title,
                "source_url": TEMPE_CONFIG.build_meeting_view_url(int(meeting_id)),
            }

            meeting_row = None
            if db_m:
                for key, val in meeting_dict.items():
                    setattr(db_m, key, val)
                db_m.sync_status = "pending"
                db_m.last_attempted_at = None
                db_m.last_error = None
                db_m.retry_count = 0
                session.flush()
                meeting_row = db_m
            else:
                pb = pb_map.get(body_code)
                meeting_row = MeetingModel(
                    body=body_code,
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    meeting_type=meeting_type,
                    meeting_title=meeting_title,
                    source_url=meeting_dict["source_url"],
                    sync_status="pending",
                    jurisdiction_id=pb.jurisdiction_id if pb else None,
                    public_body_id=pb.id if pb else None,
                )
                session.add(meeting_row)
            session.commit()

            # Fetch agenda items via direct HTTP GET (no Playwright)
            update_sync_status(session, body_code, meeting_id, "in_progress")
            session.commit()

            try:
                from scraper.onbase import fetch_agenda_sync, parse_agenda_html
                html = fetch_agenda_sync(TEMPE_CONFIG, int(meeting_id))

                # Tempe OnBase returns a 200 with "Document unavailable" when the
                # agenda hasn't been published yet (future or recently posted meetings).
                # Mark as pending so future syncs retry, unless the meeting is old
                # and has been retried multiple times — then it's likely canceled.
                if "Document unavailable" in html:
                    import datetime as _dt
                    # Re-fetch the meeting row to get current retry_count
                    current_meeting = session.execute(
                        select(MeetingModel).where(
                            MeetingModel.body == body_code,
                            MeetingModel.meeting_id == meeting_id,
                        )
                    ).scalar_one_or_none()
                    retry_count = current_meeting.retry_count if current_meeting else 0
                    try:
                        meeting_age_days = (_dt.date.today() - _dt.date.fromisoformat(meeting_date)).days
                    except (ValueError, TypeError, AttributeError):
                        meeting_age_days = 0

                    if meeting_age_days > 60 and retry_count >= 1:
                        update_sync_status(session, body_code, meeting_id, "no_agenda",
                                           error="Agenda never published (likely canceled)")
                        session.commit()
                        print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: agenda never published (no_agenda)")
                    else:
                        update_sync_status(session, body_code, meeting_id, "pending",
                                           error="Agenda not yet published")
                        session.commit()
                        print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: agenda not yet published (pending)")
                    continue

                items = parse_agenda_html(html, meeting_id, body_code)
                for item in items:
                    item["source_url"] = meeting_dict["source_url"]
                    item["body"] = body_code

                # Propagate consent/non-consent category labels
                from scraper.tempe import _assign_tempe_categories
                _assign_tempe_categories(items)
            except Exception as e:
                update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
                session.commit()
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: FAILED - {e}")
                continue

            if not items:
                # Check if the page has a meeting header but no sections —
                # indicates a procedural meeting (joint meetings, special
                # sessions) with no formal agenda items.
                import re
                has_header = bool(re.search(r"<h1[^>]*>[^<]+</h1>", html))
                has_sections = bool(re.search(r"accessible-section", html))

                if has_header and not has_sections:
                    update_sync_status(session, body_code, meeting_id, "no_agenda",
                                       error="Meeting had no published agenda items")
                    session.commit()
                    print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: no agenda items (no_agenda)")
                else:
                    update_sync_status(session, body_code, meeting_id, "pending",
                                       error="Agenda format not recognized")
                    session.commit()
                    print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: 0 items (pending)")
                continue

            # Build supporting documents (packet PDF, summary PDF)
            supp_docs = []
            packet_url = m.get("agenda_packet_url", "")
            summary_url = m.get("summary_url", "")
            if packet_url:
                supp_docs.append({
                    "agenda_item_id": 0,
                    "agenda_item_number": "0",
                    "document_title": "Agenda Packet",
                    "document_url": packet_url,
                    "document_type": "Packet",
                    "file_name": f"{meeting_id}_packet.pdf",
                    "file_extension": ".pdf",
                })
            if summary_url:
                supp_docs.append({
                    "agenda_item_id": 0,
                    "agenda_item_number": "0",
                    "document_title": "Legal Action Summary",
                    "document_url": summary_url,
                    "document_type": "Summary",
                    "file_name": f"{meeting_id}_summary.pdf",
                    "file_extension": ".pdf",
                })

            # Persist
            replace_meeting_data_safe(
                session, body_code, meeting_id, meeting_dict, items,
                supporting_doc_dicts=supp_docs,
            )
            total_items += len(items)

            detail_items = [i for i in items if i["item_type"] == "item"]

            # Download documents if --download flag is set
            doc_summary = ""
            if getattr(args, "download", False):
                try:
                    from scraper.tempe import download_tempe_documents
                    doc_results = download_tempe_documents(
                        meeting_id, meeting_date,
                        doc_dir=str(ROOT / "data"),
                    )
                    parts = []
                    if doc_results.get("agenda_pdf_path"):
                        parts.append("agenda")
                    if doc_results.get("packet_pdf_path"):
                        parts.append("packet")
                    if parts:
                        doc_summary = " (" + " + ".join(parts) + " downloaded)"
                except Exception as de:
                    log.warning("Document download failed for %s: %s", meeting_id, de)

            # Fetch and persist vote data from Legal Action Summary PDF
            # Only Regular City Council meetings have published summary PDFs
            vote_str = ""
            if body_code == "tempe-cc" and meeting_type == "Regular City Council Meeting":
                try:
                    vote_data = fetch_and_parse_summary(
                        int(meeting_id), meeting_date, meeting_type,
                    )
                    if vote_data["votes"]:
                        persist_votes(
                            session, body_code, meeting_id,
                            vote_data["supervisors"],
                            vote_data["votes"],
                        )
                        # Ensure vote supervisors are registered as public body members
                        _ensure_tempe_members(session, vote_data["supervisors"])
                        vote_str = f" ({len(vote_data['votes'])} votes)"
                except Exception as ve:
                    log.warning("Vote extraction failed for %s: %s", meeting_id, ve)

            ts = time.strftime("%H:%M:%S")
            ts_items_detail = len(items)
            print(f"{ts} [{idx}/{meeting_count}] {meeting_id} {meeting_date}: {ts_items_detail} item(s) ({len(detail_items)} actionable){doc_summary}{vote_str}")

        session.close()
        ts = time.strftime("%H:%M:%S")
        print(f"{ts} Synced {total_items} Tempe agenda items across {meeting_count} meeting(s)")
        return 0


    if args.source in ("pz", "adj", "drain", "health", "tab", "ida") and args.sync:
        from db import get_session, init_db, replace_meeting_data_safe

        init_db()

        # Source-specific dispatch
        if args.source == "pz":
            CID = "9,"
            source_body = "pz"
            source_type = "Planning & Zoning"
            source_label = "P&Z"
            from scraper.pz import build_pz_search_url as build_search_url_fn
            from scraper.pz import extract_pz_meetings as extract_meetings_fn
            from scraper.pz import extract_pz_agenda_items as extract_items_fn
            from scraper.pz import _format_mm_dd_yyyy as fmt_date_fn
            from scraper.pz import _normalize_pz_meeting_title as normalize_title_fn
        elif args.source == "adj":
            CID = "3,"
            source_body = "adj"
            source_type = "Board of Adjustment"
            source_label = "ADJ"
            from scraper.adj import build_adj_search_url as build_search_url_fn
            from scraper.adj import extract_adj_meetings as extract_meetings_fn
            from scraper.adj import extract_adj_agenda_items as extract_items_fn
            from scraper.adj import _format_mm_dd_yyyy as fmt_date_fn
            from scraper.adj import _normalize_adj_meeting_title as normalize_title_fn
        elif args.source == "drain":
            CID = "19,"
            source_body = "drain"
            source_type = "Drainage Review Board"
            source_label = "DRB"
            from scraper.drain import build_drain_search_url as build_search_url_fn
            from scraper.drain import extract_drain_meetings as extract_meetings_fn
            from scraper.drain import extract_drain_agenda_items as extract_items_fn
            from scraper.drain import _format_mm_dd_yyyy as fmt_date_fn
        elif args.source == "health":
            CID = "13,"
            source_body = "health"
            source_type = "Board of Health"
            source_label = "BOH"
            from scraper.health import build_health_search_url as build_search_url_fn
            from scraper.health import extract_health_meetings as extract_meetings_fn
            from scraper.health import extract_health_agenda_items as extract_items_fn
            from scraper.health import _format_mm_dd_yyyy as fmt_date_fn
        elif args.source == "tab":
            CID = "11,"
            source_body = "tab"
            source_type = "Transportation Advisory Board"
            source_label = "TAB"
            from scraper.tab import build_tab_search_url as build_search_url_fn
            from scraper.tab import extract_tab_meetings as extract_meetings_fn
            from scraper.tab import extract_tab_agenda_items as extract_items_fn
            from scraper.tab import _format_mm_dd_yyyy as fmt_date_fn
        elif args.source == "ida":
            CID = ""
            source_body = "ida"
            source_type = "Industrial Development Authority"
            source_label = "IDA"
            from scraper.ida import extract_ida_meetings as extract_meetings_fn
            from scraper.ida import extract_ida_agenda_items as extract_items_fn

            # IDA is a static page — no search URL or date formatting needed
            def fmt_date_fn(x): return x
            def build_search_url_fn(start, end): return "https://mcida.com/about-us/public-meetings/"
        elif args.source == "tempe":
            source_body = "tempe-cc"
            source_type = "City Council"
            source_label = "Tempe"
            from scraper.tempe import (
                search_tempe_meetings as extract_meetings_fn,
                extract_tempe_agenda_items as extract_items_fn,
                normalize_tempe_meeting_title as normalize_title_fn,
                extract_meeting_type_from_title,
                JURISDICTION_ID,
                PUBLIC_BODY_CODE,
            )
            def fmt_date_fn(x): return x
            from scraper.onbase import TEMPE_CONFIG
            def build_search_url_fn(start, end): return TEMPE_CONFIG.build_search_url(start, end)

        # If --meeting-id is provided, bypass search and use direct meeting URL
        if args.meeting_id:
            meeting_id = args.meeting_id
            # Try to get the agenda URL from the database first
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            db_session = get_session()
            existing = db_session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == source_body,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            agenda_url = ""
            meeting_date = ""
            meeting_title = ""
            if existing and existing.source_url:
                # Use the HTML agenda URL from previous sync
                agenda_url = existing.source_url
                meeting_date = existing.meeting_date or ""
                meeting_title = existing.meeting_title or ""
                print(f"Found existing {source_label} meeting {meeting_id} in database")
            else:
                # Construct a best-guess URL. Agenda Center HTML pages use
                # ViewFile/Agenda/<id>?html=true
                if source_body in ("drain", "health", "tab"):
                    agenda_url = f"https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/{meeting_id}?html=true"
                elif source_body == "ida":
                    agenda_url = ""  # IDA uses date-based IDs; agenda_url comes from re-fetching the page
                else:
                    agenda_url = f"https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/{meeting_id}?html=true"
            db_session.close()

            if args.source == "pz":
                clean_title, clean_type = normalize_title_fn(
                    meeting_title, source_type
                )
            elif args.source == "adj":
                clean_title = normalize_title_fn(meeting_title)
                clean_type = source_type
            else:
                clean_title = meeting_title
                clean_type = source_type

            pz_meetings = [Meeting(
                meeting_date=meeting_date,
                meeting_time="",
                meeting_title=clean_title,
                meeting_type=clean_type,
                body=source_body,
                row_text="",
                detail_url="",
                agenda_url=agenda_url,
            )]
            print(f"Syncing {source_label} meeting {meeting_id}...")
        else:
            now = dt.date.today()
            if args.start_date:
                pz_start = fmt_date_fn(args.start_date) or f"{now.month:02d}/01/{now.year}"
            else:
                three_months_ago = now - dt.timedelta(days=90)
                pz_start = f"{three_months_ago.month:02d}/01/{three_months_ago.year}"

            if args.end_date:
                pz_end = fmt_date_fn(args.end_date) or f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"
            else:
                pz_end = f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"

            search_url = build_search_url_fn(pz_start, pz_end)
            print(f"{source_label} search URL: {search_url}")

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.meeting_id:
                    # Skip search when --meeting-id is provided; list already built above
                    pass
                else:
                    pz_meetings = await extract_meetings_fn(page, search_url)

                    if not pz_meetings:
                        print(f"No {source_label} meetings found.")
                        return 0

                if args.limit:
                    pz_meetings = pz_meetings[:args.limit]

                print(f"Found {len(pz_meetings)} {source_label} meeting(s)")

                session = get_session()
                total = 0
                meeting_count = len(pz_meetings)

                for idx, meeting in enumerate(pz_meetings, 1):
                    meeting_dict = {
                        "meeting_id": meeting.meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                        "meeting_title": meeting.meeting_title,
                        "source_url": meeting.agenda_url,
                    }

                    items = await extract_items_fn(page, meeting.agenda_url)

                    # extract_pz_agenda_items now returns real items from the PDF
                    # with supporting_doc_dicts already set. We just need to fix
                    # meeting_id and agenda_item_id, then flatten supporting docs.
                    docs: list[dict] = []
                    for it in items:
                        it["meeting_id"] = meeting.meeting_id
                        it["agenda_item_id"] = f"{meeting.meeting_id}-{it['agenda_item_number']}-item"
                        for sd in it.pop("supporting_doc_dicts", []):
                            sd["meeting_id"] = meeting.meeting_id
                            sd["agenda_item_number"] = int(it["agenda_item_number"])
                            sd["agenda_item_id"] = int(it["agenda_item_number"])
                            docs.append(sd)

                    if items:
                        replace_meeting_data_safe(
                            session, meeting.body, meeting.meeting_id, meeting_dict, items,
                            supporting_doc_dicts=docs,
                        )

                        # Persist item details (PZ-specific) to database
                        if args.source == "pz":
                            from db import PZItemDetail, AgendaItem
                            from sqlalchemy import select
                            try:
                                # Delete old details for this meeting
                                session.execute(
                                    PZItemDetail.__table__.delete().where(
                                        PZItemDetail.body == meeting.body,
                                        PZItemDetail.meeting_id == meeting.meeting_id,
                                    )
                                )
                                # Look up agenda item DB IDs after persist
                                db_items = {
                                    row.agenda_item_number: row.id
                                    for row in session.execute(
                                        select(AgendaItem.id, AgendaItem.agenda_item_number)
                                        .where(
                                            AgendaItem.body == meeting.body,
                                            AgendaItem.meeting_id == meeting.meeting_id,
                                        )
                                    ).all()
                                }
                                # Insert new details
                                for it in items:
                                    if it.get("pz_project_name"):
                                        item_num = int(it.get("agenda_item_number", 0))
                                        detail = PZItemDetail(
                                            body=meeting.body,
                                            agenda_item_id=db_items.get(item_num),
                                            meeting_id=meeting.meeting_id,
                                            agenda_item_number=item_num,
                                            case_number=it.get("case_number", ""),
                                            district=it.get("pz_district"),
                                            project_name=it.get("pz_project_name"),
                                            applicant=it.get("pz_applicant"),
                                            request=it.get("pz_request"),
                                            location=it.get("pz_location"),
                                            recommendation=it.get("pz_recommendation"),
                                            presented_by=it.get("pz_presented_by"),
                                            staff_report_url=it.get("staff_report_url"),
                                        )
                                        session.add(detail)
                                session.commit()
                            except Exception as pz_err:
                                print(f"    PZ detail persist skipped: {pz_err}")
                                session.rollback()

                        total += len(items)
                        doc_summary = f", {len(docs)} doc(s)" if docs else ""

                        # ── PZ Minutes / Votes extraction (PZ-specific only) ──
                        vote_summary = ""
                        if args.source == "pz":
                            minutes_url = meeting.agenda_url.replace(
                                "/Agenda/", "/Minutes/"
                            ).replace("?html=true", "")
                            try:
                                import urllib.request
                                from pathlib import Path
                                from scraper.pz_minutes import parse_pz_minutes_pdf
                                from db import persist_votes

                                pdf_req = urllib.request.Request(
                                    minutes_url,
                                    headers={"User-Agent":
                                        "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
                                )
                                with urllib.request.urlopen(pdf_req, timeout=30) as pdf_resp:
                                    pdf_path = Path(f"/tmp/pz_min_{meeting.meeting_id}.pdf")
                                    pdf_path.write_bytes(pdf_resp.read())

                                minutes_data = parse_pz_minutes_pdf(str(pdf_path))
                                pdf_path.unlink(missing_ok=True)

                                if not minutes_data.get("votes"):
                                    vote_summary = ", votes=none"
                                else:
                                    supervisors: list[dict] = [
                                        {"name": name,
                                         "normalized_name": name.lower(),
                                         "present": True}
                                        for name in minutes_data["members_present"]
                                    ]
                                    supervisors.extend([
                                        {"name": name,
                                         "normalized_name": name.lower(),
                                         "present": False}
                                        for name in minutes_data["members_absent"]
                                    ])

                                    votes_list: list[dict] = []
                                    aye_names_lower = set()
                                    nay_names_lower = set()

                                    for v in minutes_data["votes"]:
                                        aye_names_lower = {
                                            n.lower() for n in v.get("ayes", []) if n
                                        }
                                        nay_names_lower = {
                                            n.lower() for n in v.get("nays", []) if n
                                        }

                                        for cn in v.get("c_numbers", []):
                                            matched_num = 0
                                            for it in items:
                                                it_case = (
                                                    it.get("case_number") or
                                                    it.get("c_number") or ""
                                                ).upper()
                                                if it_case == cn.upper():
                                                    matched_num = int(
                                                        it.get("agenda_item_number", 0)
                                                    )
                                                    break

                                            if not matched_num:
                                                continue

                                            supervisor_votes = []
                                            for sup in supervisors:
                                                name_lower = sup["normalized_name"]
                                                name_words = set(name_lower.split())
                                                voted_aye = bool(name_words & aye_names_lower)
                                                voted_nay = bool(name_words & nay_names_lower)
                                                if voted_aye and not voted_nay:
                                                    supervisor_votes.append({
                                                        "name": sup["name"],
                                                        "vote": "aye",
                                                    })
                                                elif voted_nay and not voted_aye:
                                                    supervisor_votes.append({
                                                        "name": sup["name"],
                                                        "vote": "nay",
                                                    })

                                            votes_list.append({
                                                "agenda_item_number": matched_num,
                                                "c_number": cn,
                                                "motion_result":
                                                    v.get("motion_result", "unknown"),
                                                "vote_text": v.get("vote_text", ""),
                                                "conditions": v.get("conditions"),
                                                "supervisor_votes": supervisor_votes,
                                            })

                                    if votes_list:
                                        vote_session = get_session()
                                        try:
                                            vote_count = persist_votes(
                                                vote_session, meeting.body,
                                                meeting.meeting_id,
                                                supervisors, votes_list,
                                            )
                                            vote_summary = f", votes={vote_count}"
                                        finally:
                                            vote_session.close()
                                    else:
                                        vote_summary = ", votes=0(unmatched)"
                            except urllib.error.HTTPError as e:
                                vote_summary = ", votes=na" if e.code == 404 \
                                    else f", votes=error({e.code})"
                            except Exception as ve:
                                vote_summary = f", votes=error"

                        ts = time.strftime("%H:%M:%S")
                        print(f"{ts} [{idx}/{meeting_count}] {meeting.meeting_id} {meeting.meeting_date}: {len(items)} item(s){doc_summary}{vote_summary}")

                session.close()
                ts = time.strftime("%H:%M:%S")
                print(f"{ts} Synced {total} {source_label} agenda items across {len(pz_meetings)} meeting(s)")
            finally:
                await browser.close()
        return 0

    if args.status:
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
        return 0

    if args.failed:
        from db import get_session, get_failed_meetings, get_meetings_by_status
        session = get_session()
        failed_statuses = ["failed", "partial", "manual_review"] if args.include_manual_review else ["failed", "partial"]
        body_filter = args.source if hasattr(args, 'source') else "bos"
        meetings = get_meetings_by_status(session, body_filter, failed_statuses)
        session.close()
        if not meetings:
            print("No meetings with issues.")
            return 0
        print(f"{'ID':>6}  {'Date':<12}  {'Status':<12}  {'Retries':>7}  {'Error'}")
        print(f"{'─' * 6}  {'─' * 12}  {'─' * 12}  {'─' * 7}  {'─' * 40}")
        for m in meetings:
            err = (m.last_error or "")[:60]
            print(f"{m.meeting_id:>6}  {m.meeting_date:<12}  {m.sync_status:<12}  {m.retry_count:>7}  {err}")
        return 0

    if args.persist:
        from db import get_session, persist_meeting
        import csv

        csv_path = AGENDA_ITEMS_CSV
        if not csv_path.exists():
            print(f"No agenda items CSV found at {csv_path}. Run --extract-agenda-items first.")
            return 1

        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            print("CSV is empty.")
            return 1

        # Group by meeting_id + meeting_date
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            key = (row.get("meeting_id", ""), row.get("meeting_date", ""))
            groups.setdefault(key, []).append(row)

        session = get_session()
        total = 0
        errors = 0
        for (meeting_id, meeting_date), items in sorted(groups.items()):
            first = items[0]
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": first.get("meeting_type", ""),
                "meeting_title": first.get("agenda_item_section", "") or meeting_id,
                "source_url": first.get("source_url", ""),
            }
            try:
                body = first.get("body", "bos") if "body" in first else args.source if hasattr(args, 'source') else "bos"
                count = persist_meeting(session, body, meeting_id, items)
                total += count
                print(f"  {meeting_id} {meeting_date}: {count} items")
            except Exception as e:
                errors += 1
                print(f"  {meeting_id} {meeting_date}: FAILED - {e}")

        session.close()
        print(f"Persisted {total} agenda items across {len(groups)} meeting(s)")
        if errors:
            print(f"{errors} meeting(s) had errors")
            return 1
        return 0

    if args.sync:
        from db import get_session, init_db, persist_meeting

        init_db()

        if not args.from_file and args.meeting_id and not args.offline:
            meeting_id = args.meeting_id
            source_url = (
                "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                f"?id={meeting_id}&doctype=1"
            )
            log.info(f"Syncing BOS meeting {meeting_id}...")
            meeting_prefix = f"BOS meeting_id={meeting_id}"

            # Use metadata from --meeting-* args if provided (parallel workers),
            # otherwise construct the source URL from the meeting ID and rely
            # on page-level metadata extraction as a fallback.
            meeting_date = getattr(args, "meeting_date", None) or ""
            meeting_type = getattr(args, "meeting_type", None) or ""
            meeting_title = getattr(args, "meeting_title", None) or ""
            meeting_url = getattr(args, "meeting_url", None) or ""

            if meeting_url:
                source_url = meeting_url
            else:
                source_url = (
                    "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                    f"?id={meeting_id}&doctype=1"
                )

            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": meeting_title,
                "source_url": source_url,
            }
            extract_meeting = {
                "document_url": source_url,
                "agenda_url": source_url,
                "record_id": meeting_id,
                "meeting_id": meeting_id,
                "record_date": meeting_date,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
            }

            from db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe

            session = get_session()
            try:
                # Ensure meeting row exists
                meeting = create_or_get_meeting(session, args.source, meeting_dict)
                session.commit()

                # Cancel detection
                from db import is_canceled_meeting, mark_meeting_canceled
                if is_canceled_meeting(meeting_dict):
                    mark_meeting_canceled(session, args.source, meeting_id)
                    log.info("%s canceled (no_agenda)", meeting_prefix)
                    session.close()
                    return 0

                # Check if we should skip complete
                if args.skip_complete and meeting.sync_status == "complete":
                    log.info("%s status=complete skipping", meeting_prefix)
                    session.close()
                    return 0

                async_playwright = get_async_playwright()
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=not args.headed)
                    page = await browser.new_page()
                    page.set_default_timeout(60000)
                    try:
                        # Mark as attempted
                        update_sync_status(session, args.source, meeting_id, meeting.sync_status)
                        session.commit()

                        # Establish OnBase server session by visiting the source
                        # page first.  The agenda page is a SPA that relies on
                        # a session cookie set by the search/home page; without
                        # it the AJAX call that populates agendaView fails.
                        try:
                            await page.goto(SOURCE_PAGE, wait_until="domcontentloaded", timeout=30000)
                            await page.goto(
                                SEARCH_BASE + "?dropid=11",
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                        except Exception:
                            log.warning("%s session_bootstrap failed, proceeding anyway", meeting_prefix)

                        retry = args.retry_count

                        # Extract agenda items with retry
                        async def do_extract_items():
                            return await extract_agenda_items_for_meeting(page, extract_meeting)

                        items = await retry_with_backoff(
                            lambda: do_extract_items(),
                            max_attempts=retry,
                            label=f"items {meeting_id}",
                        )

                        # After page loads, extract meeting metadata from the page
                        # and update the meeting_dict/extract_meeting with real values
                        page_meta = await extract_meeting_metadata_from_page(page, source_url)
                        if page_meta.get("meeting_date"):
                            meeting_dict["meeting_date"] = page_meta["meeting_date"]
                            extract_meeting["meeting_date"] = page_meta["meeting_date"]
                            extract_meeting["record_date"] = page_meta["meeting_date"]
                        if page_meta.get("meeting_type"):
                            meeting_dict["meeting_type"] = page_meta["meeting_type"]
                            extract_meeting["meeting_type"] = page_meta["meeting_type"]
                        if page_meta.get("meeting_title"):
                            meeting_dict["meeting_title"] = page_meta["meeting_title"]
                        if not items:
                            page_state = await get_page_state_summary(page)
                            if await is_image_based_agenda(page):
                                status = "manual_review"
                                error = "Unsupported agenda format: page loaded but no parseable agenda items found; possible image/scanned agenda"
                                log.warning("%s items=0 image_scanned page_state=%s", meeting_prefix, page_state)
                            else:
                                status = "failed"
                                error = "No agenda items found on page"
                                log.warning("%s items=0 not_found page_state=%s", meeting_prefix, page_state)
                            update_sync_status(
                                session, args.source, meeting_id, status,
                                error=error,
                            )
                            session.commit()
                            return 1

                        # Extract supporting documents with retry
                        async def do_extract_docs():
                            return await extract_supporting_documents_dynamic_concurrent(
                                page, items, source_url, concurrency=5
                            )

                        docs = []
                        docs_ok = True
                        try:
                            docs = await retry_with_backoff(
                                lambda: do_extract_docs(),
                                max_attempts=retry,
                                label=f"docs {meeting_id}",
                            )
                        except Exception as e:
                            docs_ok = False
                            log.warning("%s docs_extraction_failed error=%s", meeting_prefix, str(e)[:200])

                        if not docs_ok:
                            # Items succeeded but docs failed - partial
                            replace_meeting_data_safe(
                                session, args.source, meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            update_sync_status(
                                session, args.source, meeting_id, "partial",
                                item_count_expected=len(items),
                                item_count_actual=len(items),
                                supporting_doc_count=len(docs),
                                items_extracted=True,
                                supporting_docs_extracted=False,
                                error="Supporting document extraction failed",
                            )
                            session.commit()
                            log.info("%s items=%d docs=partial", meeting_prefix, len(items))
                        else:
                            replace_meeting_data_safe(
                                session, args.source, meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            session.commit()

                            # Build summary line
                            summary_parts = [f"{len(items)} items", f"{len(docs)} docs"]

                            # Extract and persist votes from the summary page
                            try:
                                summary_url = source_url.replace("doctype=1", "doctype=3")
                                from db import persist_votes
                                vote_items = [
                                    {"agenda_item_number": it.get("agenda_item_number", ""),
                                     "c_number": it.get("c_number", "")}
                                    for it in items
                                ]
                                supervisors, votes = await extract_votes_from_summary(
                                    page, summary_url, vote_items
                                )
                                if votes or supervisors:
                                    vote_count = persist_votes(session, args.source, meeting_id, supervisors, votes)
                                    session.commit()
                                    summary_parts.append(f"{vote_count} votes")
                            except Exception:
                                pass

                            log.info("%s %s", meeting_prefix, ", ".join(summary_parts))

                    except Exception as e:
                        # Items extraction failed
                        update_sync_status(
                            session, args.source, meeting_id, "failed",
                            error=str(e)[:500],
                        )
                        session.commit()
                        log.error("%s failed error=%s", meeting_prefix, str(e)[:500])
                        return 1
                    finally:
                        await browser.close()
            except Exception as e:
                session.rollback()
                raise
            finally:
                session.close()
            return 0

        if args.offline and args.meeting_id:
            # Offline: auto-discover HTML file by meeting ID
            meeting_id = args.meeting_id
            search_dirs = [
                ROOT / "data" / "agenda-html",
                ROOT / "tests" / "fixtures",
                ROOT / "tests" / "fixtures" / "agendas",
                ROOT,
            ]
            found = None
            for d in search_dirs:
                if not d.exists():
                    continue
                for pattern in [f"*{meeting_id}*.html", f"*{meeting_id}*.htm"]:
                    candidates = list(d.glob(pattern))
                    if candidates:
                        found = candidates[0]
                        break
                if found:
                    break

            if not found:
                raise SystemExit(
                    f"No HTML file found for meeting {meeting_id}. "
                    "Save the agenda HTML to one of:"
                    f"\n  - data/agenda-html/{meeting_id}.html"
                    f"\n  - tests/fixtures/"
                )

            print(f"Offline sync: using {found}")
            # Delegate to the --from-file handler by rewriting args
            args.from_file = str(found)
            # Fall through to the from-file block below

        if args.from_file:
            # Sync from a local HTML file — no server needed
            # Check this before --meeting-id so --from-file takes priority
            # when both flags are given
            fixture_path = Path(args.from_file)
            if not fixture_path.exists():
                raise SystemExit(f"File not found: {fixture_path}")

            # Parse meeting metadata from filename
            # Supports:
            #   {date}_{type}_{id}_agenda.html  (e.g. 2025-01-29_formal_4449_agenda.html)
            #   {id}_{type}_{date}.html         (e.g. 4667_formal_2026-04-22.html)
            fn_match = re.match(
                r"(\d{4}-\d{2}-\d{2})_(.+?)_(\d+)_agenda\.html"
                r"|(\d+)_(.+?)_(\d{4}-\d{2}-\d{2})\.html",
                fixture_path.name,
            )
            if fn_match:
                groups = fn_match.groups()
                if groups[0]:  # date_type_id_agenda pattern
                    meeting_date = groups[0]
                    meeting_type = groups[1].replace("_", " ").title()
                    meeting_id = groups[2]
                else:  # id_type_date pattern
                    meeting_id = groups[3]
                    meeting_type = groups[4].replace("_", " ").title()
                    meeting_date = groups[5]
            elif args.meeting_id:
                meeting_id = args.meeting_id
                meeting_date = ""
                meeting_type = ""
            else:
                raise SystemExit(
                    f"Could not parse meeting info from filename '{fixture_path.name}'. "
                    "Use --meeting-id to specify the meeting ID, "
                    "or rename the file to: YYYY-MM-DD_type_ID_agenda.html"
                )

            html = fixture_path.read_text(encoding="utf-8")
            source_url = (
                "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                f"?id={meeting_id}&doctype=1"
            )

            meeting = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
            }
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": f"Meeting {meeting_id}",
                "source_url": source_url,
            }

            items = parse_agenda_items_from_html(html, source_url, meeting)
            if not items:
                print(f"  {meeting_id}: 0 items (no agenda items found in file)")
                return 1

            # Extract supporting documents from the HTML
            docs = extract_supporting_documents_from_items(html, items, source_url)
            if docs:
                print(f"  {meeting_id}: {len(docs)} supporting document(s) found")

            session = get_session()
            count = persist_meeting(session, args.source, meeting_id, items, supporting_doc_dicts=docs)
            session.close()
            print(f"  {meeting_id} {meeting_date}: {count} items synced from '{fixture_path.name}'")
            return 0

        if not args.start_date or not args.end_date:
            if not args.retry_failed:
                raise SystemExit("--start-date and --end-date (or --date) are required for --sync, or use --meeting-id")
            # --retry-failed without dates: fetch meetings from DB by status
            log.info("[retry] mode started")
            from db import get_session, init_db, get_meetings_by_status
            init_db()
            session = get_session()
            retry_statuses = ["failed", "partial", "pending"]
            if args.include_manual_review:
                retry_statuses.append("manual_review")
            db_meetings = get_meetings_by_status(session, args.source, retry_statuses, force=False)
            session.close()
            if not db_meetings:
                log.info("[retry] no failed agendas found")
                return 0
            log.info("[retry] found %d failed agendas", len(db_meetings))
            meetings = [
                Meeting(
                    meeting_date=m.meeting_date or "",
                    meeting_time="",
                    meeting_title=m.meeting_title or "",
                    meeting_type=m.meeting_type,
                    body=m.body or args.source,
                    row_text="",
                    detail_url="",
                    agenda_url=m.source_url,
                )
                for m in db_meetings
            ]
        else:
            start_date = parse_date(args.start_date)
            end_date = parse_date(args.end_date)
            if end_date < start_date:
                raise SystemExit("--end-date must be on or after --start-date")
            search_url = build_search_url(start_date, end_date)
            print(f"Agenda Online search URL: {search_url}")

        from db import get_session, init_db, persist_meeting, get_meetings_by_date_range
        from db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe

        init_db()
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                # When --retry-failed was used without dates, meetings is already
                # populated from the database — skip the search.
                session = get_session()
                if 'meetings' not in dir() or not meetings:
                    await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
                    await page.goto(search_url, wait_until="domcontentloaded")
                    search_meetings = await extract_meetings(page, search_url)

                    if args.force:
                        db_meetings = get_meetings_by_date_range(
                            session,
                            args.source,
                            start_date.isoformat(),
                            end_date.isoformat(),
                        )
                        seen_ids: set[str] = set()
                        merged: list[Meeting] = []
                        for m in search_meetings:
                            if m.meeting_id not in seen_ids:
                                seen_ids.add(m.meeting_id)
                                merged.append(m)
                        for db_m in db_meetings:
                            mid = db_m.meeting_id
                            if mid not in seen_ids:
                                seen_ids.add(mid)
                                merged.append(Meeting(
                                    meeting_date=db_m.meeting_date,
                                    meeting_time="",
                                    meeting_title=db_m.meeting_title,
                                    meeting_type=db_m.meeting_type,
                                    body=db_m.body if hasattr(db_m, 'body') and db_m.body else "bos",
                                    row_text="",
                                    detail_url="",
                                    agenda_url=db_m.source_url,
                                ))
                        meetings = merged
                    else:
                        meetings = search_meetings

                if not meetings:
                    print("No meetings found.")
                    return 0

                if args.limit is not None:
                    meetings = meetings[: args.limit]
                meeting_count = len(meetings)

                # Parallel mode: spawn one subprocess per meeting
                parallel = getattr(args, "parallel", 1)
                if parallel > 1 and meeting_count > 1 and not getattr(args, "meeting_id", None):
                    import subprocess, sys as _sys
                    import asyncio as _asyncio
                    p_count = min(parallel, meeting_count)
                    log.info("Parallel mode: %d meetings, %d concurrent workers", meeting_count, p_count)

                    script = _sys.argv[0]
                    base_cmd = ["python3", script, "bos", "--sync", "--force"] if args.force else ["python3", script, "bos", "--sync"]
                    if getattr(args, 'headed', False):
                        base_cmd.append("--headed")

                    # Filter queue based on sync status
                    from db import Meeting as MeetingModel
                    from sqlalchemy import select

                    retry_statuses = ["failed", "partial", "pending"]
                    if getattr(args, 'include_manual_review', False):
                        retry_statuses.append("manual_review")

                    # Pre-load sync statuses for all meetings
                    status_map = {}
                    try:
                        db_session = get_session()
                        for m in meetings:
                            row = db_session.execute(
                                select(MeetingModel.sync_status).where(
                                    MeetingModel.body == "bos",
                                    MeetingModel.meeting_id == m.meeting_id,
                                )
                            ).scalar_one_or_none()
                            if row:
                                status_map[m.meeting_id] = row
                        db_session.close()
                    except Exception:
                        pass

                    # Build queue filtered by status
                    queue = []
                    skipped_status = 0
                    for m in meetings:
                        status = status_map.get(m.meeting_id, "")
                        # Determine if we should process this meeting
                        should_process = True
                        if args.retry_failed and not args.force:
                            should_process = status in retry_statuses or status == ""
                        elif not args.force and status == "complete":
                            should_process = True  # parallel mode processes all by default
                        if not should_process:
                            skipped_status += 1
                            continue
                        queue.append({
                            "id": m.meeting_id,
                            "date": m.meeting_date or "",
                            "type": m.meeting_type or "",
                            "title": m.meeting_title or "",
                            "url": m.agenda_url or "",
                        })

                    active: list[tuple[dict, subprocess.Popen]] = []
                    ok = 0
                    errs = 0
                    idx = 0
                    total_queue = len(queue)

                    while idx < len(queue) or active:
                        while len(active) < p_count and idx < len(queue):
                            item = queue[idx]
                            cmd = list(base_cmd) + [
                                "--meeting-id", item["id"],
                                "--meeting-date", item["date"],
                                "--meeting-type", item["type"],
                                "--meeting-title", item["title"],
                            ]
                            log.info("  [%d/%d] Worker %s starting (%s %s)", idx + 1, total_queue, item["id"], item["date"], item["type"])
                            active.append((item, subprocess.Popen(cmd)))
                            idx += 1

                        await _asyncio.sleep(5)

                        still_active: list = []
                        for item, proc in active:
                            rc = proc.poll()
                            if rc is not None:
                                done_so_far = ok + errs + 1
                                log.info("  [%d/%d] Worker %s finished code=%d", done_so_far, total_queue, item["id"], rc)
                                if rc == 0:
                                    ok += 1
                                else:
                                    errs += 1
                            else:
                                still_active.append((item, proc))
                        active = still_active

                    log.info("Parallel done. %d OK, %d failed out of %d", ok, errs, total_queue)
                    if errs:
                        return 1
                    return 0

                total = 0
                errors = 0
                skipped = 0
                for idx, meeting in enumerate(meetings, 1):
                    meeting_prefix = f"[{idx}/{meeting_count}]"
                    meeting_t0 = time.monotonic()
                    meeting_type_str = meeting.meeting_type or ""
                    meeting_title_str = (meeting.meeting_title or meeting_type_str)[:40]
                    log.info(
                        "%s meeting_id=%s date=%s type=%s",
                        meeting_prefix, meeting.meeting_id, meeting.meeting_date, meeting_title_str,
                    )

                    meeting_dict = {
                        "meeting_id": meeting.meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                        "meeting_title": meeting.meeting_title,
                        "source_url": meeting.agenda_url,
                    }

                    extract_meeting = {
                        "document_url": meeting.agenda_url,
                        "agenda_url": meeting.agenda_url,
                        "record_id": meeting.meeting_id,
                        "meeting_id": meeting.meeting_id,
                        "record_date": meeting.meeting_date,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                    }

                    # Ensure meeting row exists
                    db_meeting = create_or_get_meeting(session, args.source, meeting_dict)
                    session.commit()

                    # Canceled meeting detection — applies to meetings from
                    # search results, retry-failed, and the --meeting-id path.
                    from db import is_canceled_meeting, mark_meeting_canceled
                    if is_canceled_meeting(meeting_dict):
                        mark_meeting_canceled(session, args.source, meeting_id)
                        log.info("%s canceled (no_agenda)", meeting_prefix)
                        skipped += 1
                        continue

                    # Determine which statuses to retry
                    retry_statuses = ["failed", "partial", "pending"]
                    if args.include_manual_review:
                        retry_statuses.append("manual_review")
                    # When --retry-failed is used, only process retry_statuses
                    # When --force is used, process everything
                    # When neither, skip complete
                    if not args.force and args.retry_failed:
                        if db_meeting.sync_status not in retry_statuses:
                            log.info("%s skip reason=already_complete elapsed=%.1fs", meeting_prefix, time.monotonic() - meeting_t0)
                            skipped += 1
                            continue
                    elif not args.force and db_meeting.sync_status == "complete":
                        skipped += 1
                        continue
                    elif args.retry_failed and db_meeting.sync_status not in retry_statuses:
                        skipped += 1
                        continue

                    try:
                        log.info("%s phase=load_cached_agenda started", meeting_prefix)
                        _phase_t0 = time.monotonic()

                        # Extract agenda items with retry (server-side AJAX
                        # failures are intermittent; retry often recovers)
                        _retry_count = getattr(args, "retry_count", 3)
                        async def _do_extract():
                            return await extract_agenda_items_for_meeting(page, extract_meeting)
                        items = await retry_with_backoff(
                            _do_extract,
                            max_attempts=_retry_count,
                            label=f"items {meeting.meeting_id}",
                        )
                        log.info(
                            "%s phase=parse_agenda_items done items=%d elapsed=%.1fs",
                            meeting_prefix, len(items), time.monotonic() - _phase_t0,
                        )
                        if not items:
                            page_state = await get_page_state_summary(page)
                            if await is_image_based_agenda(page):
                                status = "manual_review"
                                error = "Unsupported agenda format: page loaded but no parseable agenda items found; possible image/scanned agenda"
                                log.warning(
                                    "%s phase=parse_agenda_items result=image_scanned status=%s page_state=%s",
                                    meeting_prefix, status, page_state,
                                )
                            else:
                                status = "failed"
                                error = "No agenda items found"
                                log.warning(
                                    "%s phase=parse_agenda_items result=no_items status=%s page_state=%s",
                                    meeting_prefix, status, page_state,
                                )
                            update_sync_status(
                                session, args.source, meeting.meeting_id, status,
                                error=error,
                            )
                            session.commit()
                            if status == "failed":
                                errors += 1
                            continue

                        log.info("%s phase=discover_documents started", meeting_prefix)
                        _doc_t0 = time.monotonic()
                        docs = await extract_supporting_documents_dynamic_concurrent(
                            page, items, meeting.agenda_url, concurrency=5
                        )
                        log.info(
                            "%s phase=discover_documents done item_docs=%d elapsed=%.1fs",
                            meeting_prefix, len(docs), time.monotonic() - _doc_t0,
                        )

                        try:
                            log.info("%s phase=persist started", meeting_prefix)
                            _persist_t0 = time.monotonic()
                            replace_meeting_data_safe(
                                session, args.source, meeting.meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            total += len(items)
                            log.info(
                                "%s complete items=%d docs=%d elapsed=%.1fs",
                                meeting_prefix, len(items), len(docs), time.monotonic() - meeting_t0,
                            )

                            # Extract votes from the summary page
                            try:
                                _vote_t0 = time.monotonic()
                                summary_url = meeting.agenda_url.replace("doctype=1", "doctype=3")
                                vote_items = [
                                    {"agenda_item_number": it.get("agenda_item_number", ""),
                                     "c_number": it.get("c_number", "")}
                                    for it in items
                                ]
                                from db import persist_votes
                                supervisors, votes = await extract_votes_from_summary(
                                    page, summary_url, vote_items
                                )
                                if votes:
                                    vote_count = persist_votes(session, args.source, meeting.meeting_id, supervisors, votes)
                                    log.info(
                                        "%s votes=%d elapsed=%.1fs",
                                        meeting_prefix, vote_count, time.monotonic() - _vote_t0,
                                    )
                                elif supervisors:
                                    vote_count = persist_votes(session, args.source, meeting.meeting_id, supervisors, votes)
                                    log.info(
                                        "%s no-item-votes supervisors=%d elapsed=%.1fs",
                                        meeting_prefix, len(supervisors), time.monotonic() - _vote_t0,
                                    )
                            except Exception as ve:
                                log.warning("%s vote-extraction skipped: %s", meeting_prefix, str(ve)[:200])
                                # Roll back the session so the next meeting doesn't hit PendingRollbackError
                                session.rollback()

                        except Exception as e:
                            update_sync_status(
                                session, args.source, meeting.meeting_id, "failed",
                                error=str(e)[:500],
                            )
                            session.commit()
                            log.error(
                                "%s persist failed error=%s elapsed=%.1fs",
                                meeting_prefix, str(e)[:200], time.monotonic() - meeting_t0,
                            )
                            errors += 1
                    except Exception as e:
                        # Pre-extraction failure
                        update_sync_status(
                            session, args.source, meeting.meeting_id, "failed",
                            error=str(e)[:500],
                        )
                        session.commit()
                        print(f"  {meeting.meeting_id} {meeting.meeting_date}: FAILED - {e}")
                        errors += 1

                session.close()
                print(f"Synced {total} agenda items across {len(meetings)} meeting(s)")
                if skipped:
                    print(f"{skipped} meeting(s) skipped (status=complete), use --force to re-sync")
                if errors:
                    print(f"{errors} meeting(s) had errors")
                    return 1
            finally:
                await browser.close()
        return 0

    # Standalone --sync-votes: re-run vote extraction for already-synced meetings
    if args.sync_votes:
        from db import get_session, init_db, persist_votes

        init_db()

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.meeting_id:
                    meeting_id = args.meeting_id
                    summary_url = (
                        "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                        f"?id={meeting_id}&doctype=3"
                    )
                    print(f"Extracting votes from meeting {meeting_id}...")
                    print(f"  Summary URL: {summary_url}")

                    # Load agenda items for C-number matching
                    from db import Meeting as MeetingModel, AgendaItem
                    from sqlalchemy import select
                    session = get_session()
                    body = args.source if hasattr(args, 'source') else "bos"
                    try:
                        meeting = session.execute(
                            select(MeetingModel).where(
                                MeetingModel.body == body,
                                MeetingModel.meeting_id == meeting_id,
                            )
                        ).scalar_one_or_none()
                        if not meeting:
                            print(f"  {body} meeting {meeting_id} not found in database. Run --sync first.")
                            return 1
                        db_items = session.execute(
                            select(AgendaItem)
                            .where(
                                AgendaItem.body == body,
                                AgendaItem.meeting_id == meeting_id,
                            )
                            .order_by(AgendaItem.agenda_item_number)
                        ).scalars().all()
                        agenda_items = [
                            {
                                "agenda_item_number": str(it.agenda_item_number),
                                "c_number": it.c_number or "",
                            }
                            for it in db_items
                        ]
                        print(f"  Found {len(agenda_items)} agenda items for C-number matching")
                    except Exception as e:
                        print(f"  WARNING: Could not load agenda items: {e}")
                        agenda_items = []
                    finally:
                        session.close()

                    supervisors, votes = await extract_votes_from_summary(
                        page, summary_url, agenda_items
                    )

                    if not votes:
                        print(f"  No vote results found in summary for meeting {meeting_id}")
                        if supervisors:
                            print(f"  Found {len(supervisors)} supervisors present")
                        return 0

                    print(f"  Found {len(supervisors)} supervisor(s)")
                    for sup in supervisors:
                        district_str = f", District {sup['district']}" if sup.get('district') else ""
                        role_str = f" ({sup['role']})" if sup.get('role') else ""
                        print(f"    {sup['name']}{district_str}{role_str}")

                    print(f"  Found {len(votes)} item(s) with votes")
                    for v in votes:
                        c_str = f" ({v.get('c_number', '')})" if v.get('c_number') else ""
                        sv_summary = ", ".join(
                            f"{sv['name']}: {sv['vote']}"
                            for sv in v.get("supervisor_votes", [])
                        )
                        print(f"    #{v['agenda_item_number']}{c_str}: {v.get('motion_result', 'unknown')}")
                        if sv_summary:
                            print(f"      {sv_summary}")

                    # Persist to database
                    session = get_session()
                    try:
                        vote_count = persist_votes(session, body, meeting_id, supervisors, votes)
                        print(f"  Persisted {vote_count} vote record(s)")
                    finally:
                        session.close()
                else:
                    print("--sync-votes requires --meeting-id to specify a meeting")
                    return 1
            finally:
                await browser.close()
        return 0

    if args.count_agenda_items or args.list_agenda_items:
        if not args.start_date or not args.end_date:
            raise SystemExit("--start-date and --end-date are required")
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
        if end_date < start_date:
            raise SystemExit("--end-date must be on or after --start-date")

        search_url = build_search_url(start_date, end_date)
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
                await page.goto(search_url, wait_until="domcontentloaded")
                meetings = await extract_meetings(page, search_url)

                if not meetings:
                    print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                    return 0

                if args.limit is not None:
                    meetings = meetings[: args.limit]

                if args.count_agenda_items:
                    print(f"{'ID':>6}  {'Date':<12}  {'Count':>5}  {'Title'}")
                    print(f"{'------':>6}  {'------------':<12}  {'-----':>5}  {'-----'}")
                    total = 0
                    for meeting in meetings:
                        count = await count_agenda_items_for_meeting(page, meeting.agenda_url)
                        total += count
                        print(f"{meeting.meeting_id:>6}  {meeting.meeting_date:<12}  {count:>5}  {meeting.meeting_title}")
                    print()
                    print(f"{len(meetings)} meeting(s), {total} total items")
                else:
                    for meeting in meetings:
                        items = await extract_agenda_item_titles(page, meeting.agenda_url)
                        print()
                        print(f"{'=' * 70}")
                        print(f"{meeting.meeting_id}  {meeting.meeting_date}  {meeting.meeting_type}  {meeting.meeting_title}")
                        print(f"{len(items)} items")
                        print(f"{'=' * 70}")
                        for num, title in items:
                            print(f"  {num:>4}.  {title}")
                    print()
                    print(f"{len(meetings)} meeting(s)")
            finally:
                await browser.close()
        return 0

    if args.extract_agenda_items:
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.debug_agenda_html:
                    meeting_rows = read_agenda_metadata_rows()
                    if meeting_rows:
                        await page.goto((meeting_rows[0].get("document_url") or meeting_rows[0].get("agenda_url") or "").strip(), wait_until="domcontentloaded")
                        await page.wait_for_timeout(1000)
                        await write_agenda_debug_files(page, meeting_rows[0])
                start_date = parse_date(args.start_date) if args.start_date else None
                end_date = parse_date(args.end_date) if args.end_date else None
                wrote = await extract_agenda_items_from_metadata(
                    page, start_date=start_date, end_date=end_date, limit=args.limit
                )
                print(f"Extracted {wrote} agenda item row(s)")
            finally:
                await browser.close()
        return 0

    if args.extract_raw_agenda_blocks:
        start_date = parse_date(args.start_date) if args.start_date else None
        end_date = parse_date(args.end_date) if args.end_date else None
        meeting_rows = filter_agenda_metadata_rows(read_agenda_metadata_rows(), start_date, end_date, args.limit)
        ensure_dir(AGENDA_ITEMS_ROOT)
        if not RAW_AGENDA_ITEMS_CSV.exists():
            RAW_AGENDA_ITEMS_CSV.write_text(
                "source_body,meeting_id,meeting_date,meeting_type,raw_block_index,raw_text,source_url\n",
                encoding="utf-8",
            )
        if not meeting_rows:
            print("No agenda metadata rows matched the selected date range/limit.")
            return 0
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                wrote_raw = await extract_raw_agenda_blocks_from_metadata(page, meeting_rows)
                if wrote_raw == 0:
                    print("No raw agenda blocks were extracted for the selected meeting(s).")
                    print("Possible causes: no matching agenda rows, selector mismatch, or the agenda HTML layout changed.")
                    return 0
                wrote_structured = split_raw_agenda_blocks_to_structured()
                print(f"Extracted {wrote_raw} raw agenda block row(s)")
                print(f"Extracted {wrote_structured} structured agenda item row(s)")
                if wrote_structured == 0:
                    print("All raw blocks were rejected; see data/agenda-items/rejected_raw_blocks.csv")
            finally:
                await browser.close()
        return 0

    if args.split_raw_agenda_blocks:
        wrote = split_raw_agenda_blocks_to_structured()
        print(f"Extracted {wrote} structured agenda item row(s)")
        return 0

    if not args.start_date or not args.end_date:
        raise SystemExit("--start-date and --end-date are required unless --extract-agenda-items, --extract-raw-agenda-blocks, --split-raw-agenda-blocks, or --sync is used")

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if end_date < start_date:
        raise SystemExit("--end-date must be on or after --start-date")

    search_url = build_search_url(start_date, end_date)
    existing = read_existing_rows()
    existing_agenda_urls = read_existing_agenda_urls([DISCOVERY_CSV, *AGENDAS_ROOT.rglob("metadata.csv")])
    existing_discovery_keys = read_existing_discovery_keys(DISCOVERY_CSV)

    ensure_dir(AGENDAS_ROOT)
    ensure_dir(SUPPORT_ROOT)
    ensure_dir(AGENDA_ITEMS_ROOT)
    ensure_dir(DISCOVERY_CSV.parent)

    print(f"Agenda Online search URL: {search_url}")

    async_playwright = get_async_playwright()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        page = await browser.new_page()
        page.set_default_timeout(60000)
        try:
            await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
            await page.goto(search_url, wait_until="domcontentloaded")
            meetings = await extract_meetings(page, search_url)
            print(f"Detected {len(meetings)} meeting row(s)")

            if not meetings:
                print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                return 0

            processed = 0
            for meeting in meetings:
                if args.limit is not None and processed >= args.limit:
                    break
                processed += 1

                agenda_month_dir = month_dir_for_date(meeting.meeting_date, AGENDAS_ROOT)
                support_month_dir = month_dir_for_date(meeting.meeting_date, SUPPORT_ROOT)
                ensure_dir(agenda_month_dir)
                ensure_dir(support_month_dir)

                existing_row = existing.get(meeting.agenda_url)
                if args.download and existing_row and row_paths_present(existing_row):
                    continue

                time_part = f"{slugify(meeting.meeting_time)}_" if meeting.meeting_time else ""
                prefix = f"{meeting.meeting_date}_{time_part}{slugify(meeting.meeting_type)}_{meeting.meeting_id}"
                agenda_path = agenda_month_dir / f"{prefix}_agenda.pdf"
                supporting_paths: list[str] = []

                if not args.download:
                    print(f"{meeting.meeting_date} | {meeting.meeting_title} | {meeting.meeting_type}")
                    print(f"  agenda_url: {meeting.agenda_url}")
                    print(f"  summary_url: {meeting.summary_url or 'none'}")
                    print(f"  minutes_url: {meeting.minutes_url or 'none'}")
                    print(f"  video_url: {meeting.video_url or 'none'}")
                    print("  supporting_materials_url: none")

                if args.download:
                    if not agenda_path.exists():
                        agenda_path, _ = download_url(meeting.agenda_url, agenda_path)
                else:
                    supporting_paths = []

                row = {
                    "source_body": "Board of Supervisors",
                    "document_category": "agenda",
                    "record_id": meeting.meeting_id,
                    "record_date": meeting.meeting_date,
                    "record_time": meeting.meeting_time,
                    "record_title": meeting.meeting_title,
                    "meeting_type": meeting.meeting_type,
                    "source_page_url": SOURCE_PAGE,
                    "document_url": meeting.agenda_url,
                    "local_path": str(agenda_path.relative_to(ROOT)),
                    "download_status": "downloaded",
                    "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "source_search_url": search_url,
                    "notes": "",
                }

                if args.download and meeting.agenda_url not in existing:
                    write_download_row(row)
                    existing[meeting.agenda_url] = row
                elif not args.download:
                    write_discovery_rows(meeting, search_url, existing_discovery_keys)
        finally:
            await browser.close()

    return 0


if __name__ == "__main__":


    raise SystemExit(asyncio.run(main()))
