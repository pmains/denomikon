#!/usr/bin/env python3
"""
MAG (Maricopa Association of Governments) scraper.

MAG's site (azmag.gov) runs DotNetNuke behind Cloudflare. Simple HTTP requests
are blocked. This scraper uses the OpenClaw browser (Playwright/Brave) which
carries an authenticated Cloudflare session.

PDFs are served via LinkClick.aspx?fileticket=... which requires a Referer
header matching the event page. This scraper navigates to each event page,
extracts fresh fileticket URLs, and downloads via synchronous XMLHttpRequest
within the browser context (bypassing Cloudflare).

Usage:
  python -m scripts.scraper.mag --sync --cid 2 --year 2024
  python -m scripts.scraper.mag --sync --cid 2 --all-years
  python -m scripts.scraper.mag --list-committees
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from argparse import ArgumentParser
from typing import Optional

log = logging.getLogger(__name__)

COMMITTEES: dict[int, tuple[str, str, str]] = {
    2: ("Management Committee", "MC", "management-committee"),
}

PDF_TYPES = ["Agenda", "Minutes"]

BASE = "https://azmag.gov"


def cli(cmd: list[str], timeout: int = 30) -> str:
    full_cmd = ["openclaw", "browser"] + cmd
    r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"Browser CLI error: {r.stderr[:200]}")
    return r.stdout.strip()


def evaluate(js: str, timeout: int = 30) -> str:
    """Evaluate a JS arrow function in the browser's current tab."""
    js_one = " ".join(line.strip() for line in js.strip().split("\n"))
    js_one = re.sub(r" {2,}", " ", js_one)
    r = subprocess.run(
        ["openclaw", "browser", "evaluate", "--fn", js_one],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        err = r.stderr[:300] if r.stderr else r.stdout[:300]
        raise RuntimeError(f"Evaluate error: {err}")
    return r.stdout.strip()


def navigate(url: str) -> None:
    cli(["navigate", url], timeout=15)


def list_committees() -> None:
    for cid, (name, code, slug) in sorted(COMMITTEES.items()):
        print(f"  cid={cid}: {name} (code={code})")


# ── Step 1: Fetch event history from /EventHistory/{cid} ──

def fetch_event_history(cid: int) -> list[dict]:
    navigate(f"{BASE}/EventHistory/{cid}")
    time.sleep(2)

    js = """
    () => {
        var tables = document.querySelectorAll('table');
        var table = null;
        for (var t = 0; t < tables.length; t++) {
            if (tables[t].querySelector('th')) { table = tables[t]; break; }
        }
        if (!table) return [];
        var results = [];
        for (var i = 1; i < table.rows.length; i++) {
            var row = table.rows[i];
            if (row.cells.length < 3) continue;
            var link = row.cells[2].querySelector('a');
            if (!link) continue;
            var eventName = link.textContent.trim();
            var eventUrl = link.getAttribute('href') || '';
            var m = eventUrl.match(/Event.(\\d+)/);
            if (!m) continue;
            results.push({
                event_id: parseInt(m[1]),
                event_name: eventName,
                event_url: eventUrl,
                is_canceled: eventName.indexOf('Canceled') !== -1
            });
        }
        return results;
    }
    """
    raw = evaluate(js)
    return json.loads(raw)


# ── Step 2: Visit event page, extract LinkClick URLs ──

def fetch_event_page_doc_links() -> list[dict]:
    """Extract LinkClick.aspx URLs from the currently-loaded event page.

    Returns list of {title, url} where url is the full https LinkClick URL.
    """
    js = """
    () => {
        var links = document.querySelectorAll('a[href*=\"LinkClick.aspx\"]');
        var results = [];
        for (var i = 0; i < links.length; i++) {
            var a = links[i];
            results.push({
                title: a.textContent.trim(),
                url: a.href
            });
        }
        return results;
    }
    """
    raw = evaluate(js)
    return json.loads(raw)


# ── Step 3: Download PDF via synchronous XHR (avoids async issues) ──

def download_pdf_via_browser(url: str, referer: str) -> Optional[bytes]:
    """Download a PDF via synchronous XMLHttpRequest in the browser.

    Uses overrideMimeType for correct binary transfer.
    Must be called from a tab with an active Cloudflare session.
    """
    js = f"""
    () => {{
        var xhr = new XMLHttpRequest();
        xhr.open('GET', {json.dumps(url)}, false);
        xhr.overrideMimeType('text/plain; charset=x-user-defined');
        xhr.setRequestHeader('Referer', {json.dumps(referer)});
        try {{ xhr.send(null); }} catch(e) {{ return {{error: e.message}}; }}
        if (xhr.status !== 200) return {{error: 'HTTP ' + xhr.status}};
        var raw = xhr.responseText;
        if (raw.charCodeAt(0) !== 0x25 || raw.charCodeAt(1) !== 0x50)
            return {{error: 'not PDF', first_bytes: raw.charCodeAt(0).toString(16) + ' ' + raw.charCodeAt(1).toString(16)}};
        var binary = '';
        for (var i = 0; i < raw.length; i++)
            binary += String.fromCharCode(raw.charCodeAt(i) & 0xff);
        return {{data: btoa(binary), size: raw.length}};
    }}
    """
    raw = evaluate(js, timeout=30)
    result = json.loads(raw)
    if "data" in result:
        return base64.b64decode(result["data"])
    log.debug("download_pdf %s: %s", url.split("/")[-1][:40], result.get("error"))
    return None


def save_pdf(bytes_data: bytes, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes_data)


# ── Orchestration ──

def sync_committee(
    cid: int,
    year: Optional[int] = None,
    output_dir: str = "data/mag",
    skip_downloads: bool = False,
) -> int:
    if cid not in COMMITTEES:
        raise ValueError(f"Unknown committee cid={cid}")

    committee_name, committee_code, _ = COMMITTEES[cid]

    log.info("Fetching event history for %s (cid=%d)...", committee_name, cid)
    events = fetch_event_history(cid)
    log.info("Found %d events", len(events))

    if year:
        events = [e for e in events if str(year) in e["event_name"]]
        log.info("Filtered to %d events in %d", len(events), year)

    downloaded = 0
    for i, event in enumerate(events):
        if event["is_canceled"]:
            continue

        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", event["event_name"])
        if not m:
            continue

        mn, d, y = m.groups()
        date_str = f"{y}-{int(mn):02d}-{int(d):02d}"
        log.info("  [%d/%d] %s (%s)...", i+1, len(events), event["event_name"], date_str)

        if skip_downloads:
            continue

        try:
            # Navigate to event page and fetch document links
            navigate(f"{BASE}{event['event_url']}")
            time.sleep(1.5)
            doc_links = fetch_event_page_doc_links()
        except Exception as e:
            log.warning("    error loading event page: %s", e)
            continue

        if not doc_links:
            log.debug("    no documents on event page")
            continue

        event_referer = f"{BASE}{event['event_url']}"

        for doc_type in PDF_TYPES:
            matching = [d for d in doc_links if doc_type.lower() in d["title"].lower()]
            if not matching:
                continue

            pdf_url = matching[0]["url"]
            pdf_bytes = download_pdf_via_browser(pdf_url, event_referer)
            if pdf_bytes:
                out_path = os.path.join(
                    output_dir, committee_code, str(y),
                    f"{committee_code}-{date_str}-{doc_type}.pdf",
                )
                save_pdf(pdf_bytes, out_path)
                downloaded += 1
                log.info("    OK %s (%d bytes)", doc_type, len(pdf_bytes))
            else:
                log.debug("    -- %s: download failed", doc_type)

    return downloaded


def main() -> int:
    p = ArgumentParser(description="MAG committee document scraper")
    p.add_argument("--sync", action="store_true")
    p.add_argument("--cid", type=int, default=2)
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--all-years", action="store_true")
    p.add_argument("--skip-downloads", action="store_true")
    p.add_argument("--output-dir", default="data/mag")
    p.add_argument("--list-committees", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.list_committees:
        for cid, (name, code, _) in sorted(COMMITTEES.items()):
            print(f"  cid={cid}: {name} (code={code})")
        return 0

    if args.sync:
        total = sync_committee(
            cid=args.cid,
            year=args.year if not args.all_years else None,
            output_dir=args.output_dir,
            skip_downloads=args.skip_downloads,
        )
        log.info("Downloaded %d document(s)", total)
        return 0

    log.warning("No action specified. Use --sync or --list-committees")
    return 1


if __name__ == "__main__":
    sys.exit(main())
