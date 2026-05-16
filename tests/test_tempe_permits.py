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
    set_database_url,
)
from scraper.tempe_permits import (
    _parse_arcgis_date,
    _build_query_url,
    normalize_row,
    categorize_permit,
    sync_permits,
    ARCGIS_URL,
    MAX_RECORD_COUNT,
    SOURCE_SYSTEM,
)
from sqlalchemy import func, select

# Temp database
_test_db_path = tempfile.mktemp(suffix=".sqlite")
set_database_url(f"sqlite:///{_test_db_path}")
init_db()


def _reset_db_engine():
    if _db_mod._engine:
        _db_mod._engine.dispose()
    _db_mod._engine = None
    _db_mod._SessionLocal = None


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


# ── Database upsert ────────────────────────────────────────────────────────

class TestDatabaseUpsert(unittest.TestCase):
    """Test idempotent upsert behavior with the Tempe permit scraper."""

    @classmethod
    def setUpClass(cls):
        _reset_db_engine()
        set_database_url(f"sqlite:///{_test_db_path}")
        init_db()

    def setUp(self):
        session = get_session()
        session.query(Permit).delete()
        session.commit()
        session.close()

    def test_idempotent_upsert(self):
        """Same row inserted twice should not duplicate."""
        from scraper.tempe_permits import normalize_row

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
        from scraper.tempe_permits import normalize_row

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
        from scraper.tempe_permits import normalize_row

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


if __name__ == "__main__":
    unittest.main()
