"""Housing Hearing Finder — find upcoming public hearings on housing development.

Usage:
    agenda_scraper.py hearings [--days=30] [--jurisdiction=CITY] [--json]

Scans upcoming meetings for agenda items related to housing development
and flags which are public hearings where you can attend and speak.

Part of the YIMBY Maricopa toolkit.

Examples:
    agenda_scraper.py hearings --days=14
    agenda_scraper.py hearings --jurisdiction=tempe
    agenda_scraper.py hearings --jurisdiction=mesa --json
"""
from __future__ import annotations

import json as _json
import sys
from datetime import date, timedelta
from typing import Optional

HOUSING_KEYWORDS = [
    'apartment', 'multi-family', 'multifamily', 'residential', 'rezon',
    'subdivision', 'dwelling', 'housing', 'affordable',
    'lot', 'unit', 'townhome', 'townhouse', 'single-family', 'PAD',
]

JURISDICTIONS = {
    'maricopa-county': 'Maricopa County', 'tempe': 'Tempe',
    'chandler': 'Chandler', 'mesa': 'Mesa', 'phoenix': 'Phoenix',
    'scottsdale': 'Scottsdale', 'glendale': 'Glendale', 'peoria': 'Peoria',
    'surprise': 'Surprise', 'buckeye': 'Buckeye', 'gilbert': 'Gilbert',
    'avondale': 'Avondale', 'goodyear': 'Goodyear', 'el-mirage': 'El Mirage',
}

HEARING_BODIES = {
    'pz': 'Planning & Zoning Commission — where most rezoning hearings happen',
    'drc': 'Development Review Commission — development applications',
    'boa': 'Board of Adjustment — variances and special use permits',
    'bza': 'Board of Zoning Adjustment',
    'hpc': 'Historic Preservation Commission',
}

HEARING_BODY_SUFFIXES = ['-pz', '-drc', '-boa', '-bza', '-hpc']


def classify(text: str) -> str:
    up = text.upper()
    if 'CALL FOR' in up and 'HEARING' in up:
        return 'CALL'
    if ('SCHEDULE' in up or 'SET' in up) and 'HEARING' in up:
        return 'CALL'
    if 'PUBLIC HEARING' in up:
        return 'HEARING'
    if 'HEARING' in up:
        return 'HEARING'
    if 'ORDINANCE' in up and 'INTRODUCE' in up:
        return 'HEARING'
    if 'ORDINANCE' in up or 'RESOLUTION' in up:
        return 'VOTE'
    if 'STUDY' in up or 'DISCUSSION' in up or 'UPDATE' in up:
        return 'STUDY'
    if 'PLANNED AREA' in up or 'GENERAL PLAN' in up or 'ZONING' in up:
        return 'DEVELOPMENT'
    if 'USE PERMIT' in up or 'SUBDIVISION' in up or 'PLAT' in up:
        return 'DEVELOPMENT'
    return 'ITEM'


def resolve_jurisdiction_prefix(jurisdiction: str) -> str:
    jur = jurisdiction.lower().strip()
    for slug in JURISDICTIONS:
        if jur in slug or slug in jur:
            return slug
    return jur


