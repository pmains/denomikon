"""Housing construction dashboard — built from Maricopa County Assessor parcel data."""
from __future__ import annotations

import json
from flask import Blueprint, render_template, request
from sqlalchemy import text
from db import get_session

housing_bp = Blueprint("housing", __name__, url_prefix="/housing")

CITY_DISPLAY = {
    'avondale': 'Avondale', 'buckeye': 'Buckeye', 'cave-creek': 'Cave Creek',
    'chandler': 'Chandler', 'el-mirage': 'El Mirage', 'fountain-hills': 'Fountain Hills',
    'gilbert': 'Gilbert', 'glendale': 'Glendale', 'goodyear': 'Goodyear',
    'guadalupe': 'Guadalupe', 'litchfield-park': 'Litchfield Park',
    'mesa': 'Mesa', 'paradise-valley': 'Paradise Valley', 'peoria': 'Peoria',
    'phoenix': 'Phoenix', 'queen-creek': 'Queen Creek', 'scottsdale': 'Scottsdale',
    'surprise': 'Surprise', 'tempe': 'Tempe', 'tolleson': 'Tolleson',
    'wickenburg': 'Wickenburg', 'youngtown': 'Youngtown',
}

CITY_LOCS = {
    'avondale': (33.4337, -112.3496), 'buckeye': (33.3703, -112.5838),
    'cave-creek': (33.8320, -111.9530), 'chandler': (33.3062, -111.8413),
    'el-mirage': (33.6131, -112.3246), 'fountain-hills': (33.6108, -111.7173),
    'gilbert': (33.3528, -111.7890), 'glendale': (33.5387, -112.1860),
    'goodyear': (33.4353, -112.3582), 'guadalupe': (33.3700, -111.9630),
    'litchfield-park': (33.4933, -112.3580), 'mesa': (33.4152, -111.8315),
    'paradise-valley': (33.5312, -111.9426), 'peoria': (33.5806, -112.2374),
    'phoenix': (33.4484, -112.0740), 'queen-creek': (33.2488, -111.6346),
    'scottsdale': (33.4942, -111.9261), 'surprise': (33.6292, -112.3279),
    'tempe': (33.4255, -111.9400), 'tolleson': (33.4500, -112.2593),
    'wickenburg': (33.9686, -112.7288), 'youngtown': (33.5942, -112.3031),
}

POPULATIONS = {
    'avondale': 89000, 'buckeye': 120000, 'chandler': 285000, 'el-mirage': 37000,
    'gilbert': 285000, 'glendale': 250000, 'goodyear': 110000, 'mesa': 520000,
    'peoria': 200000, 'phoenix': 1650000, 'scottsdale': 245000, 'surprise': 155000,
    'tempe': 190000, 'tolleson': 7500, 'queen-creek': 80000, 'cave-creek': 6500,
    'fountain-hills': 26000, 'litchfield-park': 6000, 'paradise-valley': 13000,
    'guadalupe': 6000, 'wickenburg': 8000, 'youngtown': 7000,
}


@housing_bp.route("")
def housing_index():
    view = request.args.get("view", "map")
    year = str(request.args.get("year", "2024"))
    city = request.args.get("city", "").strip().lower()
    chart_type = request.args.get("chart_type", "total")

    session = get_session()

    # All available years (strings for template comparison)
    years = [str(r[0]) for r in session.execute(
        text("SELECT DISTINCT year FROM housing_units WHERE year >= 2000 ORDER BY year DESC")
    ).all()]

    jurisdictions = sorted(CITY_DISPLAY.keys())

    # ── Per-city data for map & table ──
    city_where = "year = :year"
    city_params = {"year": year}
    if city:
        city_where += " AND jurisdiction_slug = :ct"
        city_params["ct"] = city

    city_rows = session.execute(text(f"""
        SELECT jurisdiction_slug,
               COALESCE(SUM(CASE WHEN unit_type='apartment' THEN units ELSE 0 END), 0) as apt,
               COALESCE(SUM(CASE WHEN unit_type='sf' THEN units ELSE 0 END), 0) as sf,
               COALESCE(SUM(units), 0) as total
        FROM housing_units WHERE {city_where}
        GROUP BY jurisdiction_slug ORDER BY total DESC
    """), city_params).all()

    city_data = []
    total_apt = 0
    total_sf = 0
    total_all = 0
    for r in city_rows:
        total_apt += r.apt
        total_sf += r.sf
        total_all += r.total
        city_data.append({
            'slug': r.jurisdiction_slug,
            'name': CITY_DISPLAY.get(r.jurisdiction_slug, r.jurisdiction_slug),
            'apt': r.apt, 'sf': r.sf, 'total': r.total,
            'per_capita': round(r.total / POPULATIONS.get(r.jurisdiction_slug, 1) * 1000, 2),
            'lat': CITY_LOCS.get(r.jurisdiction_slug, (33.4, -112.0))[0],
            'lng': CITY_LOCS.get(r.jurisdiction_slug, (33.4, -112.0))[1],
        })

    # ── Yearly data for chart ──
    yearly_where = "year >= 2000"
    yearly_params = {}
    if city:
        yearly_where += " AND jurisdiction_slug = :ct2"
        yearly_params["ct2"] = city

    yearly_rows = session.execute(text(f"""
        SELECT year,
               COALESCE(SUM(CASE WHEN unit_type='apartment' THEN units ELSE 0 END), 0) as apt,
               COALESCE(SUM(CASE WHEN unit_type='sf' THEN units ELSE 0 END), 0) as sf,
               COALESCE(SUM(units), 0) as total
        FROM housing_units WHERE {yearly_where}
        GROUP BY year ORDER BY year
    """), yearly_params).all()

    county_pop = POPULATIONS.get(city, 4700000) if city else 4700000
    city_yearly = [{
        'year': r.year, 'apartment': r.apt, 'sf': r.sf, 'total': r.total,
        'per_capita': round(r.total / county_pop * 1000, 2),
    } for r in yearly_rows]

    session.close()

    return render_template(
        "housing.html",
        view=view, year=year, city=city, chart_type=chart_type,
        years=years, jurisdictions=jurisdictions,
        city_data=city_data,
        city_yearly=city_yearly,
        total_apt=total_apt, total_sf=total_sf, total_all=total_all,
        POPULATIONS=POPULATIONS,
        CITY_LOCS_JSON=json.dumps(CITY_LOCS),
        CITY_DISPLAY_JSON=json.dumps(CITY_DISPLAY),
    )
