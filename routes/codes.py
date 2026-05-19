"""Permit type code reference routes blueprint."""

import logging
import time
from typing import Optional

from flask import Blueprint, render_template, request, jsonify
from sqlalchemy import select, text as sa_text

from db import get_session, Permit
from routes import _cache

log = logging.getLogger(__name__)

codes_bp = Blueprint("codes", __name__, url_prefix="/permits")

# ── Permit Type Code Reference ────────────────────────────────────────────────
# The primary source for Phoenix is the PDD Online search page dropdown at:
#   https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit
# This is the canonical list of 483 codes with official descriptions.
# Secondary source: ArcGIS Planning_Permit MapServer (PER_TYPE_DESC) for older
# codes (RSF, RSME, etc.) not in the current PDD Online system.
# Last updated: 2026-05-17

_OFFICIAL_PHOENIX_CACHE: Optional[dict] = None  # {"permit_types": {...}, "structure_classes": {...}}
_OFFICIAL_FETCH_TIME: float = 0
_OFFICIAL_CACHE_TTL: int = 86400  # re-fetch every 24 hours


# Codes that belong to the structure-class dropdown (ddlStructureClass) on the
# PDD search page. These are numeric 001-997 series and a few alpha codes.
# Any code from this set is treated as a structure class, not a permit type.
_PHX_STRUCTURE_CLASS_CODES: set = set()


