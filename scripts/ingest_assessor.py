"""Ingest Maricopa County Assessor parcel data into pre-aggregated summaries.

Usage:
    POLISCOPIC_DB_TIER=development python scripts/ingest_assessor.py

Downloads and parses Apartment Master and Residential Master (pipe-delimited CSVs
from the Assessor's office), then creates compact pre-aggregated summary tables
for the housing dashboard.

Design principles (2GB RAM constraint):
- No raw parcel data in the main database — separate file or summary-only
- Pre-aggregate by (city, year, type) at ingest time
- Dashboard queries hit ~100-row tables, never raw parcels
"""
from __future__ import annotations

import csv, io
import gc
import gzip
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Config ──

WORKSPACE = Path(__file__).resolve().parent.parent
DATA_DIR = WORKSPACE / "data"
DB_PATH = DATA_DIR / "maricopa.sqlite"
PARQUET_DIR = DATA_DIR / "parcels"  # raw data lives here, not in SQLite

# Assessor ZIP files
APARTMENT_ZIP = WORKSPACE / "Apartment_Master.zip"
RESIDENTIAL_ZIP = WORKSPACE / "Residential_Master.zip"

# Jurisdiction mapping (city name in data → our slug)
CITY_TO_SLUG = {
    'PHOENIX': 'phoenix', 'MESA': 'mesa', 'TEMPE': 'tempe',
    'GLENDALE': 'glendale', 'SCOTTSDALE': 'scottsdale', 'CHANDLER': 'chandler',
    'GILBERT': 'gilbert', 'PEORIA': 'peoria', 'SURPRISE': 'surprise',
    'GOODYEAR': 'goodyear', 'AVONDALE': 'avondale', 'BUCKEYE': 'buckeye',
    'EL MIRAGE': 'el-mirage', 'TOLLESON': 'tolleson', 'WICKENBURG': 'wickenburg',
    'YOUNGTOWN': 'youngtown', 'FOUNTAIN HILLS': 'fountain-hills',
    'PARADISE VALLEY': 'paradise-valley', 'CAVE CREEK': 'cave-creek',
    'QUEEN CREEK': 'queen-creek', 'LITCHFIELD PARK': 'litchfield-park',
    'GUADALUPE': 'guadalupe',
}

# Columns for Apartment Master (pipe-delimited, no header)
# From the file spec: Econ Unit|Lead Parcel|Complex name|PUC|Land FCV|ImprFCV|...
APARTMENT_COLS = [
    'econ_unit', 'lead_parcel', 'complex_name', 'puc',
    'land_fcv', 'impr_fcv', 'utilities', 'fireplace',
    'one_bdrm', 'two_bdrm', 'three_bdrm', 'other_units', 'studio',
    'one_bdrm_rate', 'two_bdrm_rate', 'three_bdrm_rate', 'other_rate', 'studio_rate',
    'income_source',
    'ground_flr_perimeter', 'ground_flr_area', 'total_flr_area',
    'story_count', 'height', 'construction_yr',
    'situs_address', 'situs_unit',
    'situs_city', 'situs_zip',
    'owner_name', 'in_care_of',
    'address_1', 'address_2', 'city', 'state', 'zip',
    'sum_land_sz', 'land_size', 'num_units',
]

# Columns for Residential Master (pipe-delimited, no header, 39 cols)
# Mapped from data examination
RESIDENTIAL_COLS = [
    'apn', 'field2', 'class_code', 'field4', 'ac_type', 'field6',
    'rooms', 'exterior', 'roof', 'field10',
    'year_built', 'total_sqft', 'living_sqft',
    'field14', 'field15', 'field16',
    'parking', 'patios', 'field19', 'field20',
    'sale_date', 'field22', 'field23', 'field24',
    'owner_name',
    'situs_address', 'situs_unit',
    'situs_city', 'situs_state', 'situs_zip', 'field31',
    'street_num', 'street_dir', 'street_name', 'street_type',
    'field36', 'field37',
    'mail_city', 'mail_zip',
]


def normalize_city(city: str) -> Optional[str]:
    """Map assessor city name to jurisdiction slug."""
    city = city.strip().upper()
    if not city:
        return None
    direct = CITY_TO_SLUG.get(city)
    if direct:
        return direct
    # Fuzzy match
    for name, slug in CITY_TO_SLUG.items():
        if city in name or name in city:
            return slug
    return None


def safe_int(val: str) -> int:
    try:
        return int(float(val.strip() or '0'))
    except (ValueError, TypeError):
        return 0


def safe_float(val: str) -> float:
    try:
        return float(val.strip() or '0')
    except (ValueError, TypeError):
        return 0.0


def ingest_apartment_master() -> dict:
    """Parse Apartment Master, return pre-aggregated data."""
    import zipfile
    
    log.info("Reading Apartment Master...")
    t0 = time.time()
    
    # Aggregate: { (city_slug, year, unit_type): count }
    # and per-city stats
    city_year_units: dict[tuple[str, int], dict] = defaultdict(lambda: {
        'units': 0, 'complexes': 0,
        'total_sqft': 0, 'units_sqft': 0,
        'total_rent': 0, 'units_rent': 0,
        'total_land_value': 0,
        'total_impr_value': 0,
    })
    
    raw_count = 0
    with zipfile.ZipFile(APARTMENT_ZIP, 'r') as zf:
        with zf.open('Data/Apartment_Master.txt') as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding='latin-1'), delimiter='|')
            for row in reader:
                raw_count += 1
                if len(row) < 39:
                    continue
                    
                city = normalize_city(row[27])  # situs_city (0-indexed: col 28-1)
                if not city:
                    continue
                    
                year = safe_int(row[24])  # construction_yr (col 25-1)
                if year < 1900 or year > 2030:
                    continue
                    
                num_units = safe_int(row[38])  # num_units (col 39-1)
                if num_units == 0:
                    continue
                    
                sqft = safe_float(row[21])  # total_flr_area (col 22-1)
                rent = safe_float(row[13])  # one_bdrm_rate (col 14-1)
                land_val = safe_float(row[4])  # land_fcv
                impr_val = safe_float(row[5])  # impr_fcv
                
                summary = city_year_units[(city, year)]
                summary['units'] += num_units
                summary['complexes'] += 1
                summary['total_sqft'] += sqft
                summary['units_sqft'] += num_units if sqft else 0
                summary['total_rent'] += rent * num_units if rent else 0
                summary['units_rent'] += num_units if rent else 0
                summary['total_land_value'] += land_val
                summary['total_impr_value'] += impr_val
    
    log.info(f"Processed {raw_count} apartment records in {time.time()-t0:.1f}s")
    return city_year_units


