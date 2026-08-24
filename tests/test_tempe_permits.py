"""Tests for the City of Tempe ArcGIS permit scraper."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test

# Import db first, then switch to temp database
import db as _db_mod
from db import (
    init_db,
    get_session,
    Permit,
)
from scraper.jurisdictions.tempe_permits import (
    _parse_arcgis_date,
    _build_query_url,
    normalize_row,
    categorize_permit,
    classify_work_type,
    sync_permits,
    ARCGIS_URL,
    MAX_RECORD_COUNT,
    SOURCE_SYSTEM,
)
# conftest.py sets POLISCOPIC_DB_TIER=test which handles temp DB creation.
# Use init_db() in setUp/setUpClass — conftest manages the database lifecycle.

from sqlalchemy import func, select


# ── ArcGIS date parsing ────────────────────────────────────────────────────

class TestArcGISDateParse(unittest.TestCase):
    """Test the _parse_arcgis_date helper for various date formats."""

    def test_milliseconds(self):
        """/Date(1711929600000)/ -> 2024-04-01"""
        result = _parse_arcgis_date("/Date(1711929600000)/")
        self.assertEqual(result, "2024-04-01")

    def test_milliseconds_early(self):
        """/Date(1704067200000)/ -> 2024-01-01"""
        result = _parse_arcgis_date("/Date(1704067200000)/")
        self.assertEqual(result, "2024-01-01")

    def test_iso_date(self):
        """2024-01-15T00:00:00.000Z -> 2024-01-15"""
        result = _parse_arcgis_date("2024-01-15T00:00:00.000Z")
        self.assertEqual(result, "2024-01-15")

    def test_iso_date_no_tz(self):
        """2024-06-01T12:00:00 -> 2024-06-01"""
        result = _parse_arcgis_date("2024-06-01T12:00:00")
        self.assertEqual(result, "2024-06-01")

    def test_iso_date_plain(self):
        """2024-03-15 -> 2024-03-15"""
        result = _parse_arcgis_date("2024-03-15")
        self.assertEqual(result, "2024-03-15")

    def test_null_value(self):
        """None -> None"""
        result = _parse_arcgis_date(None)
        self.assertIsNone(result)

    def test_empty_string(self):
        """Empty string -> None"""
        result = _parse_arcgis_date("")
        self.assertIsNone(result)

    def test_whitespace(self):
        """'  ' -> None"""
        result = _parse_arcgis_date("  ")
        self.assertIsNone(result)


# ── URL construction ───────────────────────────────────────────────────────

class TestPaginationUrl(unittest.TestCase):
    """Verify ArcGIS query URL construction with pagination offsets."""

    def test_default_offset(self):
        url = _build_query_url(offset=0, count=2000)
        self.assertIn("resultOffset=0", url)
        self.assertIn("resultRecordCount=2000", url)
        self.assertIn(ARCGIS_URL, url)

    def test_offset_2000(self):
        url = _build_query_url(offset=2000, count=2000)
        self.assertIn("resultOffset=2000", url)
        self.assertIn("resultRecordCount=2000", url)

    def test_custom_count(self):
        url = _build_query_url(offset=0, count=500)
        self.assertIn("resultOffset=0", url)
        self.assertIn("resultRecordCount=500", url)

    def test_has_required_params(self):
        url = _build_query_url()
        self.assertIn("where=1%3D1", url)  # URL-encoded: where=1=1
        self.assertIn("outFields=%2A", url)  # URL-encoded: outFields=*
        self.assertIn("returnGeometry=false", url)
        self.assertIn("f=json", url)


# ── Normalization ──────────────────────────────────────────────────────────

class TestNormalization(unittest.TestCase):
    """Test the normalize_row mapping from ArcGIS fields to Permit fields."""

    def test_minimal_row(self):
        """Row with only PermitNum should produce valid output."""
        row = {"PermitNum": "BLD-2024-00001", "OBJECTID": 1001}
        result = normalize_row(row)
        self.assertEqual(result["permit_number"], "BLD-2024-00001")
        self.assertEqual(result["source_record_id"], "1001")
        self.assertEqual(result["source_system"], SOURCE_SYSTEM)
        self.assertEqual(result["jurisdiction"], "City of Tempe")
        self.assertIsNotNone(result["row_hash"])
        self.assertEqual(result["normalized_category"], "Other")

    def test_full_row(self):
        """Row with all core fields mapped correctly."""
        row = {
            "PermitNum": "BLD-2024-00123",
            "Description": "Construct single-family residence",
            "IssuedDateDtm": "/Date(1711929600000)/",
            "AppliedDateDtm": "/Date(1711142400000)/",
            "CompletedDateDtm": "/Date(1719446400000)/",
            "COIssuedDateDtm": "/Date(1719532800000)/",
            "StatusCurrent": "Finaled",
            "OriginalAddress1": "123 Main St",
            "OriginalCity": "Tempe",
            "OriginalState": "AZ",
            "OriginalZip": 85281,
            "PermitType": "Residential - New",
            "PermitTypeDesc": "Residential",
            "PermitClass": "New",
            "TotalSqFt": 2500,
            "HousingUnits": 1,
            "EstProjectCost": 350000,
            "ProjectName": "Smith Residence",
            "Fee": 1500.50,
            "Latitude": 33.4145,
            "Longitude": -111.9128,
            "ContractorCompanyName": "ABC Construction",
            "ContractorLicNum": "ROC-123456",
            "Zone": "R1-6",
            "OBJECTID": 2002,
        }
        result = normalize_row(row)
        self.assertEqual(result["permit_number"], "BLD-2024-00123")
        self.assertEqual(result["permit_description"], "Construct single-family residence")
        self.assertEqual(result["permit_issue_date"], "2024-04-01")
        self.assertEqual(result["applied_date"], "2024-03-22")
        self.assertEqual(result["completed_date"], "2024-06-27")
        self.assertEqual(result["certificate_of_occupancy_date"], "2024-06-28")
        self.assertEqual(result["permit_status"], "Finaled")
        self.assertEqual(result["job_address"], "123 Main St")
        self.assertEqual(result["job_city"], "Tempe")
        self.assertEqual(result["job_state"], "AZ")
        self.assertEqual(result["job_zip"], "85281")
        self.assertEqual(result["raw_permit_type"], "Residential - New")
        self.assertEqual(result["raw_permit_type_description"], "Residential")
        self.assertEqual(result["raw_permit_class"], "New")
        self.assertEqual(result["permit_square_feet"], "2500")
        self.assertEqual(result["units"], "1")
        self.assertEqual(result["no_units"], "1")
        self.assertEqual(result["permit_valuation"], "350000")
        self.assertEqual(result["project_name"], "Smith Residence")
        self.assertEqual(result["fee"], "1500.5")
        self.assertEqual(result["latitude"], "33.4145")
        self.assertEqual(result["longitude"], "-111.9128")
        self.assertEqual(result["contractor_name"], "ABC Construction")
        self.assertEqual(result["contractor_license"], "ROC-123456")
        self.assertEqual(result["zone"], "R1-6")
        self.assertEqual(result["source_record_id"], "2002")

    def test_housing_units(self):
        """Multifamily permit with HousingUnits."""
        row = {"PermitNum": "BLD-2024-00777", "OBJECTID": 3003, "HousingUnits": 24}
        result = normalize_row(row)
        self.assertEqual(result["units"], "24")
        self.assertEqual(result["no_units"], "24")

    def test_housing_units_zero(self):
        """HousingUnits of 0 should remain 0."""
        row = {"PermitNum": "BLD-2024-00888", "OBJECTID": 3004, "HousingUnits": 0}
        result = normalize_row(row)
        self.assertEqual(result["units"], "0")
        self.assertEqual(result["no_units"], "0")

    def test_housing_units_null(self):
        """None HousingUnits should be None."""
        row = {"PermitNum": "BLD-2024-00999", "OBJECTID": 3005, "HousingUnits": None}
        result = normalize_row(row)
        self.assertIsNone(result["units"])
        self.assertIsNone(result["no_units"])

    def test_total_sqft(self):
        """TotalSqFt of 5000.0 maps to '5000'."""
        row = {"PermitNum": "BLD-2024-00100", "OBJECTID": 4001, "TotalSqFt": 5000.0}
        result = normalize_row(row)
        self.assertEqual(result["permit_square_feet"], "5000")

    def test_total_sqft_null(self):
        """None TotalSqFt should be None."""
        row = {"PermitNum": "BLD-2024-00101", "OBJECTID": 4002, "TotalSqFt": None}
        result = normalize_row(row)
        self.assertIsNone(result["permit_square_feet"])

    def test_est_project_cost(self):
        """EstProjectCost of 500000 maps to '500000'."""
        row = {"PermitNum": "BLD-2024-00200", "OBJECTID": 5001, "EstProjectCost": 500000}
        result = normalize_row(row)
        self.assertEqual(result["permit_valuation"], "500000")

    def test_lat_lng(self):
        """Latitude and Longitude are mapped."""
        row = {"PermitNum": "BLD-2024-00300", "OBJECTID": 6001, "Latitude": 33.4145, "Longitude": -111.9128}
        result = normalize_row(row)
        self.assertEqual(result["latitude"], "33.4145")
        self.assertEqual(result["longitude"], "-111.9128")

    def test_geometry_coordinates_null(self):
        """None coordinates should be None."""
        row = {"PermitNum": "BLD-2024-00301", "OBJECTID": 6002, "Latitude": None, "Longitude": None}
        result = normalize_row(row)
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["longitude"])

    def test_row_hash_stable(self):
        """Same row should produce the same hash."""
        row1 = {"PermitNum": "BLD-2024-99999", "OBJECTID": 9999}
        row2 = {"PermitNum": "BLD-2024-99999", "OBJECTID": 9999}
        r1 = normalize_row(row1)
        r2 = normalize_row(row2)
        self.assertEqual(r1["row_hash"], r2["row_hash"])

    def test_completed_date_set(self):
        """CompletedDateDtm is mapped from ArcGIS field."""
        row = {"PermitNum": "BLD-2024-00400", "OBJECTID": 7001,
               "CompletedDateDtm": "/Date(1719446400000)/"}
        result = normalize_row(row)
        self.assertEqual(result["completed_date"], "2024-06-27")

    def test_co_issued_date_set(self):
        """COIssuedDateDtm is mapped from ArcGIS field."""
        row = {"PermitNum": "BLD-2024-00401", "OBJECTID": 7002,
               "COIssuedDateDtm": "/Date(1719532800000)/"}
        result = normalize_row(row)
        self.assertEqual(result["certificate_of_occupancy_date"], "2024-06-28")


# ── Category normalization ─────────────────────────────────────────────────

class TestCategorizePermit(unittest.TestCase):
    """Test the categorize_permit function for various Tempe permit types."""

    def test_residential_new(self):
        self.assertEqual(categorize_permit("Residential - New"), "Residential")

    def test_residential_alteration(self):
        self.assertEqual(categorize_permit("Residential - Alteration"), "Residential")

    def test_commercial_new(self):
        self.assertEqual(categorize_permit("Commercial - New"), "Commercial")

    def test_commercial_alteration(self):
        self.assertEqual(categorize_permit("Commercial - Alteration"), "Commercial")

    def test_commercial_shell(self):
        self.assertEqual(categorize_permit("Commercial - Shell"), "Commercial")

    def test_commercial_bare(self):
        self.assertEqual(categorize_permit("Commercial"), "Commercial")

    def test_industrial(self):
        self.assertEqual(categorize_permit("Industrial"), "Industrial")

    def test_mixed_use(self):
        self.assertEqual(categorize_permit("Mixed"), "Mixed-Use")
        self.assertEqual(categorize_permit("Mixed Use"), "Mixed-Use")
        self.assertEqual(categorize_permit("Mixed-Use"), "Mixed-Use")

    def test_sign_fallback(self):
        """Sign permit categorized as Commercial."""
        self.assertEqual(categorize_permit("Sign"), "Commercial")

    def test_electrical_via_desc(self):
        """Fallback to description when permit_type is None."""
        result = categorize_permit(
            raw_permit_type=None,
            raw_permit_type_desc="Electrical",
            description="Residential electrical service",
        )
        self.assertEqual(result, "Residential")

    def test_fallback_type_desc(self):
        """Use PermitTypeDesc when PermitType is None."""
        result = categorize_permit(
            raw_permit_type=None,
            raw_permit_type_desc="Residential",
        )
        self.assertEqual(result, "Residential")

    def test_fallback_description(self):
        """Use Description as last resort."""
        result = categorize_permit(
            raw_permit_type=None,
            raw_permit_type_desc=None,
            description="New commercial building",
        )
        self.assertEqual(result, "Commercial")

    def test_no_text(self):
        """All None -> Other."""
        result = categorize_permit(None, None, None)
        self.assertEqual(result, "Other")

    # ── raw_permit_class tests ──────────────────────────────────────────
    def test_class_non_residential(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="437 - Additions and Alterations - Non-Residential")
        self.assertEqual(cat, "Commercial")

    def test_class_ten_or_more_family(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="106 New - Ten or more Family")
        self.assertEqual(cat, "Residential")

    def test_class_single_family_attached(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="102 New - Single Family Attached")
        self.assertEqual(cat, "Residential")

    def test_class_commercial_building(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="330 - Commercial Buildings")
        self.assertEqual(cat, "Commercial")

    def test_class_photovoltaic_residential(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="801 - Photovoltaic Residential")
        self.assertEqual(cat, "Residential")

    def test_class_photovoltaic_commercial(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="806 - Photovoltaic Commercial")
        self.assertEqual(cat, "Commercial")

    def test_class_miscellaneous(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="999 - Miscellaneous(...)")
        self.assertEqual(cat, "Other")

    def test_class_residential_alteration(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="434 - Additions or Alterations - Residential")
        self.assertEqual(cat, "Residential")

    def test_class_pool_residential(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="992 - Pool - Residential")
        self.assertEqual(cat, "Residential")

    def test_class_pool_non_residential(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="993 - Pool - Non-Residential")
        self.assertEqual(cat, "Commercial")

    def test_class_infrastructure_water(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="WA - Water")
        self.assertEqual(cat, "Infrastructure")

    def test_class_infrastructure_sewer(self):
        cat = categorize_permit(None, None, None,
                                raw_permit_class="SW - Sewer")
        self.assertEqual(cat, "Infrastructure")

    def test_empty_strings(self):
        """Empty strings -> Other."""
        result = categorize_permit("", "", "")
        self.assertEqual(result, "Other")

    def test_infrastructure_water(self):
        result = categorize_permit("Water", None, None)
        self.assertEqual(result, "Infrastructure")

    def test_infrastructure_street(self):
        result = categorize_permit(None, "Street Improvements", None)
        self.assertEqual(result, "Infrastructure")

    # ── work_type classification tests ──
    def test_work_type_new_single_family(self):
        self.assertEqual(classify_work_type("101 New - Single Family Detached"), "New Construction")

    def test_work_type_new_multi_family(self):
        self.assertEqual(classify_work_type("106 New - Ten or more Family"), "New Construction")

    def test_work_type_foundation(self):
        self.assertEqual(classify_work_type("107 - Foundation Only"), "New Construction")

    def test_work_type_alteration_ti(self):
        self.assertEqual(classify_work_type("437 - Additions and Alterations - Non-Residential",
                                              "TI - SUITE 100"), "Alteration")

    def test_work_type_alteration_remodel(self):
        self.assertEqual(classify_work_type("434 - Additions or Alterations - Residential",
                                              "RESIDENTIAL INTERIOR REMODEL"), "Alteration")

    def test_work_type_add_adu(self):
        self.assertEqual(classify_work_type("113 - Guesthouse", "NEW DETACHED ADU"), "Addition")

    def test_work_type_add_carport(self):
        self.assertEqual(classify_work_type("438 - Carports - Commercial and Cantilever"), "Addition")

    def test_work_type_trade_electrical(self):
        self.assertEqual(classify_work_type("997 - Electrical - No Value"), "Trade")

    def test_work_type_demolition(self):
        self.assertEqual(classify_work_type("644 - CM Demolition - All Building"), "Demolition")

    def test_work_type_infrastructure_water(self):
        self.assertEqual(classify_work_type("WA - Water"), "Infrastructure")

    def test_work_type_new_construction_new_in_desc(self):
        self.assertEqual(classify_work_type("999 - Miscellaneous", "NEW MIXED USE BUILDING"), "New Construction")

    def test_work_type_unknown_misc(self):
        self.assertEqual(classify_work_type("999 - Miscellaneous"), "Unknown")

    def test_work_type_unknown_sfr(self):
        self.assertEqual(classify_work_type("SFR - Single Family Residence"), "Unknown")


# ── Database upsert ────────────────────────────────────────────────────────

class TestDatabaseUpsert(unittest.TestCase):
    """Test idempotent upsert behavior with the Tempe permit scraper."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        session = get_session()
        session.query(Permit).delete()
        session.commit()
        session.close()

    def test_idempotent_upsert(self):
        """Same row inserted twice should not duplicate."""
        from scraper.jurisdictions.tempe_permits import normalize_row

        arcgis_row = {
            "PermitNum": "BLD-2024-TEST01",
            "OBJECTID": 12345,
            "PermitType": "Residential - New",
            "Description": "Test permit",
            "IssuedDateDtm": "/Date(1711929600000)/",
        }

        session = get_session()

        # Insert first time
        norm = normalize_row(arcgis_row)
        permit = Permit(**norm)
        session.add(permit)
        session.commit()

        count1 = session.execute(select(func.count()).select_from(Permit)).scalar() or 0

        # Insert second time (should update, not create new)
        norm2 = normalize_row(arcgis_row)
        existing = session.execute(
            select(Permit).where(
                Permit.source_system == SOURCE_SYSTEM,
                Permit.source_record_id == "12345",
            )
        ).scalar_one_or_none()
        if existing:
            for col, val in norm2.items():
                if col not in ("row_hash", "report_date", "report_adid", "source_file"):
                    setattr(existing, col, val)
        else:
            session.add(Permit(**norm2))
        session.commit()

        count2 = session.execute(select(func.count()).select_from(Permit)).scalar() or 0
        self.assertEqual(count2, count1, "Upsert should not create duplicate")

        session.close()

    def test_multiple_records(self):
        """Insert two different records; both should be persisted."""
        from scraper.jurisdictions.tempe_permits import normalize_row

        rows = [
            {"PermitNum": "BLD-2024-TEST02", "OBJECTID": 20001, "PermitType": "Residential - New"},
            {"PermitNum": "BLD-2024-TEST03", "OBJECTID": 20002, "PermitType": "Commercial - Alteration"},
        ]

        session = get_session()
        for r in rows:
            norm = normalize_row(r)
            session.add(Permit(**norm))
        session.commit()

        total = session.execute(select(func.count()).select_from(Permit)).scalar() or 0
        self.assertEqual(total, 2)

        # Verify jurisdiction set correctly
        for r in session.execute(select(Permit)).scalars().all():
            self.assertEqual(r.jurisdiction, "City of Tempe")
            self.assertEqual(r.source_system, SOURCE_SYSTEM)

        session.close()

    def test_source_constraint(self):
        """Verify that source_system + source_record_id uniquely identify records."""
        from scraper.jurisdictions.tempe_permits import normalize_row

        row = {"PermitNum": "BLD-2024-TEST04", "OBJECTID": 30001, "PermitType": "Residential"}
        norm = normalize_row(row)

        session = get_session()
        p1 = Permit(**norm)
        session.add(p1)
        session.commit()

        # Same source_system + source_record_id should update, not create
        norm2 = normalize_row(row)
        existing = session.execute(
            select(Permit).where(
                Permit.source_system == SOURCE_SYSTEM,
                Permit.source_record_id == "30001",
            )
        ).scalar_one_or_none()
        self.assertIsNotNone(existing)
        self.assertEqual(existing.permit_number, "BLD-2024-TEST04")

        session.close()


