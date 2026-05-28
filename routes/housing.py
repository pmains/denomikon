"""Housing construction dashboard — built from Maricopa County Assessor parcel data.

Tracks completed housing units by jurisdiction and year, sourced from the
Apartment Master and Residential Master datasets.

Pre-aggregated at ingest time — no raw parcel scans at query time.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from flask import Blueprint, render_template, jsonify, request, current_app
from sqlalchemy import text

from db import get_session

housing_bp = Blueprint("housing", __name__, url_prefix="/housing")

# Approximate city centers for Leaflet map
CITY_LOCATIONS = {
    'avondale': (33.4337, -112.3496),
    'buckeye': (33.3703, -112.5838),
    'cave-creek': (33.8320, -111.9530),
    'chandler': (33.3062, -111.8413),
    'el-mirage': (33.6131, -112.3246),
    'fountain-hills': (33.6108, -111.7173),
    'gilbert': (33.3528, -111.7890),
    'glendale': (33.5387, -112.1860),
    'goodyear': (33.4353, -112.3582),
    'guadalupe': (33.3700, -111.9630),
    'litchfield-park': (33.4933, -112.3580),
    'mesa': (33.4152, -111.8315),
    'paradise-valley': (33.5312, -111.9426),
    'peoria': (33.5806, -112.2374),
    'phoenix': (33.4484, -112.0740),
    'queen-creek': (33.2488, -111.6346),
    'scottsdale': (33.4942, -111.9261),
    'surprise': (33.6292, -112.3279),
    'tempe': (33.4255, -111.9400),
    'tolleson': (33.4500, -112.2593),
    'wickenburg': (33.9686, -112.7288),
    'youngtown': (33.5942, -112.3031),
}

DISPLAY_NAMES = {
    'avondale': 'Avondale', 'buckeye': 'Buckeye', 'cave-creek': 'Cave Creek',
    'chandler': 'Chandler', 'el-mirage': 'El Mirage', 'fountain-hills': 'Fountain Hills',
    'gilbert': 'Gilbert', 'glendale': 'Glendale', 'goodyear': 'Goodyear',
    'guadalupe': 'Guadalupe', 'litchfield-park': 'Litchfield Park',
    'mesa': 'Mesa', 'paradise-valley': 'Paradise Valley', 'peoria': 'Peoria',
    'phoenix': 'Phoenix', 'queen-creek': 'Queen Creek', 'scottsdale': 'Scottsdale',
    'surprise': 'Surprise', 'tempe': 'Tempe', 'tolleson': 'Tolleson',
    'wickenburg': 'Wickenburg', 'youngtown': 'Youngtown',
}

# Population estimates for per-capita calculations (2025 est.)
POPULATIONS = {
    'avondale': 89000, 'buckeye': 120000, 'chandler': 285000, 'el-mirage': 37000,
    'gilbert': 285000, 'glendale': 250000, 'goodyear': 110000, 'mesa': 520000,
    'peoria': 200000, 'phoenix': 1650000, 'scottsdale': 245000, 'surprise': 155000,
    'tempe': 190000, 'tolleson': 7500, 'queen-creek': 80000, 'cave-creek': 6500,
    'fountain-hills': 26000, 'litchfield-park': 6000, 'paradise-valley': 13000,
    'guadalupe': 6000, 'wickenburg': 8000, 'youngtown': 7000,
    'maricopa-county': 4700000,
}


@housing_bp.route("")
def housing_index():
    """Main housing dashboard page."""
    return render_template(
        "housing.html",
        city_json=json.dumps({k: {"lat": v[0], "lng": v[1], "name": DISPLAY_NAMES.get(k, k)} 
                              for k, v in CITY_LOCATIONS.items()}),
    )


@housing_bp.route("/api/data")
@housing_bp.route("/api/data/<int:start_year>")
@housing_bp.route("/api/data/<int:start_year>/<int:end_year>")
def housing_data(start_year=2000, end_year=2026):
    """JSON endpoint returning pre-aggregated housing data."""
    # Get query params from request
    if request.args.get("start"):
        start_year = int(request.args["start"])
    if request.args.get("end"):
        end_year = int(request.args["end"])
    city_filter = request.args.get("city", "").strip().lower()
    
    session = get_session()
    
    where = 'year >= :start AND year <= :end'
    params = {"start": start_year, "end": end_year}
    
    if city_filter:
        where += ' AND jurisdiction_slug = :city'
        params["city"] = city_filter
    
    rows = session.execute(text(f"""
        SELECT jurisdiction_slug, year, unit_type, units, parcels,
               avg_rent, avg_sqft, avg_land_value, avg_improvement_value
        FROM housing_units
        WHERE {where}
        ORDER BY jurisdiction_slug, year, unit_type
    """), params).all()
    
    session.close()
    
    result = []
    for r in rows:
        result.append({
            "city": r.jurisdiction_slug,
            "city_name": DISPLAY_NAMES.get(r.jurisdiction_slug, r.jurisdiction_slug),
            "year": r.year,
            "type": r.unit_type,
            "units": r.units,
            "parcels": r.parcels,
            "avg_rent": r.avg_rent,
            "avg_sqft": round(r.avg_sqft, 1) if r.avg_sqft else None,
            "population": POPULATIONS.get(r.jurisdiction_slug, 0),
        })
    
    return jsonify(result)


@housing_bp.route("/api/summary")
def housing_summary():
    """Yearly totals across all jurisdictions, with per-capita."""
    session = get_session()
    
    rows = session.execute(text("""
        SELECT year, unit_type, SUM(units) as total
        FROM housing_units
        WHERE year >= 2000
        GROUP BY year, unit_type
        ORDER BY year, unit_type
    """)).all()
    
    session.close()
    
    # Aggregate by year
    years = {}
    for r in rows:
        if r.year not in years:
            years[r.year] = {"year": r.year, "apartment": 0, "sf": 0, "total": 0}
        years[r.year][r.unit_type] = r.total
        years[r.year]["total"] += r.total
    
    # Add per-capita
    county_pop = POPULATIONS.get('maricopa-county', 4700000)
    for y in years.values():
        y["per_capita"] = round(y["total"] / county_pop * 1000, 2) if county_pop else 0
    
    return jsonify(sorted(years.values(), key=lambda x: x["year"]))


@housing_bp.route("/api/cities")
def housing_cities():
    """Per-city totals for the latest year or a selected year."""
    year = request.args.get("year", "2024")
    session = get_session()
    
    rows = session.execute(text("""
        SELECT jurisdiction_slug,
               SUM(CASE WHEN unit_type='apartment' THEN units ELSE 0 END) as apt_units,
               SUM(CASE WHEN unit_type='sf' THEN units ELSE 0 END) as sf_units,
               SUM(units) as total_units
        FROM housing_units
        WHERE year = :year
        GROUP BY jurisdiction_slug
        ORDER BY total_units DESC
    """), {"year": year}).all()
    
    session.close()
    
    result = []
    for r in rows:
        pop = POPULATIONS.get(r.jurisdiction_slug, 1)
        result.append({
            "city": r.jurisdiction_slug,
            "city_name": DISPLAY_NAMES.get(r.jurisdiction_slug, r.jurisdiction_slug),
            "apartment_units": r.apt_units,
            "sf_units": r.sf_units,
            "total_units": r.total_units,
            "per_capita": round(r.total_units / pop * 1000, 2) if pop else 0,
            "lat": CITY_LOCATIONS.get(r.jurisdiction_slug, (33.4, -112.0))[0],
            "lng": CITY_LOCATIONS.get(r.jurisdiction_slug, (33.4, -112.0))[1],
        })
    
    return jsonify(result)
