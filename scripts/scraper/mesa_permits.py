"""Scrape Mesa building permits from the Socrata SODA API.

Source: https://data.mesaaz.gov/Development-Services/Building-Permits-Filtered-View/a2ui-hcuj
API Endpoint: https://data.mesaaz.gov/resource/a2ui-hcuj.json
"""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)

API_BASE = "https://data.mesaaz.gov/resource/a2ui-hcuj.json"
USER_AGENT = "Poliscopic/1.0 (housing tracker)"

STAGE_MAP = [
    ("Certificate of Occupancy Issued", "complete"),
    ("Approved", "approved"),
    ("Issued", "issued"),
    ("In Review", "plan_review"),
]


def classify_stage(status: str) -> str:
    """Map Mesa's long status strings to our pipeline stage names."""
    for key, stage in STAGE_MAP:
        if key in status:
            return stage
    return "other"


def _req(query: str) -> list[dict]:
    """Make a SODA API request with retries."""
    url = f"{API_BASE}?{query}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read())
        except urllib.request.HTTPError as e:
            body = e.read().decode()[:200] if e.fp else ""
            if attempt < 2:
                log.warning("HTTP %d on attempt %d, retrying... %s", e.code, attempt + 1, body)
                time.sleep(2 ** attempt)
            else:
                log.error("HTTP %d after 3 attempts: %s", e.code, body)
                return []
        except Exception as e:
            if attempt < 2:
                log.warning("Error on attempt %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
            else:
                log.error("Error after 3 attempts: %s", e)
                return []
    return []


def fetch_by_year(permit_type: str = "RES") -> list[dict]:
    """Fetch all permits grouped by (year, status) for a given permit type.

    Returns list of {yr, status_category, cnt}
    """
    # The SODA API may not support EXTRACT() reliably on this dataset.
    # We iterate by year using $where, which is simpler and more reliable.
    results = []
    years = list(range(2017, 2027))  # data starts 2017, partial 2026

    for year in years:
        q = (
            "$select=status_category,COUNT(*)+as+cnt"
            f"&$where=permit_type='{permit_type}'"
            f"+AND+issued_date>='{year}-01-01'"
            f"+AND+issued_date<'{year + 1}-01-01'"
            "&$group=status_category"
            "&$order=cnt+DESC"
        )
        data = _req(q)
        if not data:
            continue
        for row in data:
            results.append({
                "yr": str(year),
                "status_category": row.get("status_category", ""),
                "cnt": int(row.get("cnt", 0)),
            })
        log.info("  %d %s: %d status groups", year, permit_type, len(data))

    return results


def fetch_all_status(permit_type: str = "RES") -> list[dict]:
    """Get all status groups without year breakdown (for permits with no issued_date)."""
    q = (
        "$select=status_category,COUNT(*)+as+cnt"
        f"&$where=permit_type='{permit_type}'"
        "+AND+issued_date+IS+NULL"
        "&$group=status_category"
        "&$order=cnt+DESC"
    )
    data = _req(q)
    results = []
    for row in data:
        results.append({
            "yr": "unknown",
            "status_category": row.get("status_category", ""),
            "cnt": int(row.get("cnt", 0)),
        })
    return results


def persist_aggregates(rows: list[dict]) -> int:
    """Write aggregated Mesa permit data to permit_pipeline table."""
    import sqlite3

    conn = sqlite3.connect("data/maricopa.sqlite")
    cur = conn.cursor()

    # Clear existing Mesa data
    cur.execute("DELETE FROM permit_pipeline WHERE jurisdiction_slug = 'mesa'")

    inserted = 0
    for row in rows:
        yr = row.get("yr", "0")
        if yr == "unknown":
            yr = 0
        else:
            yr = int(yr)

        stage = classify_stage(row.get("status_category", ""))
        cnt = row.get("cnt", 0)

        if cnt == 0:
            continue

        # Check if row exists
        existing = cur.execute(
            "SELECT permits FROM permit_pipeline WHERE jurisdiction_slug='mesa' AND year=? AND stage=?",
            (yr, stage)
        ).fetchone()

        if existing:
            cur.execute(
                "UPDATE permit_pipeline SET permits = permits + ? WHERE jurisdiction_slug='mesa' AND year=? AND stage=?",
                (cnt, yr, stage)
            )
        else:
            cur.execute(
                "INSERT INTO permit_pipeline (jurisdiction_slug, year, stage, permits) VALUES ('mesa', ?, ?, ?)",
                (yr, stage, cnt)
            )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.info("Mesa Building Permit Scraper")
    log.info("=" * 40)

    # Fetch residential permits by year + status
    log.info("Fetching residential permits by year...")
    res_data = fetch_by_year("RES")
    log.info("  %d year-status groups", len(res_data))

    # Fetch residential permits with no issued_date (in review, etc.)
    log.info("Fetching residential permits without issue date...")
    res_null = fetch_all_status("RES")
    log.info("  %d status groups (null date)", len(res_null))

    # Combine and persist
    all_rows = res_data + res_null
    inserted = persist_aggregates(all_rows)

    # Show results
    log.info("\nInserted %d rows into permit_pipeline", inserted)
    log.info("\nMesa permit pipeline summary:")

    import sqlite3
    conn = sqlite3.connect("data/maricopa.sqlite")
    cur = conn.cursor()
    cur.execute("""
        SELECT stage, SUM(permits) as cnt
        FROM permit_pipeline
        WHERE jurisdiction_slug = 'mesa'
        GROUP BY stage
        ORDER BY stage
    """)
    for r in cur.fetchall():
        log.info("  %s: %s permits", r[0], r[1])
    conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
