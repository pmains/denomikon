from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from scraper.common.utils import _extract_c_number, parse_c_number_parts
from scraper.common.io_utils import url_ext
from scraper.common.html_utils import _clean_html_text

def _extract_supporting_docs_from_table(table_html: str, agenda_item_dict: dict, base_url: str) -> list[dict]:
    """Extract supporting document links from an agenda item's table HTML.

    Looks for anchor tags pointing to external documents (PDF, DOC,
    URLs containing /Document/, /File/, etc.).
    """
    docs: list[dict] = []
    seen_urls: set[str] = set()

    doc_pattern = re.compile(
        r'href="(?!\#)([^"]*(?:Document|File|Attachment|download|\\.pdf|\\.doc)"[^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL | re.I,
    )
    for m in doc_pattern.finditer(table_html):
        url = m.group(1).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
        title = _clean_html_text(title)
        abs_url = urllib.parse.urljoin(base_url, url) if not url.startswith("http") else url

        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)

        parsed = urllib.parse.urlparse(abs_url)
        path = Path(parsed.path) if parsed.path else Path(title)
        file_name = path.name or None
        ext = path.suffix.lstrip(".") or None

        docs.append({
            "agenda_item_id": "0",
            "meeting_id": agenda_item_dict.get("meeting_id", ""),
            "agenda_item_number": int(agenda_item_dict.get("agenda_item_number", 0)),
            "c_number": agenda_item_dict.get("c_number", "") or None,
            "c_number_base": agenda_item_dict.get("c_number_base", "") or None,
            "c_number_revision": agenda_item_dict.get("c_number_revision"),
            "document_title": title or file_name or "",
            "document_url": abs_url,
            "document_type": ext.upper() if ext else None,
            "file_name": file_name,
            "file_extension": ext,
        })

    return docs


def extract_supporting_documents_from_items(
    html: str,
    agenda_items: list[dict],
    source_url: str,
) -> list[dict]:
    """Extract supporting documents from the full agenda HTML.

    Finds each item's table, then searches for document links within it.
    Returns a flat list of supporting document dicts.
    """
    all_docs: list[dict] = []
    seen_urls: set[str] = set()

    for item_dict in agenda_items:
        item_num = int(item_dict.get("agenda_item_number", 0))
        if not item_num:
            continue

        # Find the item's bold span in the HTML
        bold_pattern = re.compile(
            r'<span[^>]*font-weight:bold[^>]*>'
            + re.escape(str(item_num))
            + r'\.</span>'
        )
        m = bold_pattern.search(html)
        if not m:
            continue

        pos = m.start()
        tstart = html.rfind("<table", 0, pos)
        tend = html.find("</table>", pos)
        if tstart < 0 or tend < 0:
            continue
        table_html = html[tstart : tend + 8]

        docs = _extract_supporting_docs_from_table(table_html, item_dict, source_url)
        for doc in docs:
            url = doc["document_url"]
            if url not in seen_urls:
                seen_urls.add(url)
                all_docs.append(doc)

    return all_docs


