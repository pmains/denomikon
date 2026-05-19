"""Permits routes blueprint (aggregate view, chart data, filter helpers)."""

import logging
from collections import defaultdict
from typing import Optional

from flask import Blueprint, render_template, request, jsonify, redirect
from sqlalchemy import cast, Float, func, select, text as _sa_text

from db import get_session, Jurisdiction, PublicBody, Permit, PermitReport
from routes import _cache

log = logging.getLogger(__name__)

permits_bp = Blueprint("permits", __name__, url_prefix="")

@permits_bp.route("/permits")
@_cache(timeout=604800, query_string=True)  # 7 days — invalidated on sync
def permits_index():
    """Permit overview — aggregate summaries by default, raw list on request."""
    session = get_session()
    view = request.args.get("view", "aggregate")
    jurisdiction_filter = request.args.get("jurisdiction", "")
    category_filter = request.args.get("category", "")  # legacy single-category filter
    year_filter = request.args.get("year", "")
    native_type_filter = request.args.get("native_type", "").strip()
    units_filter = request.args.get("_units", "").strip().lower() == "true"

    # ── New positive inclusion filters ──
    categories_filter = request.args.get("categories", "").strip()
    work_types_filter = request.args.get("work_types", "").strip()

    from sqlalchemy import cast, Float, func

    # ── Legacy exclusion filters (backward compat) ──
    exclude_filter = request.args.get("exclude", "").strip()
    exclude_wt_filter = request.args.get("exclude_work_type", "").strip()

    # Convert legacy exclusion to inclusion when possible
    from sqlalchemy import text as _sa_text
    all_categories = sorted(set(
        r[0] for r in session.execute(
            _sa_text("SELECT DISTINCT normalized_category FROM permits WHERE normalized_category IS NOT NULL")
        ).all()
    ))
    all_work_types = sorted(set(
        r[0] for r in session.execute(
            _sa_text("SELECT DISTINCT work_type FROM permits WHERE work_type IS NOT NULL AND work_type != ''")
        ).all()
    ))

    if exclude_filter and not categories_filter:
        excluded = set(c.strip() for c in exclude_filter.split(",") if c.strip())
        included = [c for c in all_categories if c not in excluded]
        if included:
            categories_filter = ",".join(included)
        exclude_filter = ""  # clear so template doesn't show old UI

    if exclude_wt_filter and not work_types_filter:
        excluded = set(w.strip() for w in exclude_wt_filter.split(",") if w.strip())
        included = [w for w in all_work_types if w not in excluded]
        if included:
            work_types_filter = ",".join(included)
        exclude_wt_filter = ""  # clear

    # Parse inclusion lists
    selected_categories = [c.strip() for c in categories_filter.split(",") if c.strip()]
    selected_work_types = [w.strip() for w in work_types_filter.split(",") if w.strip()]

    # Gather distinct work_types for filter UI
    work_types_all = all_work_types

    # ── Helper: build inclusion-based WHERE clause parts ────────────────
    def _build_parts():
        parts = []
        params = {}
        if jurisdiction_filter:
            parts.append("p.jurisdiction = :jur")
            params["jur"] = jurisdiction_filter
        if selected_categories:
            phs = ",".join(f":cat_{i}" for i in range(len(selected_categories)))
            parts.append(f"p.normalized_category IN ({phs})")
            for i, c in enumerate(selected_categories):
                params[f"cat_{i}"] = c
        if selected_work_types:
            phs = ",".join(f":wt_{i}" for i in range(len(selected_work_types)))
            parts.append(f"p.work_type IN ({phs})")
            for i, w in enumerate(selected_work_types):
                params[f"wt_{i}"] = w
        if year_filter:
            parts.append("p.permit_issue_date LIKE :yr")
            params["yr"] = f"{year_filter}%"
        if native_type_filter:
            parts.append("p.native_type = :nt")
            params["nt"] = native_type_filter
        return parts, params

    # ── Filter builder for raw-list mode (non-deduped) ──────────────────
    def _base_filter(q):
        if jurisdiction_filter:
            q = q.where(Permit.jurisdiction == jurisdiction_filter)
        if selected_categories:
            q = q.where(Permit.normalized_category.in_(selected_categories))
        if selected_work_types:
            q = q.where(Permit.work_type.in_(selected_work_types))
        if year_filter:
            q = q.where(Permit.permit_issue_date.startswith(year_filter))
        if native_type_filter:
            q = q.where(Permit.native_type == native_type_filter)
        if units_filter:
            # Show only permits that carry housing units
            q = q.filter(
                func.coalesce(
                    func.cast(Permit.units, Float),
                    func.cast(Permit.no_units, Float),
                    0.0
                ) > 0
            )
        return q

    # ── Single dedup CTE with SQL GROUP BY ────────────────────────────────
    # Weekly reports are cumulative snapshots — the same permit can appear
    # in many reports.  A single dedup pass removes duplicates, then SQL
    # GROUP BY collapses the result into ~hundreds of aggregate rows that
    # Python reshapes for the template/chart structures.
    from sqlalchemy import text as _sa_text
    from collections import defaultdict

    parts, params = _build_parts()
    where = " AND ".join(parts) if parts else "1=1"

    sql = _sa_text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE {where}
        )
        SELECT d.jurisdiction,
               COALESCE(d.normalized_category, 'Other') AS category,
               d.native_type,
               d.work_type AS wt,
               SUBSTR(d.permit_issue_date, 1, 4) AS yr,
               COUNT(*) AS cnt,
               SUM(CAST(NULLIF(d.permit_valuation, '') AS REAL)) AS tot_val,
               SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)) AS tot_sqft,
               COALESCE(SUM(CAST(NULLIF(COALESCE(d.units, d.no_units, ''), '') AS REAL)), 0) AS tot_units,
               SUM(CASE WHEN LOWER(d.permit_status) IN ('finaled','final','completed','closed') THEN 1 ELSE 0 END) AS completed_cnt,
               SUM(CASE WHEN d.certificate_of_occupancy_date IS NOT NULL AND d.certificate_of_occupancy_date != '' THEN 1 ELSE 0 END) AS co_cnt
        FROM deduped d
        WHERE d.rn = 1
          AND d.permit_issue_date IS NOT NULL
        GROUP BY d.jurisdiction, d.normalized_category, d.native_type, d.work_type, yr
    """)

    jur_tot: dict = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0, "units": 0.0, "completed": 0, "co_issued": 0})
    cat_tot: dict = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0})
    type_cnt: dict = defaultdict(int)
    sqft_by_year: dict = defaultdict(lambda: defaultdict(float))
    cnt_by_year: dict = defaultdict(lambda: defaultdict(int))
    val_by_year: dict = defaultdict(lambda: defaultdict(float))
    all_cats: set = set()
    # Track new housing units (Residential + New Construction) per jurisdiction per year
    residential_units_cache: dict = defaultdict(lambda: defaultdict(int))

    for r in session.execute(sql, params):
        j = r.jurisdiction
        cat = r.category
        t = r.native_type
        yr = r.yr
        cnt = r.cnt or 0
        v = r.tot_val or 0.0
        s = r.tot_sqft or 0.0
        u = r.tot_units or 0.0
        comp = r.completed_cnt or 0
        co = r.co_cnt or 0

        wt = r.wt
        # Track residential new-construction units in the same pass
        is_new_housing = (
            cat == "Residential" and wt == "New Construction" and u > 0
        )

        if j:
            jt = jur_tot[j]
            jt["count"] += cnt
            jt["sqft"] += s
            jt["val"] += v
            jt["units"] += u
            jt["completed"] += comp
            jt["co_issued"] += co

        all_cats.add(cat)
        ct = cat_tot[cat]
        ct["count"] += cnt
        ct["sqft"] += s
        ct["val"] += v
        if yr:
            sqft_by_year[yr][cat] += s
            cnt_by_year[yr][cat] += cnt
            val_by_year[yr][cat] += v
        if t:
            type_cnt[t] += cnt

    # ── Batched housing-unit queries (3 total, not per-jurisdiction) ────
    # Each dedup strategy fires one query across all relevant jurisdictions
    # and returns (jurisdiction, year, units).  O(1) queries regardless of
    # how many jurisdictions exist — scales to 50+ without slowing down.
    #
    #   (A) Standard:     dedup by (permit_number, square_feet)
    #       → Phoenix RSF codes, Chandler, and future jurisdictions.
    #   (B) Phoenix PDD:   address-level dedup with stage-overcount correction
    #       → Multi-family under BLD/TCO codes not classified as Residential.
    #   (C) Tempe/Maricopa: same address dedup as B, different filter criteria.
    #
    # Rather than using NOT LIKE to exclude jurisdictions (which defeats
    # the B-tree index prefix), we query all distinct jurisdiction names
    # once, partition them by strategy in Python, and use an IN (...) clause
    # with exact names for index-efficient equality lookups.
    _HOUSING_CAPABLE_PDD = ['BLD','TCO','COND','LPRN','LPRR','LPRM','LPRT','LPRX','CSIT','PRLM','PAPP','PHAS','SCMJ','SCSU']
    _jur_names = [r[0] for r in session.execute(
        _sa_text("SELECT DISTINCT jurisdiction FROM permits")
    ).all()]
    _all_jur_lower = {j: j.lower() for j in _jur_names}

    # ── Strategy A: Standard dedup by (permit_number, square_feet) ──────
    # Tempe/Maricopa excluded — they use C with address-level dedup.
    _skip_a = selected_categories and "Residential" not in selected_categories
    if not _skip_a:
        # Identify non-Tempe, non-Maricopa jurisdictions
        _a_candidates = [j for j, lc in _all_jur_lower.items()
                         if "tempe" not in lc and "maricopa" not in lc]
        if jurisdiction_filter:
            _a_candidates = [j for j in _a_candidates if j == jurisdiction_filter]
        if _a_candidates:
            a_params = {}
            a_phs = ",".join(f":a{i}" for i in range(len(_a_candidates)))
            for i, j in enumerate(_a_candidates):
                a_params[f"a{i}"] = j
            if year_filter:
                yr_where = "AND p.permit_issue_date LIKE :yr"
                a_params["yr"] = f"{year_filter}%"
            else:
                yr_where = ""

            a_sql = _sa_text(f"""
                WITH deduped AS (
                    SELECT p.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                             COALESCE(p.permit_square_feet, '')
                               ORDER BY p.permit_issue_date
                           ) AS rn
                    FROM permits p
                    WHERE p.normalized_category = 'Residential'
                      AND p.work_type = 'New Construction'
                      AND p.jurisdiction IN ({a_phs})
                      {yr_where}
                )
                SELECT d.jurisdiction AS jur,
                       SUBSTR(d.permit_issue_date, 1, 4) AS yr,
                       SUM(CAST(NULLIF(COALESCE(d.units, d.no_units, ''), '') AS REAL)) AS units
                FROM deduped d
                WHERE d.rn = 1 AND d.permit_issue_date IS NOT NULL
                  AND CAST(NULLIF(COALESCE(d.units, d.no_units, ''), '') AS REAL) > 0
                GROUP BY d.jurisdiction, yr ORDER BY d.jurisdiction, yr
            """)
            for row in session.execute(a_sql, a_params):
                if row.units:
                    residential_units_cache[row.jur][row.yr] += int(row.units)

    # ── Strategy B: Phoenix PDD address dedup ──────────────────────────
    # Multi-family housing appearing under commercial PDD codes (BLD, TCO,
    # etc.) rather than Residential.  Only relevant to Phoenix.
    _skip_b = (selected_work_types and "New Construction" not in selected_work_types)
    if not _skip_b:
        _b_candidates = [j for j, lc in _all_jur_lower.items() if "phoenix" in lc]
        if jurisdiction_filter:
            _b_candidates = [j for j in _b_candidates if j == jurisdiction_filter]
        if _b_candidates:
            ht_list = ",".join(f"'{t}'" for t in _HOUSING_CAPABLE_PDD)
            b_params = {}
            b_phs = ",".join(f":b{i}" for i in range(len(_b_candidates)))
            for i, j in enumerate(_b_candidates):
                b_params[f"b{i}"] = j
            if year_filter:
                yr_where = "AND permit_issue_date LIKE :yr"
                b_params["yr"] = f"{year_filter}%"
            else:
                yr_where = ""

            b_sql = _sa_text(f"""
                SELECT jurisdiction AS jur, yr, SUM(corrected_units) AS units FROM (
                    SELECT jurisdiction, SUBSTR(MIN(permit_issue_date), 1, 4) AS yr,
                           job_address,
                           CASE
                               WHEN MIN(u) = MAX(u) AND MIN(u) > 1 AND COUNT(*) > 1 THEN MAX(u)
                               ELSE SUM(u)
                           END AS corrected_units
                    FROM (
                        SELECT jurisdiction, job_address, permit_issue_date,
                               CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) AS u
                        FROM permits
                        WHERE jurisdiction IN ({b_phs})
                          AND normalized_category NOT IN ('Residential','Demolition')
                          AND source_system = 'phoenix_pdd'
                          AND native_type IN ({ht_list})
                          AND job_address IS NOT NULL
                          AND CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) > 0
                          {yr_where}
                    )
                    GROUP BY jurisdiction, job_address
                    HAVING SUM(u) > 0
                ) GROUP BY jurisdiction, yr ORDER BY jurisdiction, yr
            """)
            for row in session.execute(b_sql, b_params):
                if row.units:
                    residential_units_cache[row.jur][row.yr] += int(row.units)

    # ── Strategy C: Tempe / Maricopa address dedup ─────────────────────
    # Same smart-address logic as B but for Residential + New Construction.
    _skip_c = (selected_work_types and "New Construction" not in selected_work_types)
    if not _skip_c:
        _c_candidates = [j for j, lc in _all_jur_lower.items()
                         if "tempe" in lc or "maricopa" in lc]
        if jurisdiction_filter:
            _c_candidates = [j for j in _c_candidates if j == jurisdiction_filter]
        if _c_candidates:
            c_params = {}
            c_phs = ",".join(f":c{i}" for i in range(len(_c_candidates)))
            for i, j in enumerate(_c_candidates):
                c_params[f"c{i}"] = j
            if year_filter:
                yr_where = "AND permit_issue_date LIKE :yr"
                c_params["yr"] = f"{year_filter}%"
            else:
                yr_where = ""

            c_sql = _sa_text(f"""
                SELECT jurisdiction AS jur, yr, SUM(corrected_units) AS units FROM (
                    SELECT jurisdiction, SUBSTR(MIN(permit_issue_date), 1, 4) AS yr,
                           job_address,
                           CASE
                               WHEN MIN(u) = MAX(u) AND MIN(u) > 1 AND COUNT(*) > 1 THEN MAX(u)
                               ELSE SUM(u)
                           END AS corrected_units
                    FROM (
                        SELECT jurisdiction, job_address, permit_issue_date,
                               CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) AS u
                        FROM permits
                        WHERE jurisdiction IN ({c_phs})
                          AND normalized_category = 'Residential'
                          AND work_type = 'New Construction'
                          AND job_address IS NOT NULL
                          AND CAST(NULLIF(COALESCE(units, no_units, ''), '') AS REAL) > 0
                          {yr_where}
                    )
                    GROUP BY jurisdiction, job_address
                    HAVING SUM(u) > 0
                ) GROUP BY jurisdiction, yr ORDER BY jurisdiction, yr
            """)
            for row in session.execute(c_sql, c_params):
                if row.units:
                    residential_units_cache[row.jur][row.yr] += int(row.units)

    # Ensure explicitly selected categories appear in chart data even with zero records
    if selected_categories:
        for c in selected_categories:
            all_cats.add(c)
            if c not in cat_tot:
                cat_tot[c] = {"count": 0, "sqft": 0.0, "val": 0.0}

    years = sorted(sqft_by_year.keys())

    # Build chart-data structures inline (no extra API round-trip)
    cats_ordered = sorted(all_cats, key=lambda x: -cat_tot[x]["count"])
    chart_sqft_by_year = {y: {c: sqft_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_cnt_by_year = {y: {c: cnt_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_val_by_year = {y: {c: val_by_year[y].get(c, 0) for c in cats_ordered} for y in years}
    chart_cat_totals = [
        {"category": c, "sqft": cat_tot[c]["sqft"],
         "valuation": cat_tot[c]["val"], "count": cat_tot[c]["count"]}
        for c in cats_ordered
    ]

    _EXCLUDED_JURISDICTIONS = {"City of Chandler"}
    by_jurisdiction = sorted(
        [{"jurisdiction": k, "count": v["count"],
          "total_valuation": v["val"], "total_sqft": v["sqft"],
          "avg_valuation": v["val"] / v["count"] if v["count"] else 0,
          "total_units": v["units"],
          "completed_count": v["completed"],
          "co_issued_count": v["co_issued"]}
         for k, v in jur_tot.items() if k not in _EXCLUDED_JURISDICTIONS],
        key=lambda r: r["count"], reverse=True,
    )

    # all_categories already queried above for backward-compat conversion
    by_category = sorted(
        [{"normalized_category": c,
          "count": cat_tot[c]["count"] if c in cat_tot else 0,
          "total_valuation": cat_tot[c]["val"] if c in cat_tot else 0,
          "total_sqft": cat_tot[c]["sqft"] if c in cat_tot else 0}
         for c in all_categories],
        key=lambda r: r["count"], reverse=True,
    )

    # ── Cross-jurisdiction type label normalization ─────────────────────
    # Phoenix uses short codes (RSF, BLD, SGNP). Tempe and Maricopa use
    # descriptive labels (Building (Residential), New Commercial).
    # Consolidate them into meaningful labels for the Top Types table.
    def _type_label(nt: str) -> str:
        """Map raw native_type to a consolidated, human-readable label."""
        if not nt:
            return "Other"
        code = nt.upper().strip()
        # Phoenix R-prefix codes → Residential
        if code.startswith("RSF") or code.startswith("RSME") or code == "RSP":
            return "Single-Family Home"
        if code.startswith("RS"):
            return "Single-Family Home"
        if code.startswith("RV"):
            return "Residential (Multi-Unit)"
        if code.startswith("RM") and not code.startswith("RMC"):
            return "Multi-Family"
        if code.startswith("RMC") or code.startswith("REC"):
            return "Residential (Commercial)"
        if code == "RPV" or code == "RPBI":
            return "Residential Patio Villa"
        if code == "RE" or code == "REM":
            return "Residential Alteration"
        if code == "RSE":
            return "Residential Alteration"
        if code.startswith("RPSC") or code.startswith("RPR"):
            return "Residential Alteration"
        if code.startswith("RWH") or code.startswith("RFEN"):
            return "Residential Alteration"
        if code.startswith("RNSP") or code == "RDEM":
            return "Residential Demolition"
        if code.startswith("RCIT") or code.startswith("RSTD"):
            return "Residential Addition"
        if code.startswith("R"):
            return "Residential (Other)"
        # Phoenix C-prefix and BLD → Commercial
        if code == "BLD" or code.startswith("BLDS") or code.startswith("BLDA") or code.startswith("BLSC"):
            return "Commercial Building"
        if code.startswith("CSW") or code.startswith("CSL"):
            return "Commercial Shell"
        if code.startswith("CSIT") or code.startswith("CSE") or code.startswith("CSLC"):
            return "Commercial Interior"
        if code.startswith("CCO") or code.startswith("CPR") or code.startswith("CES"):
            return "Commercial Alteration"
        if code.startswith("CGD") or code.startswith("CDW"):
            return "Commercial Grading"
        if code.startswith("CLS") or code.startswith("CLT") or code.startswith("CMC"):
            return "Commercial Construction"
        if code.startswith("CDF") or code.startswith("CPA"):
            return "Commercial Plan/Design"
        if code.startswith("CP") and code != "CPGD":
            return "Commercial Plan/Design"
        if code == "CPGD":
            return "Commercial Grading"
        if code.startswith("C"):
            return "Commercial (Other)"
        # Phoenix trade codes
        if code == "ELEC" or code.startswith("EL") or code == "PLMB" or code == "MECH":
            return "Trade (Elec/Plumb/Mech)"
        if code.startswith("ELEV") or code.startswith("ELFT"):
            return "Trade (Elevator)"
        if code.startswith("EHYD"):
            return "Trade (Hydronic)"
        if code.startswith("ENVR"):
            return "Trade (Environmental)"
        if code.startswith("ETRC"):
            return "Trade (Electrical Tr.)"
        # Phoenix FENCE permits
        if code == "FEN":
            return "Fence/Wall"
        # Phoenix fire codes → Trade
        if code.startswith("F") and len(code) >= 2 and code[1:].isdigit():
            return "Fire System"
        if code.startswith("FPP") or code.startswith("FPS") or code.startswith("FP"):
            return "Fire Protection"
        if code.startswith("FBB") or code.startswith("FITM"):
            return "Fire Protection"
        if code.startswith("FLRV"):
            return "Fire Protection"
        if code.startswith("FLSR") or code.startswith("FOCS") or code.startswith("FPAP"):
            return "Fire Protection"
        # Phoenix sign codes
        if code.startswith("SGN") or code == "S":
            return "Sign"
        # Phoenix SE, SME, SCSR, SP, etc.
        if code.startswith("SE") or code.startswith("SME") or code == "SM":
            return "Service Existing"
        if code.startswith("SP") or code.startswith("SPE") or code.startswith("SPM"):
            return "Trade (Other)"
        if code.startswith("SC"):
            return "Trade (Other)"
        # Phoenix LP/LS codes → Land Use / Plan Review
        if code.startswith("LPRM") or code.startswith("LPRR") or code.startswith("LPRS"):
            return "Plan Review"
        if code.startswith("LP"):
            return "Plan Review"
        if code.startswith("LS"):
            return "Plan Review"
        # Phoenix infrastructure
        if code.startswith("WS"):
            return "Infrastructure (Water/Sewer)"
        if code.startswith("TRFN"):
            return "Infrastructure (Traffic)"
        # Phoenix demolition
        if code.startswith("DEM") or code.startswith("ABND"):
            return "Demolition"
        # Phoenix pool
        if code.startswith("POOL"):
            return "Pool"
        # Phoenix other/existing
        if code.startswith("OE") or code.startswith("OP") or code.startswith("OS"):
            return "Other Existing"
        if code.startswith("OBLD"):
            return "Other Existing"
        if code.startswith("OM"):
            return "Other Existing"
        if code.startswith("PHAS") or code.startswith("PLAT") or code.startswith("PLZA"):
            return "Plans/Zoning"
        if code.startswith("PAPP") or code.startswith("PR"):
            return "Plans/Zoning"
        if code.startswith("COFO") or code.startswith("COFC"):
            return "Certificate of Occupancy"
        if code.startswith("TCO"):
            return "Temp Certificate of Occupancy"
        if code.startswith("MHZ") or code.startswith("MDHM"):
            return "Manufactured/Mobile Home"
        if code.startswith("INSP"):
            return "Inspection"
        if code.startswith("AMND"):
            return "Amendment"
        if code.startswith("EXTR"):
            return "Excavation/Trench"
        if code.startswith("CAT"):
            return "Catenary/Telecom"
        if code.startswith("CHA"):
            return "Change of Use"
        if code.startswith("CHG"):
            return "Change"
        if code.startswith("DAPP") or code.startswith("DEDI"):
            return "Design/Development"
        if code.startswith("SC") or code == "SM" or code == "SP" or code.startswith("SPE"):
            return "Trade (Other)"
        if code.startswith("BLD-") and "RESIDENTIAL" in nt.upper():
            return "Single-Family Home"
        if code.startswith("BLD-") and "COMMERCIAL" in nt.upper():
            return "Commercial Building"
        # Tempe/Maricopa descriptive labels — normalize these too
        low = nt.lower()
        if "residential" in low and ("new" in low or "build" in low):
            return "Single-Family Home"
        if "residential" in low and ("alter" in low or "addition" in low):
            return "Residential Alteration"
        if "commercial" in low and ("new" in low or "build" in low):
            return "Commercial Building"
        if "commercial" in low and "alter" in low:
            return "Commercial Alteration"
        if "trade" in low or "electrical" in low or "plumbing" in low or "mechanical" in low:
            return "Trade (General)"
        if "demolition" in low:
            return "Demolition"
        if "infrastructure" in low or "grading" in low:
            return "Infrastructure"
        if "standard" in low or "plan" in low:
            return "Standard Plan"
        if "sign" in low or "awning" in low:
            return "Sign"
        if "pool" in low or "spa" in low:
            return "Pool/Spa"
        if "fire" in low or "sprinkler" in low or "alarm" in low:
            return "Fire System"
        if "fence" in low or "wall" in low:
            return "Fence/Wall"
        if "roof" in low:
            return "Roof"
        if "solar" in low or "photovoltaic" in low:
            return "Solar/PV"
        if "foundation" in low:
            return "Foundation"
        if "occupancy" in low:
            return "Certificate of Occupancy"
        if "addition" in low:
            return "Addition"
        # Fallback: use the raw type but clean it up a bit
        return nt.strip()

    # Build type counts by consolidated label
    type_label_cnt: dict = defaultdict(int)
    for k, v in type_cnt.items():
        label = _type_label(k)
        type_label_cnt[label] += v

    by_type_top = sorted(
        [{"type": k, "count": v} for k, v in type_label_cnt.items()],
        key=lambda r: r["count"], reverse=True,
    )[:20]

    # Available filter options — respect jurisdiction for year list
    yr_q = select(Permit.permit_issue_date).distinct().where(
        Permit.permit_issue_date.isnot(None), Permit.permit_issue_date != ""
    )
    if jurisdiction_filter:
        yr_q = yr_q.where(Permit.jurisdiction == jurisdiction_filter)
    year_options = session.execute(
        yr_q.order_by(Permit.permit_issue_date.desc())
    ).scalars().all()
    # Extract unique years from ISO dates
    year_options = sorted(set(d[:4] for d in year_options if d and len(d) >= 4), reverse=True)

    # Compute zero-categories note: selected categories with zero matching records
    zero_categories = [c for c in selected_categories if cat_tot.get(c, {}).get("count", 0) == 0]

    jurisdictions = [
        j for j in session.execute(
            select(Permit.jurisdiction).distinct().where(Permit.jurisdiction.isnot(None)).order_by(Permit.jurisdiction)
        ).scalars().all()
        if j not in _EXCLUDED_JURISDICTIONS
    ]

    # When year is selected, also filter jurisdictions to those active that year
    if year_filter:
        jur_q = select(Permit.jurisdiction).distinct().where(
            Permit.jurisdiction.isnot(None),
            Permit.permit_issue_date.startswith(year_filter),
        )
        filtered_jurs = [r[0] for r in session.execute(jur_q.order_by(Permit.jurisdiction)).all()]
        if filtered_jurs:
            jurisdictions = [j for j in filtered_jurs if j not in _EXCLUDED_JURISDICTIONS]

    categories = all_categories  # from backward-compat query above

    # Raw list mode
    permits_raw = []
    page = 1
    total_pages = 1
    total = 0
    per_page = 25

    if view == "raw":
        page = request.args.get("page", 1, type=int)
        if units_filter:
            # Group related permits by address so multiple stages of the same
            # project appear together
            base_q = select(Permit).order_by(Permit.job_address.asc().nullslast(), Permit.permit_issue_date.desc().nullslast(), Permit.id.desc())
        else:
            base_q = select(Permit).order_by(Permit.permit_issue_date.desc().nullslast(), Permit.id.desc())
        count_q = select(func.count(Permit.id))
        base_q = _base_filter(base_q)
        count_q = _base_filter(count_q)
        total = session.execute(count_q).scalar() or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        permits_raw = session.execute(base_q.offset(offset).limit(per_page)).scalars().all()

    # ── Residential units by jurisdiction and year ────────────────────────
    # Data was accumulated from the same dedup pass above (no second CTE scan).
    # residential_units_cache[jur][year] = units
    units_by_jur_year: dict = defaultdict(list)
    all_unit_years: set = set()
    new_housing_by_jur: dict = defaultdict(int)
    for jur, yr_data in residential_units_cache.items():
        for yr, u in yr_data.items():
            if jur not in _EXCLUDED_JURISDICTIONS:
                units_by_jur_year[jur].append({"year": yr, "units": u})
                all_unit_years.add(yr)
                new_housing_by_jur[jur] += u

    # Override total_units in by_jurisdiction with new housing units
    for entry in by_jurisdiction:
        jur = entry["jurisdiction"]
        nh = new_housing_by_jur.get(jur, 0)
        entry["total_units"] = nh

    session.close()
    return render_template(
        "permits.html",
        view=view,
        by_jurisdiction=by_jurisdiction,
        by_category=by_category,
        by_type_top=by_type_top,
        permits_raw=permits_raw,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
        years=year_options,
        jurisdictions=jurisdictions,
        categories=categories,
        jurisdiction_filter=jurisdiction_filter,
        category_filter=category_filter,
        year_filter=year_filter,
        categories_filter=categories_filter,
        work_types_filter=work_types_filter,
        selected_categories=selected_categories,
        selected_work_types=selected_work_types,
        zero_categories=zero_categories,
        work_types_all=work_types_all,
        units_filter=units_filter,
        native_type_filter=native_type_filter,
        chart_data={
            "years": years,
            "sqft_by_year": chart_sqft_by_year,
            "permits_by_year": chart_cnt_by_year,
            "valuation_by_year": chart_val_by_year,
            "category_totals": chart_cat_totals,
            "residential_units": {
                "by_jurisdiction": dict(units_by_jur_year),
                "years": sorted(all_unit_years),
            },
        },
    )


@permits_bp.route("/api/permits/chart-data")
def permits_chart_data():
    """JSON endpoint with deduped chart data for the permits template.

    Returns sqft_by_year, permits_by_year, and category_totals,
    optionally filtered by jurisdiction, category, work type, or year.
    """
    from sqlalchemy import text
    session = get_session()
    jf = request.args.get("jurisdiction", "")
    cf = request.args.get("category", "")
    yf = request.args.get("year", "")

    # New positive inclusion filters
    categories_filter = request.args.get("categories", "").strip()
    work_types_filter = request.args.get("work_types", "").strip()

    # Legacy exclusion filters (backward compat)
    ef = request.args.get("exclude", "").strip()
    ewtf = request.args.get("exclude_work_type", "").strip()

    # Convert legacy exclusion to inclusion
    if ef and not categories_filter:
        all_cats = sorted(set(
            r[0] for r in session.execute(
                text("SELECT DISTINCT normalized_category FROM permits WHERE normalized_category IS NOT NULL")
            ).all()
        ))
        excluded = set(c.strip() for c in ef.split(",") if c.strip())
        included = [c for c in all_cats if c not in excluded]
        if included:
            categories_filter = ",".join(included)
        ef = ""

    if ewtf and not work_types_filter:
        all_wts = sorted(set(
            r[0] for r in session.execute(
                text("SELECT DISTINCT work_type FROM permits WHERE work_type IS NOT NULL AND work_type != ''")
            ).all()
        ))
        excluded = set(w.strip() for w in ewtf.split(",") if w.strip())
        included = [w for w in all_wts if w not in excluded]
        if included:
            work_types_filter = ",".join(included)
        ewtf = ""

    selected_cats = [c.strip() for c in categories_filter.split(",") if c.strip()]
    selected_wts = [w.strip() for w in work_types_filter.split(",") if w.strip()]

    parts = ["1=1"]
    params = {}
    if jf:
        parts.append("p.jurisdiction = :jur")
        params["jur"] = jf
    if cf:
        parts.append("p.normalized_category = :cat")
        params["cat"] = cf
    if selected_cats:
        phs = ",".join(f":cat_{i}" for i in range(len(selected_cats)))
        parts.append(f"p.normalized_category IN ({phs})")
        for i, c in enumerate(selected_cats):
            params[f"cat_{i}"] = c
    if selected_wts:
        phs = ",".join(f":wt_{i}" for i in range(len(selected_wts)))
        parts.append(f"p.work_type IN ({phs})")
        for i, w in enumerate(selected_wts):
            params[f"wt_{i}"] = w
    if yf:
        parts.append("p.permit_issue_date LIKE :yr")
        params["yr"] = f"{yf}%"
    where = " AND ".join(parts)

    # Years that have data, sorted
    years_sql = text(f"""
        SELECT DISTINCT SUBSTR(p.permit_issue_date, 1, 4) AS yr
        FROM permits p
        WHERE p.permit_issue_date IS NOT NULL AND {where}
        ORDER BY yr
    """)
    years = [r[0] for r in session.execute(years_sql, params).all()]

    # Sqft per year per category
    sqft_sql = text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE {where}
        )
        SELECT SUBSTR(d.permit_issue_date, 1, 4) AS yr,
               COALESCE(d.normalized_category, 'Other') AS cat,
               COALESCE(SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)), 0) AS sqft,
               COUNT(*) AS cnt
        FROM deduped d
        WHERE d.rn = 1 AND d.permit_issue_date IS NOT NULL
        GROUP BY yr, cat
        ORDER BY yr, cat
    """)
    sqft_by_year: dict[str, dict[str, float]] = {}
    permits_by_year: dict[str, dict[str, int]] = {}
    for r in session.execute(sqft_sql, params).all():
        yr, cat, sqft, cnt = r
        sqft_by_year.setdefault(yr, {})[cat] = sqft
        permits_by_year.setdefault(yr, {})[cat] = cnt

    # Category totals (all years)
    cat_totals_sql = text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE {where}
        )
        SELECT COALESCE(d.normalized_category, 'Other') AS cat,
               COALESCE(SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)), 0) AS sqft,
               COALESCE(SUM(CAST(NULLIF(d.permit_valuation, '') AS REAL)), 0) AS valuation,
               COUNT(*) AS cnt
        FROM deduped d
        WHERE d.rn = 1
        GROUP BY cat
        ORDER BY cnt DESC
    """)
    category_totals: list[dict] = []
    for r in session.execute(cat_totals_sql, params).all():
        category_totals.append({"category": r[0], "sqft": r[1], "valuation": r[2], "count": r[3]})

    session.close()

    return {
        "years": years,
        "sqft_by_year": sqft_by_year,
        "permits_by_year": permits_by_year,
        "category_totals": category_totals,
    }


@permits_bp.route("/permits/category/<category_name>")
def permit_category_detail(category_name):
    """Year-over-year breakdown for a single permit category.

    Shows a line chart and data table of sqft / count / valuation
    across all available years, optionally filtered by jurisdiction.
    """
    session = get_session()
    jurisdiction_filter = request.args.get("jurisdiction", "")
    exclude_filter = request.args.get("exclude", "").strip()
    exclude_cats = [c.strip() for c in exclude_filter.split(",") if c.strip()]

    parts = ["1=1", "d.rn = 1"]
    params = {"cat": category_name}
    if jurisdiction_filter:
        parts.append("d.jurisdiction = :jur")
        params["jur"] = jurisdiction_filter
    if exclude_cats:
        placeholders = ",".join(f":exc_{i}" for i in range(len(exclude_cats)))
        parts.append(f"d.normalized_category NOT IN ({placeholders})")
        for i, c in enumerate(exclude_cats):
            params[f"exc_{i}"] = c
    where = " AND ".join(parts)

    from sqlalchemy import text

    sql = text(f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(p.permit_number, p.row_hash),
                                     COALESCE(p.permit_square_feet, '')
                       ORDER BY p.permit_issue_date
                   ) AS rn
            FROM permits p
            WHERE COALESCE(p.normalized_category, 'Other') = :cat
              AND p.permit_issue_date IS NOT NULL
        )
        SELECT SUBSTR(d.permit_issue_date, 1, 4) AS yr,
               COUNT(*) AS cnt,
               COALESCE(SUM(CAST(NULLIF(d.permit_square_feet, '') AS REAL)), 0) AS sqft,
               COALESCE(SUM(CAST(NULLIF(d.permit_valuation, '') AS REAL)), 0) AS valuation
        FROM deduped d
        WHERE {where}
        GROUP BY yr
        ORDER BY yr
    """)

    yearly = [
        {"year": r[0], "count": r[1], "sqft": r[2], "valuation": r[3]}
        for r in session.execute(sql, params).all()
    ]

    session.close()
    return render_template(
        "permit_category.html",
        category=category_name,
        yearly=yearly,
        jurisdiction_filter=jurisdiction_filter,
    )