def _fetch_phoenix_official_codes() -> Optional[dict]:
    """Fetch the official Phoenix PDD codes from the PDD Online search page.

    The page at https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit has two
    dropdowns:
      - ddlPermitType (413 options): permit type codes (RVSN, LPRM, BLD, etc.)
      - ddlStructureClass (70 options): structure class codes (001-997 series)

    Returns a dict of {code: full_description} merging both lists.
    Codes from ddlStructureClass are noted in their description.
    """
    global _OFFICIAL_PHOENIX_CACHE, _OFFICIAL_FETCH_TIME, _PHX_STRUCTURE_CLASS_CODES
    now = time.time()
    if _OFFICIAL_PHOENIX_CACHE is not None and (now - _OFFICIAL_FETCH_TIME) < _OFFICIAL_CACHE_TTL:
        return _OFFICIAL_PHOENIX_CACHE

    try:
        import urllib.request, re
        req = urllib.request.Request(
            "https://apps-secure.phoenix.gov/PDD/Search/IssuedPermit",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8")

        # Parse structure class dropdown (ddlStructureClass)
        structure_classes = {}
        sc_match = re.search(
            r'<select[^>]*id="ddlStructureClass"[^>]*>(.*?)</select>',
            html, re.DOTALL
        )
        if sc_match:
            for m in re.finditer(r'<option[^>]*value="([^"]+)"[^>]*>([^<]+)</option>', sc_match.group(1)):
                code = m.group(1).strip()
                if not code or code.startswith("-ALL-"):
                    continue
                label = m.group(2).strip()
                if " - " in label:
                    desc = label.split(" - ", 1)[1].strip()
                else:
                    desc = label
                structure_classes[code] = desc

        # Parse permit type dropdown (ddlPermitType)
        permit_types = {}
        pt_match = re.search(
            r'<select[^>]*id="ddlPermitType"[^>]*>(.*?)</select>',
            html, re.DOTALL
        )
        if pt_match:
            for m in re.finditer(r'<option[^>]*value="([^"]+)"[^>]*>([^<]+)</option>', pt_match.group(1)):
                code = m.group(1).strip()
                if not code or code.startswith("-ALL-"):
                    continue
                label = m.group(2).strip()
                if " - " in label:
                    desc = label.split(" - ", 1)[1].strip()
                else:
                    desc = label
                permit_types[code] = desc

        # Merge: permit types first, then structure classes as a separate set
        all_codes = dict(permit_types)
        all_codes.update(structure_classes)

        # Store the structure class set for downstream use
        _PHX_STRUCTURE_CLASS_CODES = set(structure_classes.keys())

        _OFFICIAL_PHOENIX_CACHE = all_codes
        _OFFICIAL_FETCH_TIME = now
        log.info(
            f"Fetched Phoenix codes: {len(permit_types)} permit types, "
            f"{len(structure_classes)} structure classes"
        )
        return all_codes
    except Exception as e:
        log.warning(f"Failed to fetch official Phoenix codes: {e}")
        return _OFFICIAL_PHOENIX_CACHE or {}


# This old hard-coded reference is now replaced by the live fetch above.
# It is kept only for offline/fallback scenarios.
_PHOENIX_FALLBACK_TYPE_CODES = {
    "RSF": {
        "full_name": "RES STRUC",
        "category": "Residential",
        "work_type": "New Construction",
        "scope": "RETAINING WALL",
        "description": "Residential structural permit (e.g. retaining wall)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RSFC": {
        "full_name": "RESIDENTIAL SINGLE FAMILY - SELF CERT",
        "category": "Residential",
        "work_type": "New Construction",
        "scope": "CUSTOM RESIDENCE",
        "description": "Single-family home via self-certification path",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RSC": {
        "full_name": "RESIDENTIAL PERMIT - SELF CERT",
        "category": "Residential",
        "work_type": "New Construction",
        "scope": "ADDITION TO EXISTING RESIDENCE",
        "description": "Residential self-certification permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RSME": {
        "full_name": "RES STRUC/MECH OR PLMB/ELEC",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "ADDITION TO EXISTING RESIDENCE",
        "description": "Residential structural/mechanical/plumbing/electrical alteration on an existing home. Also used for fire rehabilitation remodels.",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC; Phoenix.gov inspections page",
        "verified": True,
    },
    "RSP": {
        "full_name": None,
        "category": "Residential",
        "work_type": "New Construction",
        "scope": None,
        "description": "Likely single-family patio home (zero-lot-line product)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "RM": {
        "full_name": None,
        "category": "Residential",
        "work_type": "New Construction",
        "scope": None,
        "description": "Residential multifamily — apartments, condos, townhomes",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "RPV": {
        "full_name": "RES PHOTOVOLTAIC SYSTEM",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "PHOTOVOLTAIC SYSTEM",
        "description": "Residential solar panel installation",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RE": {
        "full_name": "RES ELEC",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "PHOTOVOLTAIC SYSTEM",
        "description": "Residential electrical permit (often PV solar)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "REM": {
        "full_name": "RES ELEC/MECH OR PLMB",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "RESIDENTIAL MISCELLANEOUS",
        "description": "Residential electrical/mechanical/plumbing miscellaneous work",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "RDEM": {
        "full_name": "RES DEMOLITION",
        "category": "Residential",
        "work_type": "Demolition",
        "scope": "RES AS-BUILT NON-PERMITTED CONSTRUCTION",
        "description": "Residential demolition",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── RV* prefix — STATUS: LIKELY MISCLASSIFIED ──
    "RVSN": {
        "full_name": None,
        "category": "Commercial (inferred)",
        "work_type": "Trade (inferred)",
        "scope": None,
        "description": "⚠️ Likely NOT residential. All known instances are fire-sprinkler permits at TSMC semiconductor fab (32200 N 43RD AVE). Needs official definition.",
        "source": "PDD CSV sample data — not present in ArcGIS",
        "verified": False,
    },
    "RVSX": {
        "full_name": None,
        "category": None,
        "work_type": None,
        "scope": None,
        "description": "⚠️ Unknown code — same RV prefix as RVSN; may not be residential",
        "source": "PDD CSV export only",
        "verified": False,
    },
    "RVSC": {
        "full_name": None,
        "category": None,
        "work_type": None,
        "scope": None,
        "description": "⚠️ Unknown code — same RV prefix as RVSN; may not be residential",
        "source": "PDD CSV export only",
        "verified": False,
    },
    "RVCA": {
        "full_name": None,
        "category": None,
        "work_type": None,
        "scope": None,
        "description": "⚠️ Unknown code — same RV prefix as RVSN; may not be residential",
        "source": "PDD CSV export only",
        "verified": False,
    },
    # ── Commercial / Building codes ──
    "BLD": {
        "full_name": "STRUC/ELEC/PLMB/MECH",
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": "COMMERCIAL REMODEL",
        "description": "General commercial building permit (structural/electrical/plumbing/mechanical)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "BLDS": {
        "full_name": "SHELL - STRUC/ELEC/PLMB/MECH",
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": "COMMERCIAL NEW",
        "description": "Commercial shell building (new construction)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "OBLD": {
        "full_name": "OTC STRUC/ELEC/PLMB/MECH",
        "category": "Other",
        "work_type": "Alteration",
        "scope": "COMMERCIAL REMODEL",
        "description": "Over-the-counter commercial building permit (small remodel)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── Sign permits ──
    "SGNP": {
        "full_name": "SIGN PERMIT",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": "COMMERCIAL SIGN APPLICATON REVIEW",
        "description": "Permanent sign permit (SGN = Sign, P = Permanent)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "SGNT": {
        "full_name": "SIGN TEMPORARY PERMIT",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": "TEMPORARY SIGN",
        "description": "Temporary sign permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "SGNV": {
        "full_name": "SIGN VIOLATION",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": "SIGN INSPECTION",
        "description": "Sign violation inspection",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── Land-use permits ──
    "LPRM": {
        "full_name": None,
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": None,
        "description": "Land-Use Permit, Plan Review (LP=Land-Use, RM=Review Major, hypothesized)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "LPRN": {
        "full_name": None,
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": None,
        "description": "Land-Use Permit, New (LP=Land-Use, RN=New, hypothesized)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    "LPRS": {
        "full_name": None,
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": None,
        "description": "Land-Use Permit, Site Plan (LP=Land-Use, RS=Site, hypothesized)",
        "source": "Convention — no ArcGIS description available",
        "verified": False,
    },
    # ── Fire / Safety ──
    "FPSR": {
        "full_name": "FIRE PREVENTION SERVICE REQUEST",
        "category": "Commercial",
        "work_type": "Trade",
        "scope": None,
        "description": "Fire prevention service request / inspection",
        "source": "ArcGIS PER_TYPE_DESC",
        "verified": True,
    },
    # ── Demolition ──
    "DEM": {
        "full_name": "DEMOLITION",
        "category": "Demolition",
        "work_type": "Demolition",
        "scope": "DEMO PERMIT ONLY",
        "description": "Standard demolition permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    # ── Other ──
    "EXTR": {
        "full_name": "EXTENDED CONSTRUCTION WORK HOURS RENEWAL",
        "category": "Other",
        "work_type": "Alteration",
        "scope": "EXTENDED CONSTRUCTION WORK HOURS",
        "description": "Extended work hours permit (construction noise variance)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "ERES": {
        "full_name": "RESIDENTIAL ELEVATOR",
        "category": "Residential",
        "work_type": "Alteration",
        "scope": "PRIVATE RESIDENTIAL ELEVATOR--ELEVINSP",
        "description": "Private residential elevator installation",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "SHOR": {
        "full_name": "SHORING PERMIT",
        "category": "Commercial",
        "work_type": "New Construction",
        "scope": "SHORING",
        "description": "Excavation shoring permit",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
    "BMR": {
        "full_name": "BUILDING MAINTENANCE REGISTRATION",
        "category": "Commercial",
        "work_type": "Alteration",
        "scope": "BUILDING MAINTENANCE REGISTRATION",
        "description": "Building maintenance registration (annual program)",
        "source": "ArcGIS PER_TYPE_DESC + SCOPE_DESC",
        "verified": True,
    },
}


def _get_type_codes_for_jurisdiction(jurisdiction: str) -> dict:
    """Return type code reference for a given jurisdiction.

    For Phoenix, the primary source is the official PDD Online search page
    dropdown (483 codes). Falls back to permit data for codes not in that list.
    Returns a dict of {code: info} suitable for JSON serialization.
    """
    # Import Phoenix permit type helpers
    # On development, this lives at scripts/scraper/phoenix_permits.py.
    # On production (older deployments), it may be at scraper/phoenix_permits.py
    # due to directory structure differences. Handle both.
    import sys as _sys
    from pathlib import Path as _Path
    _scraper_dir = _Path(__file__).resolve().parent.parent / "scraper"
    if str(_scraper_dir) not in _sys.path:
        _sys.path.insert(0, str(_scraper_dir))
    try:
        from phoenix_permits import (
            PHX_CATEGORY_MAP, PHX_WORK_TYPE_MAP, categorize_phoenix_type,
        )
    except ImportError:
        # Fallback: try scripts.scraper.phoenix_permits
        from scripts.scraper.phoenix_permits import (
            PHX_CATEGORY_MAP, PHX_WORK_TYPE_MAP, categorize_phoenix_type,
        )
    jur_lower = jurisdiction.lower().strip() if jurisdiction else ""

    if "phoenix" in jur_lower:
        official = _fetch_phoenix_official_codes()
        result = {}

        # Classify: figure out which codes are structure classes vs permit types
        is_structure_class = _PHX_STRUCTURE_CLASS_CODES

        # First pass: all codes from the official PDD Online dropdown
        for code, desc in official.items():
            cat, wt = categorize_phoenix_type(code)
            if code in is_structure_class:
                # Structure class codes (001-997) describe the building type,
                # not the permit type. They're used alongside permit types.
                result[code] = {
                    "full_name": desc,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": f"Structure class: {desc}. Used alongside a permit type code (e.g. BLD + structure class 330) to describe what kind of building the work is on.",
                    "source": "Phoenix PDD Online (official) — ddlStructureClass dropdown",
                    "verified": True,
                    "is_structure_class": True,
                }
            else:
                result[code] = {
                    "full_name": desc,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": desc,
                    "source": "Phoenix PDD Online (official)",
                    "verified": True,
                    "is_structure_class": False,
                }

        # Second pass: codes in permit data not in the official list
        # (older ArcGIS codes like RSF, RSME, etc.)
        session = get_session()
        from sqlalchemy import text
        known_types = session.execute(
            text("SELECT DISTINCT native_type FROM permits WHERE jurisdiction = :j AND native_type IS NOT NULL"),
            {"j": jurisdiction},
        ).scalars().all()
        session.close()

        for t in known_types:
            if t and t not in result:
                cat, wt = categorize_phoenix_type(t)
                result[t] = {
                    "full_name": None,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": f"Code from ArcGIS permit data — not in current PDD Online system",
                    "source": "ArcGIS permit data",
                    "verified": False,
                }

        return result

    if "tempe" in jur_lower:
        # Tempe uses Accela-based classification codes in raw_permit_class.
        # These are already descriptive labels like "330 - Commercial Buildings".
        from scripts.scraper.tempe_permits import categorize_permit, classify_work_type
        session = get_session()
        from sqlalchemy import text
        known_classes = session.execute(
            text("SELECT DISTINCT raw_permit_class FROM permits WHERE jurisdiction = :j AND raw_permit_class IS NOT NULL AND raw_permit_class != '' ORDER BY raw_permit_class"),
            {"j": jurisdiction},
        ).scalars().all()
        session.close()

        result = {}
        for pc in known_classes:
            if pc:
                cat = categorize_permit(raw_permit_class=pc)
                wt = classify_work_type(raw_permit_class=pc)
                result[pc] = {
                    "full_name": pc,
                    "category": cat,
                    "work_type": wt,
                    "scope": None,
                    "description": pc,
                    "source": "Tempe Accela Civic Platform (raw_permit_class)",
                    "verified": True,
                }
        return result

    # Generic fallback for other jurisdictions
    import re
    session = get_session()
    from sqlalchemy import text
    known_types = session.execute(
        text("SELECT DISTINCT native_type FROM permits WHERE jurisdiction = :j AND native_type IS NOT NULL"),
        {"j": jurisdiction},
    ).scalars().all()
    session.close()

    result = {}
    for t in known_types:
        if t:
            # Check if the type is a descriptive label (contains spaces, parentheses,
            # mixed case) vs. a short code (all-caps, 2-8 chars, no spaces).
            # Descriptive labels ARE the full name already.
            is_short_code = bool(
                t.isupper() and len(t) <= 10 and " " not in t and "(" not in t
            )
            if is_short_code:
                result[t] = {
                    "full_name": None,
                    "category": None,
                    "work_type": None,
                    "scope": None,
                    "description": "Type code in use — no verified definition available",
                    "source": "From data",
                    "verified": False,
                }
            else:
                result[t] = {
                    "full_name": t,
                    "category": None,
                    "work_type": None,
                    "scope": None,
                    "description": t,
                    "source": "Self-describing label — this IS the full name",
                    "verified": True,
                }
    return result


@codes_bp.route("/type-codes")
def permit_type_codes():
    """Reference page showing all permit type codes for a jurisdiction."""
    jurisdiction = request.args.get("jurisdiction", "").strip()
    code_filter = request.args.get("code", "").strip().upper()

    codes = _get_type_codes_for_jurisdiction(jurisdiction) if jurisdiction else {}

    # Always pass the full list — code_filter just sets an anchor target
    # for scrolling to that specific row
    anchor_code = code_filter if code_filter in codes else None

    # Sort: verified first, then alphabetical
    sorted_codes = sorted(codes.items(), key=lambda kv: (not kv[1]["verified"], kv[0]))

    # Get all jurisdictions for the dropdown (always, even when filtered)
    session = get_session()
    known_jurisdictions = [
        j for j in session.execute(
            select(Permit.jurisdiction).distinct().where(Permit.jurisdiction.isnot(None)).order_by(Permit.jurisdiction)
        ).scalars().all()
    ]
    session.close()

    return render_template(
        "permit_type_codes.html",
        codes=sorted_codes,
        jurisdiction=jurisdiction,
        anchor_code=anchor_code,
        jurisdictions=known_jurisdictions,
    )


@codes_bp.route("/api/type-codes")
def permit_type_codes_api():
    """JSON endpoint returning type code reference for a jurisdiction."""
    jurisdiction = request.args.get("jurisdiction", "").strip()
    code_filter = request.args.get("code", "").strip().upper()

    codes = _get_type_codes_for_jurisdiction(jurisdiction) if jurisdiction else {}

    if code_filter and code_filter in codes:
        codes = {code_filter: codes[code_filter]}

    return jsonify(codes)


if __name__ == "__main__":
    app.run(debug=True)
