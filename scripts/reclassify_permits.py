#!/usr/bin/env python3
"""
Re-classify existing permit records with updated categorization logic.

Run after changing classify_phoenix_type() / categorize_permit() in the scrapers
to bring existing database records in line with the new logic.

Usage:
    .venv/bin/python3 scripts/reclassify_permits.py [--dry-run]

The script applies targeted updates:
  - RSME: work_type = "New Construction" → "Alteration"
  - RVSN/RVSX/RVSC/RVCA/RPDR: category = "Residential" → "Plan Review", 
                                work_type = "New Construction" → "Plan Review"
  - Tempe UF (Underground Fire): category = "Other" → "Trade"
  - Tempe RAE (Engineering Revisions): category = "Other" → "Trade"
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Ensure scripts/ is on the path
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from db import get_session, set_database_url
from scripts.scraper.phoenix_permits import categorize_phoenix_type
from scripts.scraper.tempe_permits import categorize_permit, classify_work_type


DRY_RUN = "--dry-run" in sys.argv


def fix_phoenix_records(session):
    """Re-classify Phoenix RSME and RV*/RPDR records."""
    from db import Permit

    # ── RSME: New Construction → Alteration ──
    rsme_q = session.query(Permit).filter(
        Permit.jurisdiction == "City of Phoenix",
        Permit.native_type == "RSME",
        Permit.work_type != "Alteration",
    )
    rsme_count = rsme_q.count()
    if rsme_count:
        log.info("Phoenix RSME: %d records to fix (New Construction → Alteration)", rsme_count)
        if not DRY_RUN:
            rsme_q.update({"work_type": "Alteration"}, synchronize_session=False)

    # ── RVSN/RVSX/RVSC/RVCA: Residential/New Construction → Plan Review/Plan Review ──
    rv_codes = ["RVSN", "RVSX", "RVSC", "RVCA", "RPDR"]
    for code in rv_codes:
        rv_q = session.query(Permit).filter(
            Permit.jurisdiction == "City of Phoenix",
            Permit.native_type == code,
        )
        total = rv_q.count()
        wrong_cat = rv_q.filter(Permit.normalized_category != "Plan Review").count()
        wrong_wt = rv_q.filter(Permit.work_type != "Plan Review").count()
        if wrong_cat or wrong_wt:
            log.info("Phoenix %s: %d records (cat=%d wrong, wt=%d wrong)", code, total, wrong_cat, wrong_wt)
            if not DRY_RUN:
                rv_q.update({
                    "normalized_category": "Plan Review",
                    "work_type": "Plan Review",
                }, synchronize_session=False)


def fix_maricopa_county_records(session):
    """Backfill work_type for Maricopa County from work_class."""
    from db import Permit
    from scripts.permit_scraper import _classify_work_type_from_work_class

    # Get all distinct work_class values and their current work_type
    from sqlalchemy import func
    classes = session.query(
        Permit.work_class, Permit.work_type, func.count(Permit.id)
    ).filter(
        Permit.jurisdiction == "Maricopa County",
    ).group_by(Permit.work_class, Permit.work_type).all()

    fixed_any = False
    for wc, current_wt, cnt in classes:
        expected_wt = _classify_work_type_from_work_class(wc)
        if current_wt is None or current_wt != expected_wt:
            log.info(
                "Maricopa work_class=%s: %d records work_type %s → %s",
                str(wc)[:40], cnt, current_wt or "NULL", expected_wt
            )
            if not DRY_RUN:
                session.query(Permit).filter(
                    Permit.jurisdiction == "Maricopa County",
                    Permit.work_class == wc,
                ).update({"work_type": expected_wt}, synchronize_session=False)
            fixed_any = True

    if not fixed_any:
        log.info("Maricopa County: no work_type reclassification needed")


def fix_tempe_records(session):
    """Re-classify Tempe records where the existing categorization is wrong."""
    from db import Permit

    # Get all distinct raw_permit_class values and re-categorize
    classes = session.query(Permit.raw_permit_class).filter(
        Permit.jurisdiction == "City of Tempe",
        Permit.raw_permit_class.isnot(None),
        Permit.raw_permit_class != "",
    ).distinct().all()

    fixed_any = False
    for (pclass,) in classes:
        new_cat = categorize_permit(raw_permit_class=pclass)
        new_wt = classify_work_type(raw_permit_class=pclass)

        if new_cat is None:
            continue

        # Find records that don't match
        q = session.query(Permit).filter(
            Permit.jurisdiction == "City of Tempe",
            Permit.raw_permit_class == pclass,
        )
        mismatches = q.filter(
            (Permit.normalized_category != new_cat) | (Permit.work_type != new_wt)
        )
        count = mismatches.count()
        if count:
            log.info("Tempe %s: %d records → cat=%s wt=%s", pclass[:40], count, new_cat, new_wt)
            if not DRY_RUN:
                mismatches.update({
                    "normalized_category": new_cat,
                    "work_type": new_wt,
                }, synchronize_session=False)
            fixed_any = True

    if not fixed_any:
        log.info("Tempe: no reclassification needed")


def main():
    log.info("Starting reclassification%s", " (DRY RUN)" if DRY_RUN else "")
    session = get_session()

    try:
        fix_phoenix_records(session)
        fix_maricopa_county_records(session)
        fix_tempe_records(session)
        if not DRY_RUN:
            session.commit()
            # Bump the Flask cache version so cached pages are invalidated
            _bump_cache_version()
            log.info("Committed. All records updated and cache invalidated.")
        else:
            log.info("Dry run — no changes made. Re-run without --dry-run to apply.")
    except Exception:
        session.rollback()
        log.exception("Reclassification failed, rolled back")
        sys.exit(1)
    finally:
        session.close()


def _bump_cache_version():
    """Increment _CACHE_VERSION in app.py so Flask pages re-render fresh."""
    import re
    app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
    with open(app_path, "r") as f:
        content = f.read()
    new_content = re.sub(
        r'_CACHE_VERSION = "v(\d+)"',
        lambda m: f'_CACHE_VERSION = "v{int(m.group(1)) + 1}"',
        content
    )
    if new_content != content:
        with open(app_path, "w") as f:
            f.write(new_content)
        log.info("Bumped _CACHE_VERSION in app.py")


if __name__ == "__main__":
    main()
