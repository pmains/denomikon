"""Housing Hearing Finder — find upcoming public hearings on housing development.

Usage:
    agenda_scraper.py hearings [--days=30] [--body=BODY] [--json]

Scans upcoming meetings for agenda items related to housing development
and flags which are public hearings where you can attend and speak.

Part of the YIMBY Maricopa toolkit.
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

HEARING_BODIES = {
    'pz': 'Planning & Zoning Commission — regular hearings on zonings',
    'drc': 'Development Review Commission — development applications',
    'boa': 'Board of Adjustment — variances and special use permits',
    'bza': 'Board of Zoning Adjustment',
    'hpc': 'Historic Preservation Commission',
}


def classify(text: str) -> str:
    up = text.upper()
    if 'PUBLIC HEARING' in up:
        return 'HEARING'
    if 'HEARING' in up:
        return 'HEARING'
    if ('ORDINANCE' in up and 'INTRODUCE' in up):
        return 'HEARING'
    if 'CALL FOR' in up and 'HEARING' in up:
        return 'CALL'
    if 'ORDINANCE' in up or 'RESOLUTION' in up:
        return 'VOTE'
    if 'STUDY' in up or 'DISCUSSION' in up or 'UPDATE' in up:
        return 'STUDY'
    if 'PLANNED AREA' in up or 'GENERAL PLAN' in up or 'ZONING' in up:
        return 'DEVELOPMENT'
    if 'USE PERMIT' in up:
        return 'DEVELOPMENT'
    if 'SUBDIVISION' in up or 'PLAT' in up:
        return 'DEVELOPMENT'
    return 'ITEM'


class HearingFinder:
    def __init__(self):
        self.session = None

    def _get_session(self):
        if self.session is None:
            from db import get_session
            self.session = get_session()
        return self.session

    def find_housing_hearings(self, days: int = 30, body_filter: Optional[str] = None):
        from sqlalchemy import text
        
        session = self._get_session()
        today = date.today()
        end = today + timedelta(days=days)
        today_iso = today.isoformat()
        end_iso = end.isoformat()

        kw_conds = ' OR '.join(
            f"(ai.agenda_item_text LIKE '%{kw}%' OR ai.agenda_item_title LIKE '%{kw}%')"
            for kw in HOUSING_KEYWORDS
        )

        body_cond = ""
        if body_filter:
            body_cond = f"AND (m.body = '{body_filter}' OR m.body LIKE '{body_filter}-%')"

        sql = f"""
            SELECT DISTINCT m.meeting_date, m.body, m.meeting_type,
                   ai.agenda_item_number, ai.agenda_item_title,
                   SUBSTR(ai.agenda_item_text, 1, 500) as excerpt
            FROM meetings m
            JOIN agenda_items ai ON m.meeting_id = ai.meeting_id AND m.body = ai.source_body
            WHERE m.meeting_date >= :today AND m.meeting_date <= :end
              AND ({kw_conds})
              AND LENGTH(ai.agenda_item_title) > 15
              {body_cond}
            ORDER BY m.meeting_date, m.body, ai.sort_order
        """
        
        items = list(session.execute(text(sql), {"today": today_iso, "end": end_iso}))
        
        hearing_bodies_sql = f"""
            SELECT meeting_date, body, meeting_type,
                   (SELECT COUNT(*) FROM agenda_items ai
                    WHERE ai.meeting_id = m.meeting_id AND ai.body = m.body
                      AND LENGTH(ai.agenda_item_title) > 15) as item_count
            FROM meetings m
            WHERE meeting_date >= :today AND meeting_date <= :end
              AND (body LIKE '%-pz' OR body LIKE '%-drc' OR body LIKE '%-boa' 
                   OR body LIKE '%-bza' OR body LIKE '%-hpc')
              AND body NOT LIKE 'mc-%'
              AND body NOT LIKE 'maricopa-%'
            ORDER BY meeting_date, body
        """
        
        hearing_meetings = list(session.execute(text(hearing_bodies_sql), {"today": today_iso, "end": end_iso}))
        
        return items, hearing_meetings

    def print_report(self, items, hearing_meetings, as_json=False):
        if as_json:
            output = []
            for r in items:
                excerpt = str(r.excerpt or '')
                output.append({
                    "date": str(r.meeting_date),
                    "body": str(r.body),
                    "type": str(r.meeting_type or ''),
                    "item": str(r.agenda_item_number),
                    "title": str(r.agenda_item_title or ''),
                    "action": classify(excerpt + ' ' + (r.agenda_item_title or '')),
                    "excerpt": excerpt[:300],
                })
            print(_json.dumps(output, indent=2))
            return 0

        today_str = date.today().isoformat()
        print(f"\n{'='*70}")
        print(f"  🏠 HOUSING HEARING FINDER — Next {len(items)} housing items in {items[0].meeting_date if items else '?'} to {items[-1].meeting_date if items else '?'}" if items else 
              f"\n{'='*70}\n  🏠 HOUSING HEARING FINDER — No housing items found")
        print(f"{'='*70}")

        if not items:
            print("\n  No housing-related items found in upcoming meetings.")
            print("  Agenda PDFs for the next 1-2 weeks may not be published yet.")
            print("  Check meeting websites directly.")
        else:
            print()
            for r in items:
                excerpt = str(r.excerpt or '')
                title = str(r.agenda_item_title or '')
                combined = excerpt + ' ' + title
                action = classify(combined)
                
                icons = {
                    'HEARING': '🗣️',
                    'CALL': '📅',
                    'VOTE': '⚖️',
                    'STUDY': '📋',
                    'DEVELOPMENT': '🏗️',
                    'ITEM': '📄',
                }
                icon = icons.get(action, '📄')
                
                d = str(r.meeting_date)
                b = str(r.body)
                num = str(r.agenda_item_number)
                
                print(f"  {icon} {d} | {b:25s} | {action:12s} | [{num:5s}] {title[:115]}")
                if action in ('HEARING', 'DEVELOPMENT') and excerpt and len(excerpt) > len(title) + 5:
                    clean = excerpt.replace('\n', ' ').strip()[:350]
                    print(f"  {'':48s}{clean}")
                print()

        print(f"  {'─'*66}")
        print(f"  🏛️  UPCOMING HEARING-BODY MEETINGS (P&Z, DRC, BOA):")
        print(f"     Agendas are typically published 5-7 days before the meeting date.")
        print()
        for r in hearing_meetings:
            d = str(r.meeting_date)
            b = str(r.body)
            mt = str(r.meeting_type or '')
            ic = str(r.item_count)
            suffix = b.split('-')[-1]
            label = HEARING_BODIES.get(suffix, '')
            print(f"     {d} | {b:25s} | items: {ic:3s} | {label}")
        print()

        return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Find upcoming housing hearings")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--body", default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    
    finder = HearingFinder()
    items, meetings = finder.find_housing_hearings(args.days, args.body)
    sys.exit(finder.print_report(items, meetings, args.json))