def ingest_residential_master() -> dict:
    """Parse Residential Master, return pre-aggregated data."""
    import zipfile
    
    log.info("Reading Residential Master (this may take a minute)...")
    t0 = time.time()
    
    city_year_units: dict[tuple[str, int], dict] = defaultdict(lambda: {
        'units': 0, 'parcels': 0,
        'total_sqft': 0, 'units_sqft': 0,
    })
    
    raw_count = 0
    with zipfile.ZipFile(RESIDENTIAL_ZIP, 'r') as zf:
        with zf.open('Data/Residential_Master.txt') as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding='latin-1'), delimiter='|')
            for row in reader:
                raw_count += 1
                if len(row) < 39:
                    continue
                    
                city = normalize_city(row[27])  # situs_city (col 28-1)
                if not city:
                    continue
                    
                year = safe_int(row[10])  # year_built (col 11-1)
                if year < 1900 or year > 2030:
                    continue
                    
                sqft = safe_float(row[12])  # living_sqft (col 13-1)
                
                # Each residential parcel = 1 unit
                summary = city_year_units[(city, year)]
                summary['units'] += 1
                summary['parcels'] += 1
                summary['total_sqft'] += sqft
                summary['units_sqft'] += 1 if sqft else 0
                
                if raw_count % 200000 == 0:
                    log.info(f"  ... {raw_count} rows processed ({time.time()-t0:.0f}s)")
                    gc.collect()
    
    log.info(f"Processed {raw_count} residential records in {time.time()-t0:.1f}s")
    return city_year_units


def write_summaries(apartment_data: dict, residential_data: dict) -> int:
    """Write pre-aggregated summaries to the main database."""
    import sqlite3
    
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    
    # Create summary table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS housing_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jurisdiction_slug TEXT NOT NULL,
            year INTEGER NOT NULL,
            unit_type TEXT NOT NULL,  -- 'apartment', 'sf', 'condo'
            units INTEGER NOT NULL DEFAULT 0,
            parcels INTEGER NOT NULL DEFAULT 0,
            avg_rent REAL,
            avg_sqft REAL,
            avg_land_value REAL,
            avg_improvement_value REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_housing_units 
        ON housing_units(jurisdiction_slug, year, unit_type)
    """)
    
    # Clear old data
    cur.execute("DELETE FROM housing_units")
    
    row_count = 0
    
    # Write apartment summaries
    for (city, year), data in sorted(apartment_data.items()):
        avg_rent = (data['total_rent'] / data['units_rent']) if data['units_rent'] > 0 else None
        avg_sqft = (data['total_sqft'] / data['units_sqft']) if data['units_sqft'] > 0 else None
        avg_lv = data['total_land_value'] / data['units'] if data['units'] > 0 else 0
        avg_iv = data['total_impr_value'] / data['units'] if data['units'] > 0 else 0
        
        cur.execute("""
            INSERT INTO housing_units 
                (jurisdiction_slug, year, unit_type, units, parcels, avg_rent, avg_sqft, avg_land_value, avg_improvement_value)
            VALUES (?, ?, 'apartment', ?, ?, ?, ?, ?, ?)
        """, (city, year, data['units'], data['complexes'], avg_rent, avg_sqft, avg_lv, avg_iv))
        row_count += 1
    
    # Write residential summaries
    for (city, year), data in sorted(residential_data.items()):
        avg_sqft = (data['total_sqft'] / data['units_sqft']) if data['units_sqft'] > 0 else None
        
        cur.execute("""
            INSERT INTO housing_units 
                (jurisdiction_slug, year, unit_type, units, parcels, avg_sqft)
            VALUES (?, ?, 'sf', ?, ?, ?)
        """, (city, year, data['units'], data['parcels'], avg_sqft))
        row_count += 1
    
    conn.commit()
    conn.close()
    
    log.info(f"Wrote {row_count} summary rows to {DB_PATH}")
    return row_count


def main():
    
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    log.info("Assessor Data Ingestion")
    log.info("=" * 50)
    
    # Check files exist
    for f in [APARTMENT_ZIP, RESIDENTIAL_ZIP]:
        if not f.exists():
            log.error(f"Missing: {f}")
            return 1
    
    # Ingest
    apartment_data = ingest_apartment_master()
    gc.collect()
    residential_data = ingest_residential_master()
    
    # Summarize
    apt_total = sum(d['units'] for d in apartment_data.values())
    sf_total = sum(d['units'] for d in residential_data.values())
    log.info(f"\nTotals: {apt_total:,} apartment units in {len(apartment_data)} city-year groups")
    log.info(f"        {sf_total:,} residential parcels in {len(residential_data)} city-year groups")
    
    # Write to DB
    rows = write_summaries(apartment_data, residential_data)
    
    log.info(f"\nDone. {rows} summary rows in {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
