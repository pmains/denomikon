#!/usr/bin/env python3
"""
Classify all public_bodies by body_type for consistent badge rendering.

Body type categories:
  primary          — City Council, Town Council, Board of Supervisors (governing body)
  land_use         — Planning & Zoning, DRC, BOA, HPC, Design Review, Building Code
  community_services — Health, housing, human services, community action, domestic violence
  fiscal_oversight — Audit, PSPRS, risk, benefits, budget, workers comp, savings
  culture_recreation — Arts, museums, parks, libraries, cultural foundations, youth, stadium
  infrastructure   — Transportation, flood control, airport, water, drainage
  advisory_general — General advisory, neighborhood, economic dev, disabilities, nominations,
                     judicial selection, citizen review

Badge colors by type:
  primary → bg-primary (blue)
  land_use → bg-secondary (orange)
  community_services → bg-success (green)
  fiscal_oversight → bg-danger (red)
  culture_recreation → bg-info (teal)
  infrastructure → bg-warning (yellow)
  advisory_general → bg-dark (gray)

USAGE:
  python scripts/classify_bodies.py
"""

import sqlite3
import sys
import os


BODY_TYPE_CLASSIFICATION = {
    # ── Maricopa County ──
    "bos": "primary",
    "adj": "land_use",
    "pz": "land_use",
    "drain": "infrastructure",
    "health": "community_services",
    "tab": "infrastructure",
    "ida": "fiscal_oversight",
    "mc-audit": "fiscal_oversight",
    "mc-benefit-trust": "fiscal_oversight",
    "mc-community-action": "community_services",
    "mc-cdac": "community_services",
    "mc-eed-policy": "community_services",
    "mc-flood-advisory": "infrastructure",
    "mc-home": "community_services",
    "mc-mclepc": "advisory_general",
    "mc-mcao-psprs": "fiscal_oversight",
    "mc-mcso-corp": "fiscal_oversight",
    "mc-mcso-psprs": "fiscal_oversight",
    "mc-merit": "fiscal_oversight",
    "mc-psfc": "fiscal_oversight",
    "mc-risk-trust": "fiscal_oversight",
    "mc-smart-savings": "fiscal_oversight",
    "mc-stadium": "culture_recreation",
    "mc-trp": "infrastructure",
    "mc-air-pollution": "advisory_general",
    "mc-bcab": "land_use",
    "mc-flood-stakeholder": "infrastructure",

    # ── Tempe ──
    "tempe-cc": "primary",
    "tempe-drc": "land_use",
    "tempe-boa": "land_use",
    "tempe-hpc": "land_use",
    "tempe-ha": "community_services",
    "tempe-rio": "infrastructure",
    "tempe-rmt": "fiscal_oversight",
    "tempe-jrc": "land_use",

    # ── Chandler ──
    "chandler-cc": "primary",
    "chandler-pz": "land_use",
    "chandler-drc": "land_use",
    "chandler-boa": "land_use",
    "chandler-hpc": "land_use",
    "chandler-ida": "fiscal_oversight",
    "chandler-prb": "culture_recreation",
    "chandler-lb": "culture_recreation",
    "chandler-mf": "culture_recreation",
    "chandler-cf": "culture_recreation",
    "chandler-arts": "culture_recreation",
    "chandler-tc": "infrastructure",
    "chandler-mvc": "advisory_general",
    "chandler-hhsc": "community_services",
    "chandler-hrc": "advisory_general",
    "chandler-dvc": "community_services",
    "chandler-pha": "community_services",
    "chandler-nac": "advisory_general",
    "chandler-yc": "advisory_general",
    "chandler-pdc": "advisory_general",
    "chandler-eda": "advisory_general",
    "chandler-psprs-f": "fiscal_oversight",
    "chandler-psprs-p": "fiscal_oversight",
    "chandler-hcc": "community_services",
    "chandler-cpr": "advisory_general",
    "chandler-hct": "fiscal_oversight",
    "chandler-wct": "fiscal_oversight",
    "chandler-air": "infrastructure",

    # ── Phoenix ──
    "phoenix-cc": "primary",
    "phoenix-pc": "land_use",
    "phoenix-boa": "land_use",
    "phoenix-vpc": "land_use",
    "phoenix-ti": "infrastructure",
    "phoenix-ps": "advisory_general",
    "phoenix-ed": "advisory_general",
    "phoenix-cs": "community_services",
    "phoenix-bh": "fiscal_oversight",
    "phoenix-sp": "advisory_general",

    # ── Mesa ──
    "mesa-cc": "primary",
    "mesa-pz": "land_use",
    "mesa-drb": "land_use",
    "mesa-boa": "land_use",
    "mesa-hpb": "land_use",
    "mesa-cadence": "infrastructure",
    "mesa-eastmark1": "infrastructure",
    "mesa-eastmark2": "infrastructure",

    # ── Gilbert ──
    "gilbert-tc": "primary",
    "gilbert-red": "advisory_general",
    "gilbert-pf": "fiscal_oversight",
    "gilbert-water": "infrastructure",

    # ── Scottsdale ──
    "scottsdale-cc": "primary",
    "scottsdale-pc": "land_use",
    "scottsdale-boa": "land_use",
    "scottsdale-drb": "land_use",
    "scottsdale-hpc": "land_use",
    "scottsdale-baba": "land_use",

    # ── Glendale ──
    "glendale-cc": "primary",

    # ── Peoria ──
    "peoria-cc": "primary",
    "peoria-pz": "land_use",
    "peoria-boa": "land_use",
    "peoria-sub": "advisory_general",

    # ── Surprise ──
    "surprise-cc": "primary",
    "surprise-pz": "land_use",
    "surprise-planning-zoning": "land_use",
    "surprise-arts": "culture_recreation",
    "surprise-nominations": "advisory_general",
    "surprise-audit": "fiscal_oversight",
    "surprise-health-benefits": "fiscal_oversight",
    "surprise-judicial-selection": "advisory_general",
    "surprise-library": "culture_recreation",
    "surprise-psprs-fire": "fiscal_oversight",
    "surprise-psprs-police": "fiscal_oversight",
    "surprise-parks": "culture_recreation",
    "surprise-tourism": "culture_recreation",
    "surprise-veterans": "advisory_general",

    # ── Buckeye ──
    "buckeye-cc": "primary",
    "buckeye-pz": "land_use",
    "buckeye-airport": "infrastructure",
    "buckeye-arts-culture": "culture_recreation",
    "buckeye-community-services": "community_services",
    "buckeye-cfd": "infrastructure",
    "buckeye-library": "culture_recreation",
    "buckeye-pollution-control": "advisory_general",
    "buckeye-psprs-fire": "fiscal_oversight",
    "buckeye-psprs-police": "fiscal_oversight",
    "buckeye-youth": "advisory_general",
    "buckeye-water-rate": "infrastructure",

    # ── Avondale ──
    "avondale-cc": "primary",
    "avondale-pz": "land_use",
    "avondale-boa": "land_use",
    "avondale-arts": "culture_recreation",
    "avondale-audit": "fiscal_oversight",
    "avondale-benefits": "fiscal_oversight",
    "avondale-cfd": "infrastructure",
    "avondale-judicial": "advisory_general",
    "avondale-neighborhood": "advisory_general",
    "avondale-psprs": "fiscal_oversight",
    "avondale-parks": "culture_recreation",
    "avondale-risk": "fiscal_oversight",
    "avondale-sustainability": "advisory_general",
    "avondale-quorum": "primary",
    "avondale-library": "culture_recreation",
    "avondale-hpc": "land_use",

    # ── El Mirage ──
    "el-mirage-cc": "primary",
    "el-mirage-pz": "land_use",
    "el-mirage-boa": "land_use",

    # ── Goodyear ──
    "goodyear-cc": "primary",
    "goodyear-pz": "land_use",

    # ── Paradise Valley ──
    "paradise-valley-boa": "land_use",
    "paradise-valley-pc": "land_use",

    # ── Queen Creek ──
    "queen-creek-cc": "primary",
}