class HearingFinder:
    def __init__(self):
        self.session = None

    def _get_session(self):
        if self.session is None:
            from db import get_session
            self.session = get_session()
        return self.session

    def find_housing_hearings(
        self, days: int = 30,
        jurisdiction: Optional[str] = None,
        body_filter: Optional[str] = None,
    ):
        from sqlalchemy import text
        session = self._get_session()
        today = date.today()
        end = today + timedelta(days=days)

        kw_conds = ' OR '.join(
            f"(ai.agenda_item_text LIKE '%{kw}%' OR ai.agenda_item_title LIKE '%{kw}%')"
            for kw in HOUSING_KEYWORDS
        )

        scope_conds = ""
        if jurisdiction:
            prefix = resolve_jurisdiction_prefix(jurisdiction)
            scope_conds = f"AND m.body LIKE '{prefix}-%'"
        elif body_filter:
            scope_conds = f"AND (m.body = '{body_filter}' OR m.body LIKE '{body_filter}-%')"

        sql = f"""
            SELECT DISTINCT m.meeting_date, m.body, m.meeting_type,
                   ai.agenda_item_number, ai.agenda_item_title,
                   SUBSTR(ai.agenda_item_text, 1, 500) as excerpt
            FROM meetings m
            JOIN agenda_items ai ON m.meeting_id = ai.meeting_id AND m.body = ai.source_body
            WHERE m.meeting_date >= :today AND m.meeting_date <= :end
              AND ({kw_conds})
              AND LENGTH(ai.agenda_item_title) > 15
              {scope_conds}
            ORDER BY m.meeting_date, m.body, ai.sort_order
        """
        items = list(session.execute(text(sql), {"today": today.isoformat(), "end": end.isoformat()}))

        body_scope = ""
        if jurisdiction:
            prefix = resolve_jurisdiction_prefix(jurisdiction)
            body_scope = f"AND body LIKE '{prefix}-%'"
        elif body_filter:
            body_scope = f"AND (body = '{body_filter}' OR body LIKE '{body_filter}-%')"

        hb_where = ' OR '.join(f"body LIKE '%{s}'" for s in HEARING_BODY_SUFFIXES)
        hb_sql = f"""
            SELECT meeting_date, body, meeting_type,
                   (SELECT COUNT(*) FROM agenda_items ai
                    WHERE ai.meeting_id = m.meeting_id AND ai.body = m.body
                      AND LENGTH(ai.agenda_item_title) > 15) as item_count
            FROM meetings m
            WHERE meeting_date >= :today AND meeting_date <= :end
              AND ({hb_where})
              AND body NOT LIKE 'mc-%' AND body NOT LIKE 'maricopa-%'
              {body_scope}
            ORDER BY meeting_date, body
        """
        hearing_meetings = list(session.execute(text(hb_sql), {"today": today.isoformat(), "end": end.isoformat()}))
        return items, hearing_meetings

    def print_report(self, items, hearing_meetings, as_json=False, jurisdiction=None):
        if as_json:
            output = []
            for r in items:
                excerpt = str(r.excerpt or '')
                output.append({
                    "date": str(r.meeting_date), "body": str(r.body),
                    "type": str(r.meeting_type or ''), "item": str(r.agenda_item_number),
                    "title": str(r.agenda_item_title or ''),
                    "action": classify(excerpt + ' ' + (r.agenda_item_title or '')),
                    "excerpt": excerpt[:300],
                })
            print(_json.dumps(output, indent=2))
            return 0

        jur_label = f" — {JURISDICTIONS.get(jurisdiction or '', jurisdiction or '')}" if jurisdiction else ""
        print()
        print("=" * 70)
        print(f"  YIMBY HOUSING HEARING FINDER{jur_label}")
        if items:
            print(f"  {len(items)} housing-related items from {items[0].meeting_date} to {items[-1].meeting_date}")
        else:
            print("  No housing items found in upcoming meetings.")
            print("  Agenda PDFs for the next 1-2 weeks may not be published yet.")
        print("=" * 70)

        if items:
            sections = [
                ('HEARING', 'PUBLIC HEARINGS — attend and speak', ''),
                ('CALL', 'CALLS FOR HEARING — council scheduling a future hearing', ''),
                ('DEVELOPMENT', 'DEVELOPMENT APPLICATIONS — likely involves a hearing', ''),
                ('VOTE', 'VOTES — hearings already closed, final action', ''),
                ('STUDY', 'STUDY / DISCUSSION — information only', ''),
            ]
            for action, label, _ in sections:
                entries = [r for r in items if classify(
                    str(r.excerpt or '') + ' ' + str(r.agenda_item_title or '')
                ) == action]
                if entries:
                    print(f"\n  {label}")
                    print("  " + "-" * 66)
                    for r in entries:
                        excerpt = str(r.excerpt or '')
                        title = str(r.agenda_item_title or '')
                        d = str(r.meeting_date)
                        b = str(r.body)
                        num = str(r.agenda_item_number)
                        print(f"  {d} | {b:25s} | [{num:5s}] {title[:120]}")
                        if excerpt and len(excerpt) > len(title) + 5:
                            clean = excerpt.replace('\n', ' ').strip()[:350]
                            print(f"  {'':38s}{clean}")
                        print()

        print("  " + "-" * 66)
        print("  UPCOMING HEARING-BODY MEETINGS (P&Z, DRC, BOA):")
        print("     Agendas typically published 5-7 days before.")
        print()
        for r in hearing_meetings:
            d = str(r.meeting_date)
            b = str(r.body)
            ic = str(r.item_count)
            suffix = b.split('-')[-1]
            label = HEARING_BODIES.get(suffix, '')
            print(f"     {d} | {b:25s} | items: {ic:3s} | {label}")
        print()
        return 0


def main():
    import argparse
    p = argparse.ArgumentParser(description="Find upcoming housing hearings")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--jurisdiction", default=None, help="Filter: tempe, mesa, phoenix, etc.")
    p.add_argument("--body", default=None, help="Filter by body code e.g. tempe-drc")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    finder = HearingFinder()
    items, meetings = finder.find_housing_hearings(
        days=args.days, jurisdiction=args.jurisdiction, body_filter=args.body)
    sys.exit(finder.print_report(items, meetings, args.json, args.jurisdiction))


if __name__ == "__main__":
    main()
