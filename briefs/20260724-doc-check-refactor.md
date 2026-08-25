# Brief: Promote Doc Check to 1st-Class Scheduler

## Problem

The existing `doc_check.py` lives under `scripts/scraper/jurisdictions/tempe/` and only knows how to check Tempe OnBase meetings for late-published supporting documents. Other jurisdictions — Granicus, Legistar, CivicClerk, AgendaQuick — have the same "documents come later" dynamic but no re-check mechanism. If a meeting's minutes or supporting docs aren't published when the agenda gets scraped, we never come back for them (except for minutes URLs via `minutes_check.py`, which is a narrower concern).

## Design

Move to the same pattern as `minutes_check.py`:

```
scripts/
  scraper/
    common/
      doc_check.py          ← Scheduler: finds meetings due for check,
                               dispatches to platform probes, manages
                               backoff/sunset, exposes CLI + API
    platforms/
      onbase.py             ← Add `check_meeting_docs(onbase_config, meeting)` probe
      granicus_common.py    ← Add probe for Granicus instances
      civicclerk.py         ← Add probe for CivicClerk instances
      agendacenter.py       ← Add probe for AgendaCenter instances
      ...                   ← Others as needed
    jurisdictions/
      tempe/
        doc_check.py        ← Deprecated; remove after migration
```

## What the Scheduler (`common/doc_check.py`) Does

Same contract as the Tempe checker, but platform-agnostic:

1. **Query meetings** due for check — `next_doc_check_at <= now`, `supporting_docs_extracted == False`, `items_extracted == True`, `sync_status IN ('complete', 'pending')`.
2. **Map body → platform** (OnBase, Granicus, CivicClerk, etc.) and dispatch to the right probe.
3. **Lightweight check** — each platform probe does one quick HTTP GET/HEAD to see if documents are present now. No full re-scrape.
4. **If docs found** → trigger targeted re-sync (same `_resync_meeting` pattern, but dispatches to the correct scraper).
5. **If not found** → exponential backoff (`next_doc_check_at += 2d → 4d → 8d → 16d`).
6. **Sunset** — stop checking after 30 days past meeting date.
7. **Skeleton meeting detection** — if the meeting page shows only boilerplate/agenda items (Call to Order, Minutes, Adjournment) and nothing substantive, mark `no_agenda` and stop checking.

## What Each Platform Probe Returns

```python
# Simple contract — just answer one question:
def check_meeting_docs(meeting: Meeting) -> DocCheckResult:
    """Check if supporting docs are available now for this meeting.

    Returns a dataclass/namedtuple (or raises).
    """
    # Fields:
    #   docs_available: bool     — are item-level supporting docs published?
    #   is_skeleton: bool        — past meeting with only boilerplate items?
    #   error: str | None        — fetch/parse error message
```

The scheduler handles all the scheduling logic (backoff, sunset, re-sync dispatch). The probe is just one question.

## Existing Code to Preserve

The Tempe OnBase probe already exists at `scripts/scraper/jurisdictions/tempe/doc_check.py` — the `fetch_agenda_page()`, `has_item_detail_handlers()`, `_is_skeleton_meeting()`, and `_resync_meeting()` functions. These become a `check_meeting_docs()` function in `scripts/scraper/platforms/onbase.py`.

The skeleton detection logic (`_SKELETON_TITLE_LOWERS`, `_SUBSTANTIVE_KEYWORDS`) should move into the scheduler since it's a general heuristic, not OnBase-specific.

## Platform Probe Priority

| Platform | CMS | Needs probe? | Notes |
|---|---|---|---|
| Tempe | OnBase | Existing — move to `platforms/onbase.py` | Already works, just needs a new home |
| Buckeye, Surprise, Goodyear, Avondale | Granicus | New | Minutes come later as RSS entries |
| Phoenix, Mesa, Glendale | Legistar | New | Minutes as attachments on MeetingDetail |
| Chandler, Gilbert, Peoria, etc. | CivicClerk | New | Documents may be posted after sync |
| Scottsdale | AgendaQuick | New | Check Destiny for late docs |

First pass: OnBase (already works) + Granicus (parallels existing `minutes_check.py` work). CivicClerk and AgendaQuick can follow once the pattern is proven.

## Wiring into `daily_sync.py`

Replace the inlined Tempe-specific doc check in `sync_log.sh` (lines ~292-350) with a single call:

```python
from scraper.common.doc_check import run_doc_check
run_doc_check()   # or run_doc_check(limit=...) for control
```

Same pattern as `minutes_check.py`. The scheduler owns the queries, the probes own the platform logic, `daily_sync.py` just calls it.

The standalone CLI (`python3 scripts/scraper/common/doc_check.py --apply`) stays for manual use / debugging — same as `downloader.py`.

## What to Keep from `sync_log.sh`

The current `sync_log.sh` doc check section does two things:
1. Seeds `next_doc_check_at` for Tempe meetings that need checking
2. Runs `doc_check.py --apply`

After the refactor, `daily_sync.py` handles both: it seeds `next_doc_check_at` for *all* platforms' meetings after the main sync, then calls the scheduler.

The seeding logic (`seed_next_doc_check_at`) moves into `daily_sync.py` or the scheduler itself — applied once per meeting after first sync if `supporting_docs_extracted == False`.

## Files to Create / Modify

| File | Action |
|---|---|
| `scripts/scraper/common/doc_check.py` | **Create** — scheduler + CLI |
| `scripts/scraper/platforms/onbase.py` | **Modify** — add `check_meeting_docs()` from existing Tempe code |
| `scripts/scraper/platforms/granicus_common.py` | **Modify** — add `check_meeting_docs()` |
| `scripts/daily_sync.py` | **Modify** — replace inlined seed+check with scheduler call |
| `scripts/sync/sync_log.sh` | **Modify** — remove doc check block (now in daily_sync.py) |
| `scripts/scraper/jurisdictions/tempe/doc_check.py` | **Deprecate** → remove after migration verified |

## Non-Goals

- This brief does *not* cover re-checking for documents from non-Tempe jurisdictions that use OnBase (none exist yet — Tempe is the only OnBase jurisdiction).
- This brief does *not* change the `supporting_docs_extracted` / `next_doc_check_at` schema — the existing columns are sufficient.
- This brief does *not* add CivicClerk or AgendaQuick probes. Those are follow-up briefs once the Granicus probe validates the pattern.

## Verification

1. Run `python3 scripts/scraper/common/doc_check.py --dry-run` — should report the same meetings as `scripts/scraper/jurisdictions/tempe/doc_check.py --dry-run`
2. Run `python3 scripts/daily_sync.py --days-back 3` — should include doc check in output logs
3. Wire up: `scripts/scraper/jurisdictions/tempe/doc_check.py` is deleted only after confirming the new scheduler handles all Tempe OnBase meetings it was checking