async def extract_supporting_documents_dynamic(
    page,
    agenda_items: list[dict],
    base_url: str,
) -> list[dict]:
    """Extract supporting documents by clicking each agenda item link.

    On Agenda Online, supporting documents are revealed by clicking each
    agenda item link, which populates a #itemView div via AJAX. The
    interactive links are those where the page's JavaScript has bound a
    click handler that calls loadAgendaItem(). These links have href="#"
    and live inside #agendaView.

    For each interactive link:
    1. Click the link
    2. Wait for #itemView to update
    3. Extract the C-number from .item-view-title-text
    4. Extract supporting document links from lnkAttachment_* anchors
    5. Look up the corresponding agenda item by C-number
    6. Build supporting document dicts with meeting_id and agenda_item_number

    Returns a flat list of supporting document dicts ready for persist_meeting().
    """
    all_docs: list[dict] = []
    seen_urls: set[str] = set()

    # Build a lookup: C-number → agenda_item_dict
    # Also build a text-based fallback lookup
    items_by_c_number: dict[str, dict] = {}
    items_by_text: dict[str, dict] = {}
    items_ordered: list[dict] = list(agenda_items)
    for item_dict in agenda_items:
        c_num = (item_dict.get("c_number") or "").strip()
        if c_num:
            items_by_c_number[c_num] = item_dict
        title = (item_dict.get("agenda_item_title") or "").strip().lower()
        if title:
            items_by_text[title] = item_dict

    # Find interactive links (href="#") in #agendaView
    interactive_links = await page.evaluate(
        """() => {
            const container = document.getElementById('agendaView');
            if (!container) return [];
            const links = container.querySelectorAll('a[href="#"]');
            return Array.from(links).map(l => ({
                id: l.id,
                text: (l.textContent || '').trim()
            }));
        }"""
    )

    if not interactive_links:
        return all_docs

    # Local reference to avoid repeated re-import
    join = urllib.parse.urljoin

    for link_info in interactive_links:
        link_id = link_info["id"]
        link_text = link_info["text"]

        try:
            # Click and extract in one evaluate call with timeout
            result = await asyncio.wait_for(
                _click_and_extract_item(page, link_id),
                timeout=12,
            )

            if result is None:
                continue

            c_number = result.get("c_number", "")
            attachments = result.get("attachments", [])

            if not attachments:
                continue

            # Look up the agenda item by C-number or link text
            item_dict = None
            if c_number and c_number in items_by_c_number:
                item_dict = items_by_c_number[c_number]
            elif link_text.lower() in items_by_text:
                item_dict = items_by_text[link_text.lower()]

            meeting_id = (item_dict or {}).get("meeting_id", "")
            base_item_num = int((item_dict or {}).get("agenda_item_number", 0))
            c_number_parts = parse_c_number_parts(c_number) if c_number else {}

            for att in attachments:
                url = att.get("href", "")
                if not url or url in seen_urls:
                    continue
                abs_url = join(base_url, url) if not url.startswith("http") else url
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)

                title = att.get("text", "")
                parsed = urllib.parse.urlparse(abs_url)
                path = Path(parsed.path) if parsed.path else Path(title)
                file_name = path.name or None
                ext = path.suffix.lstrip(".") or None
                ext = ext or url_ext(abs_url).lstrip(".") or None

                doc = {
                    "agenda_item_id": base_item_num,
                    "meeting_id": meeting_id,
                    "agenda_item_number": base_item_num,
                    "c_number": c_number if c_number else None,
                    "c_number_base": c_number_parts.get("c_number_base", "") or None,
                    "c_number_revision": c_number_parts.get("c_number_revision"),
                    "document_title": title or file_name or "",
                    "document_url": abs_url,
                    "document_type": ext.upper() if ext else None,
                    "file_name": file_name,
                    "file_extension": ext,
                }
                all_docs.append(doc)

        except asyncio.TimeoutError:
            continue
        except Exception:
            continue

    return all_docs


async def _click_and_extract_item(page, link_id: str) -> dict | None:
    """Click an interactive agenda item link and extract item view data.

    Waits for #itemView content to change after the click (tracked via a
    page-level `__ocLastItemViewInnerLength` counter to avoid race conditions
    with stale AJAX data from previous clicks).
    Queries attachment anchors scoped to #itemView only.

    Returns a dict with 'c_number' and 'attachments' keys, or None if
    the click failed or timed out.
    """
    # Click the link
    clicked = await page.evaluate(
        f"""(id) => {{
            const el = document.getElementById(id);
            if (!el) return false;
            el.click();
            return true;
        }}""",
        link_id,
    )
    if not clicked:
        return None

    # Wait for #itemView content to CHANGE (not just exist — it's already
    # populated from a previous click, so children.length > 0 would race)
    try:
        await page.wait_for_function(
            """() => {
                const iv = document.getElementById('itemView');
                if (!iv || !iv.children.length) return false;
                const prevLen = window.__ocLastItemViewInnerLength || 0;
                const currLen = iv.innerHTML.length;
                if (currLen !== prevLen) {
                    window.__ocLastItemViewInnerLength = currLen;
                    return true;
                }
                return false;
            }""",
            timeout=10000,
        )
    except Exception:
        pass

    # Small settle time
    await page.wait_for_timeout(300)

    # Extract C-number and attachments scoped to #itemView
    result = await page.evaluate(
        """() => {
            const iv = document.getElementById('itemView');
            if (!iv) return { c_number: '', attachments: [] };
            const cnum = iv.querySelector('.item-view-title-text');
            const c_number = cnum ? cnum.textContent.trim() : '';
            const anchors = iv.querySelectorAll('a[id^="lnkAttachment_"]');
            const attachments = Array.from(anchors).map(a => ({
                href: a.getAttribute('href') || '',
                text: (a.textContent || '').trim()
            }));
            return { c_number, attachments };
        }"""
    )

    return result


