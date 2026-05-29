"""Scrape Scottsdale building permits from the ArcGIS Active_CDS_Cases MapServer.

Source: https://maps.scottsdaleaz.gov/arcgis/rest/services/Active_CDS_Cases/MapServer

Layers:
  1 - Active Building Permits (DR-PP Cases) → "issued" stage
  4 - Completed Cases → "complete" stage
  5 - Active Plan Checks → "plan_review" stage
  7 - Approved Plan Checks → "approved" stage

Key note: This is a current-pipeline snapshot, not full historical archive.
Completed Cases goes back to ~2010 but only ~773 housing records.
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.request
from collections import Counter
from datetime import datetime

log = logging.getLogger(__name__)

BASE = "https://maps.scottsdaleaz.gov/arcgis/rest/services/Active_CDS_Cases/MapServer"
USER_AGENT = "Poliscopic/1.0"

# Housing type keywords to filter for
HOUSING_KEYWORDS = ["SFR", "MULTI", "APARTMENT", "DWELLING", "RESIDENTIAL", "DUPLEX", "TOWNHOME"]

# Layer config: (layer_id, stage_name, date_field)
LAYERS = [
    (1, "issued", "issuance_date"),
    (4, "complete", "issuance_date"),
    (5, "plan_review", None),
    (7, "approved", None),
]


def build_where() -> str:
    """Build WHERE clause filtering for housing types."""
    clauses = [f"UPPER(type_desc)+LIKE+UPPER('%25{kw}%25')" for kw in HOUSING_KEYWORDS]
    return "+OR+".join(clauses)


def fetch_layer(lid: int, date_field: str = None) -> list[dict]:
    """Fetch all features from a layer, filtering for housing types."""
    where = build_where()
    url = f"{BASE}/{lid}/query?where={where}&outFields=*&returnGeometry=false&f=json&resultRecordCount=2000"
    
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        log.error("Layer %d: %s", lid, e)
        return []
    
    features = data.get('features', [])
    results = []
    
    for feat in features:
        attrs = feat.get('attributes', {})
        type_desc = attrs.get('type_desc', '') or ''
        
        # Extract year from date field
        year = 0
        if date_field:
            ts = attrs.get(date_field)
            if ts and ts > 0:
                try:
                    year = datetime.fromtimestamp(ts / 1000).year
                except (ValueError, OSError):
                    year = 0
        
        results.append({
            "type_desc": type_desc,
            "year": year,
        })
    
    log.info("  Layer %d: %d housing records", lid, len(results))
    return results


def aggregate(records_by_layer: dict[int, list[dict]]) -> list[dict]:
    """Group by year and count, produce (year, stage, count) tuples."""
    from collections import defaultdict
    
    # Layer mapping
    stage_names = {1: "issued", 4: "complete", 5: "plan_review", 7: "approved"}
    
    agg = defaultdict(lambda: defaultdict(int))
    for lid, records in records_by_layer.items():
        stage = stage_names[lid]
        for r in records:
            yr = r["year"]
            agg[stage][yr] += 1
    
    results = []
    for stage, year_counts in agg.items():
        for yr, cnt in sorted(year_counts.items()):
            results.append({"year": yr, "stage": stage, "count": cnt})
    return results


def persist(results: list[dict]) -> int:
    """Write aggregated Scottsdale data to permit_pipeline."""
    import sqlite3
    
    conn = sqlite3.connect("data/maricopa.sqlite")
    cur = conn.cursor()
    
    # Clear existing Scottsdale data
    cur.execute("DELETE FROM permit_pipeline WHERE jurisdiction_slug = 'scottsdale'")
    
    inserted = 0
    for r in results:
        cur.execute(
            "INSERT INTO permit_pipeline (jurisdiction_slug, year, stage, permits) VALUES ('scottsdale', ?, ?, ?)",
            (r["year"], r["stage"], r["count"])
        )
        inserted += 1
    
    conn.commit()
    conn.close()
    return inserted


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.info("Scottsdale Building Permit Scraper")
    log.info("=" * 40)
    
    # Fetch each layer
    records = {}
    for lid, stage, date_field in LAYERS:
        log.info("Fetching layer %d (%s)...", lid, stage)
        records[lid] = fetch_layer(lid, date_field)
    
    # Aggregate
    results = aggregate(records)
    
    # Persist
    inserted = persist(results)
    
    log.info(f"\nWrote {inserted} rows to permit_pipeline")
    
    # Summary
    import sqlite3
    conn = sqlite3.connect("data/maricopa.sqlite")
    cur = conn.cursor()
    cur.execute("""
        SELECT stage, SUM(permits) as cnt
        FROM permit_pipeline WHERE jurisdiction_slug = 'scottsdale'
        GROUP BY stage ORDER BY stage
    """)
    for r in cur.fetchall():
        log.info("  %s: %s permits", r[0], r[1])
    conn.close()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