# =====================================================================
# Filter behavior tests: zero-value categories in aggregate outputs
# =====================================================================

class TestPermitFilterAggregation(unittest.TestCase):
    """
    Test that selected categories with zero matching records still appear
    in by_category table and chart_cat_totals.

    These tests insert known permit data and verify the aggregation logic
    used by app.py's permits_index route.
    """

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        session = get_session()
        session.query(Permit).delete()
        session.commit()
        session.close()

    def _add_permit(self, session, **overrides):
        """Insert a single permit row with defaults."""
        defaults = dict(
            jurisdiction="City of Tempe",
            permit_issue_date="2026-03-15",
            normalized_category="Residential",
            work_type="New Construction",
            permit_number="TEST-0001",
            row_hash="hash1",
            report_adid="rpt001",
            report_date="2026-03-15",
            permit_valuation="100000",
            permit_square_feet="2000",
        )
        defaults.update(overrides)
        session.add(Permit(**defaults))
        session.commit()

    def _run_aggregate(self, categories_filter="", work_types_filter="",
                       jurisdiction_filter="", year_filter=""):
        """
        Run the same dedup + aggregation logic used by permits_index.
        Returns (by_category, chart_cat_totals, data_rows) where
        by_category is the list of dicts passed to the template.
        """
        from collections import defaultdict
        from sqlalchemy import text as _sa

        session = get_session()

        selected_cats = [c.strip() for c in categories_filter.split(",") if c.strip()]
        selected_wts = [w.strip() for w in work_types_filter.split(",") if w.strip()]

        # Build filter parts (same as _build_parts in app.py)
        parts = []
        params = {}
        if jurisdiction_filter:
            parts.append("p.jurisdiction = :jur")
            params["jur"] = jurisdiction_filter
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
        if year_filter:
            parts.append("p.permit_issue_date LIKE :yr")
            params["yr"] = f"{year_filter}%"
        where = " AND ".join(parts) if parts else "1=1"

        sql = _sa(f"""
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
            SELECT d.normalized_category AS cat,
                   CAST(NULLIF(d.permit_valuation, '') AS REAL) AS val,
                   CAST(NULLIF(d.permit_square_feet, '') AS REAL) AS sqft
            FROM deduped d
            WHERE d.rn = 1
              AND d.permit_issue_date IS NOT NULL
        """)

        cat_tot = defaultdict(lambda: {"count": 0, "sqft": 0.0, "val": 0.0})
        all_cats = set()

        for r in session.execute(sql, params).all():
            c = r.cat or "Other"
            v = r.val or 0.0
            s = r.sqft or 0.0
            all_cats.add(c)
            ct = cat_tot[c]
            ct["count"] += 1
            ct["sqft"] += s
            ct["val"] += v

        # Ensure selected categories appear even with zero records
        if selected_cats:
            for c in selected_cats:
                all_cats.add(c)
                if c not in cat_tot:
                    cat_tot[c] = {"count": 0, "sqft": 0.0, "val": 0.0}

        # Query all distinct categories for the table
        all_distinct = sorted(set(
            r[0] for r in session.execute(
                _sa("SELECT DISTINCT normalized_category FROM permits WHERE normalized_category IS NOT NULL")
            ).all()
        ))

        # by_category (same as app.py)
        by_category = sorted(
            [{"normalized_category": c,
              "count": cat_tot[c]["count"] if c in cat_tot else 0,
              "total_valuation": cat_tot[c]["val"] if c in cat_tot else 0,
              "total_sqft": cat_tot[c]["sqft"] if c in cat_tot else 0}
             for c in all_distinct],
            key=lambda r: r["count"], reverse=True,
        )

        # chart_cat_totals (same as app.py)
        cats_ordered = sorted(all_cats, key=lambda x: -cat_tot[x]["count"])
        chart_cat_totals = [
            {"category": c, "sqft": cat_tot[c]["sqft"],
             "valuation": cat_tot[c]["val"], "count": cat_tot[c]["count"]}
            for c in cats_ordered
        ]

        # Zero-categories diagnostic
        zero_cats = [c for c in selected_cats
                     if cat_tot.get(c, {}).get("count", 0) == 0]

        session.close()
        return by_category, chart_cat_totals, zero_cats

    # ── Tests ────────────────────────────────────────────────────────────

    def test_selected_categories_with_zero_records_in_table(self):
        """
        Selected categories exist in DB (with other work types) but have
        zero matching records for the work_type filter. They should still
        appear in by_category with count=0.
        """
        session = get_session()
        # Insert Residential (New Construction) and Commercial/Industrial
        # with DIFFERENT work types so they exist in DB but don't match filter
        self._add_permit(session, normalized_category="Residential",
                         work_type="New Construction")
        self._add_permit(session, normalized_category="Commercial",
                         work_type="Trade",
                         permit_number="TEST-0002", row_hash="hash2")
        self._add_permit(session, normalized_category="Industrial",
                         work_type="Demolition",
                         permit_number="TEST-0003", row_hash="hash3")
        session.close()

        by_category, chart_totals, zero_cats = self._run_aggregate(
            categories_filter="Commercial,Industrial,Residential",
            work_types_filter="New Construction",
        )

        # All selected categories should be present in by_category
        cat_names = [r["normalized_category"] for r in by_category]
        self.assertIn("Commercial", cat_names)
        self.assertIn("Industrial", cat_names)
        self.assertIn("Residential", cat_names)

        # Commercial and Industrial should have count=0 in filtered output
        for r in by_category:
            if r["normalized_category"] == "Commercial":
                self.assertEqual(r["count"], 0)
            if r["normalized_category"] == "Industrial":
                self.assertEqual(r["count"], 0)

    def test_selected_categories_with_zero_records_in_chart(self):
        """
        Selected categories exist in DB (with other work types) but have
        zero matching records for the work_type filter. They should still
        appear in chart_cat_totals with count=0.
        """
        session = get_session()
        self._add_permit(session, normalized_category="Residential",
                         work_type="New Construction")
        self._add_permit(session, normalized_category="Commercial",
                         work_type="Trade",
                         permit_number="TEST-0004", row_hash="hash4")
        self._add_permit(session, normalized_category="Industrial",
                         work_type="Demolition",
                         permit_number="TEST-0005", row_hash="hash5")
        session.close()

        by_category, chart_totals, zero_cats = self._run_aggregate(
            categories_filter="Commercial,Industrial,Residential",
            work_types_filter="New Construction",
        )

        chart_names = [r["category"] for r in chart_totals]
        self.assertIn("Commercial", chart_names)
        self.assertIn("Industrial", chart_names)
        self.assertIn("Residential", chart_names)

        for r in chart_totals:
            if r["category"] in ("Commercial", "Industrial"):
                self.assertEqual(r["count"], 0)

    def test_zero_categories_diagnostic(self):
        """zero_categories should list selected categories with 0 matching rows."""
        session = get_session()
        self._add_permit(session, normalized_category="Residential",
                         work_type="New Construction")
        self._add_permit(session, normalized_category="Commercial",
                         work_type="Trade",
                         permit_number="TEST-0006", row_hash="hash6")
        self._add_permit(session, normalized_category="Industrial",
                         work_type="Demolition",
                         permit_number="TEST-0007", row_hash="hash7")
        session.close()

        by_category, chart_totals, zero_cats = self._run_aggregate(
            categories_filter="Commercial,Industrial,Residential",
            work_types_filter="New Construction",
        )

        self.assertIn("Commercial", zero_cats)
        self.assertIn("Industrial", zero_cats)
        self.assertNotIn("Residential", zero_cats)

    def test_no_regression_unfiltered_aggregate(self):
        """
        Without filters, all categories present in data appear with
        correct non-zero counts and no phantom zero rows.
        """
        session = get_session()
        self._add_permit(session, normalized_category="Residential",
                         work_type="New Construction")
        self._add_permit(session, normalized_category="Commercial",
                         work_type="Alteration",
                         permit_number="TEST-0003", row_hash="hash3")
        self._add_permit(session, normalized_category="Industrial",
                         work_type="Trade",
                         permit_number="TEST-0004", row_hash="hash4")
        session.close()

        by_category, chart_totals, zero_cats = self._run_aggregate()

        # All three should have count >= 1
        for r in by_category:
            if r["normalized_category"] in ("Residential", "Commercial", "Industrial"):
                self.assertGreater(r["count"], 0, f"{r['normalized_category']} should have data")

        # No zero categories since no filters
        self.assertEqual(zero_cats, [])

    def test_diagnostic_aggregation_by_category_work_type(self):
        """Diagnostic: verify count by (category, work_type) matrix matches."""
        from sqlalchemy import text as _sa

        session = get_session()
        # Insert mixed data
        self._add_permit(session, normalized_category="Residential",
                         work_type="New Construction",
                         permit_number="R1", row_hash="hr1")
        self._add_permit(session, normalized_category="Residential",
                         work_type="Addition",
                         permit_number="R2", row_hash="hr2")
        self._add_permit(session, normalized_category="Commercial",
                         work_type="New Construction",
                         permit_number="C1", row_hash="hc1")
        self._add_permit(session, normalized_category="Industrial",
                         work_type="Trade",
                         permit_number="I1", row_hash="hi1")
        session.close()

        rows = session.execute(_sa("""
            SELECT normalized_category, work_type, COUNT(*) AS cnt
            FROM permits
            WHERE normalized_category IS NOT NULL
            GROUP BY normalized_category, work_type
            ORDER BY normalized_category, work_type
        """)).all()

        result_map = {}
        for r in rows:
            result_map[(r[0], r[1])] = r[2]

        self.assertEqual(result_map.get(("Residential", "New Construction")), 1)
        self.assertEqual(result_map.get(("Residential", "Addition")), 1)
        self.assertEqual(result_map.get(("Commercial", "New Construction")), 1)
        self.assertEqual(result_map.get(("Industrial", "Trade")), 1)
        self.assertEqual(len(rows), 4)

    def test_selected_only_category_no_work_type_filter(self):
        """
        When only categories are selected (no work_type filter),
        zero-category entries should still be in chart totals.
        """
        session = get_session()
        self._add_permit(session, normalized_category="Residential",
                         work_type="New Construction")
        self._add_permit(session, normalized_category="Commercial",
                         work_type="Alteration",
                         permit_number="T-005", row_hash="h5")
        session.close()

        by_category, chart_totals, zero_cats = self._run_aggregate(
            categories_filter="Residential,Commercial,Industrial,Mixed-Use",
        )

        # Industrial & Mixed-Use should be in charts with count=0
        for r in chart_totals:
            if r["category"] in ("Industrial", "Mixed-Use"):
                self.assertEqual(r["count"], 0,
                                 f"{r['category']} should have 0 count")

        zero_names = set(zero_cats)
        self.assertIn("Industrial", zero_names)
        self.assertIn("Mixed-Use", zero_names)


if __name__ == "__main__":
    unittest.main()