async def extract_supporting_documents_dynamic_concurrent(
    page,
    agenda_items: list[dict],
    base_url: str,
    concurrency: int = 5,
) -> list[dict]:
    """Extract supporting documents by clicking agenda item links concurrently.

    Same logic as extract_supporting_documents_dynamic(), but distributes the
    interactive links across *concurrency* browser pages (within the same
    browser instance) so that AJAX round-trips for different items overlap.

    For a meeting with 80+ items this cuts the discovery phase from ~130 s
    down to ~30-40 s (limited by the server's own concurrency handling).
    """
    import math
    import urllib.parse
    from pathlib import Path

    # 1. Build C-number / text lookups (same as the sequential version)
    items_by_c_number: dict[str, dict] = {}
    items_by_text: dict[str, dict] = {}
    for item_dict in agenda_items:
        c_num = (item_dict.get("c_number") or "").strip()
        if c_num:
            items_by_c_number[c_num] = item_dict
        title = (item_dict.get("agenda_item_title") or "").strip().lower()
        if title:
            items_by_text[title] = item_dict

    # 2. Get all interactive link IDs from the primary page
    interactive_links = await page.evaluate(
        """() => {
            const container = document.getElementById('agendaView');
            if (!container) return [];
            const links = container.querySelectorAll('a[href="#"]');
            return Array.from(links).map(l => ({
                id: l.id,
                text: (l.textContent || '').trim()
            }));
        }"""
    )
    if not interactive_links:
        return []

    # Filter out section-header links (no attachment possible) by checking
    # if any of the agenda items' titles appear in the link text
    item_title_set = {
        (d.get("agenda_item_title") or "").strip().lower()
        for d in agenda_items
    }
    candidate_links = [
        li for li in interactive_links
        if li["text"].lower() in item_title_set
    ]
    if not candidate_links:
        # Fallback: use all interactive links
        candidate_links = interactive_links

    # 3. Build per-item docs helper (shared by all workers)
    all_docs: list[dict] = []
    seen_urls: set[str] = set()

    def _build_docs(result: dict, link_text: str) -> list[dict]:
        local: list[dict] = []
        c_number = result.get("c_number", "")
        attachments = result.get("attachments", [])
        if not attachments:
            return local

        item_dict = None
        if c_number and c_number in items_by_c_number:
            item_dict = items_by_c_number[c_number]
        elif link_text.lower() in items_by_text:
            item_dict = items_by_text[link_text.lower()]

        meeting_id = (item_dict or {}).get("meeting_id", "")
        base_item_num = int((item_dict or {}).get("agenda_item_number", 0))
        parts = parse_c_number_parts(c_number) if c_number else {}
        for att in attachments:
            url = att.get("href", "")
            if not url:
                continue
            abs_url = urllib.parse.urljoin(base_url, url) if not url.startswith("http") else url
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)

            title = att.get("text", "")
            parsed = urllib.parse.urlparse(abs_url)
            path = Path(parsed.path) if parsed.path else Path(title)
            file_name = path.name or None
            ext = path.suffix.lstrip(".") or None

            doc = {
                "agenda_item_id": base_item_num,
                "meeting_id": meeting_id,
                "agenda_item_number": base_item_num,
                "c_number": c_number if c_number else None,
                "c_number_base": parts.get("c_number_base", "") or None,
                "c_number_revision": parts.get("c_number_revision"),
                "document_title": title or file_name or "",
                "document_url": abs_url,
                "document_type": ext.upper() if ext else None,
                "file_name": file_name,
                "file_extension": ext,
            }
            local.append(doc)
        return local

    # 4. Distribute links across worker pages
    chunk_size = math.ceil(len(candidate_links) / concurrency)
    chunks = [candidate_links[i:i + chunk_size] for i in range(0, len(candidate_links), chunk_size)]

    async def _worker(chunk: list[dict]) -> list[dict]:
        if not chunk:
            return []
        browser = page.context.browser
        worker_page = await browser.new_page()
        worker_page.set_default_timeout(60000)
        try:
            # Load the same meeting page — use load and then wait for the
            # agendaView to populate via AJAX.  networkidle is unreliable
            # here because the server may send background keepalive
            # connections that prevent it from ever settling.
            await worker_page.goto(base_url, wait_until="load")
            try:
                await worker_page.wait_for_function(
                    """() => {
                        const av = document.getElementById('agendaView');
                        if (!av) return false;
                        // Check for actual numbered item markers rather than
                        // placeholder/loading text.
                        const text = av.textContent || '';
                        if (text.length < 100) return false;
                        const spans = av.querySelectorAll('span[style*="bold"]');
                        for (const s of spans) {
                            if (/^\\d+\\.$/.test((s.textContent || '').trim()))
                                return true;
                        }
                        return av.querySelector('a[id^="lnkAgendaItem"]') !== null;
                    }""",
                    timeout=20000,
                )
            except Exception:
                pass

            worker_docs: list[dict] = []
            for link_info in chunk:
                link_id = link_info["id"]
                link_text = link_info["text"]
                try:
                    result = await asyncio.wait_for(
                        _click_and_extract_item(worker_page, link_id),
                        timeout=12,
                    )
                    if result:
                        docs = _build_docs(result, link_text)
                        worker_docs.extend(docs)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    continue
            return worker_docs
        finally:
            await worker_page.close()

    # 5. Run workers concurrently
    worker_results = await asyncio.gather(*[_worker(chunk) for chunk in chunks])

    # 6. Merge results
    for wr in worker_results:
        all_docs.extend(wr)

    return all_docs