BADGE_COLORS = {
    "primary": "primary",
    "land_use": "secondary",
    "community_services": "success",
    "fiscal_oversight": "danger",
    "culture_recreation": "info",
    "infrastructure": "warning",
    "advisory_general": "dark",
}


def get_conn():
    db_path = os.environ.get("DATABASE_URL", "sqlite:///data/maricopa.sqlite")
    db_path = db_path.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    return conn


def verify(conn):
    """Show current state."""
    cur = conn.execute("SELECT body_type, COUNT(*) FROM public_bodies GROUP BY body_type ORDER BY COUNT(*) DESC")
    print("Current body_type distribution:")
    for r in cur.fetchall():
        bt = r[0] or "NULL"
        print(f"  {bt:25s}: {r[1]}")

    # Show bodies NOT in our classification
    cur = conn.execute("SELECT body_code, name, body_type FROM public_bodies ORDER BY body_code")
    missing = []
    for code, name, bt in cur:
        if code not in BODY_TYPE_CLASSIFICATION:
            missing.append(f"  {code:30s} {name or '?':40s} current_type={bt}")
    if missing:
        print(f"\n⚠️  {len(missing)} bodies not yet classified:")
        for m in missing[:20]:
            print(m)
        if len(missing) > 20:
            print(f"  ... and {len(missing)-20} more")
    else:
        print("\n✅ All bodies classified")


def apply(conn):
    """Update all public_bodies with classified body_type."""
    updated = 0
    for code, btype in BODY_TYPE_CLASSIFICATION.items():
        cur = conn.execute("SELECT body_type FROM public_bodies WHERE body_code = ?", (code,))
        existing = cur.fetchone()
        if existing:
            conn.execute("UPDATE public_bodies SET body_type = ? WHERE body_code = ?", (btype, code))
            updated += 1

    conn.commit()
    print(f"Classified {updated} bodies")

    # Show new distribution
    cur = conn.execute("SELECT body_type, COUNT(*) FROM public_bodies GROUP BY body_type ORDER BY COUNT(*) DESC")
    print("\nNew body_type distribution:")
    for r in cur.fetchall():
        print(f"  {r[0]:25s}: {r[1]}")


def main():
    verify_only = "--verify" in sys.argv
    conn = get_conn()

    if verify_only:
        verify(conn)
    else:
        apply(conn)
        print()
        verify(conn)

    conn.close()


if __name__ == "__main__":
    main()
