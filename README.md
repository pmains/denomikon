# Poliscopic — Arizona Public Governance & Development Intelligence

Extract, organize, and persist public governance materials and development
permit data from Arizona jurisdictions.

## What it does

Poliscopic ingests and displays two categories of public data:

### Meeting & Agenda Tracking

- BOS, P&Z, Board of Adjustment, Board of Health, Drainage Review Board,
  Transportation Advisory Board, Industrial Development Authority, Tempe City
  Council, and other Tempe boards
- Agenda item and supporting document extraction
- Vote tracking and case-number cross-referencing

### Development Permit Analysis

Structured permit data from **four jurisdictions** with cross-jurisdiction
category and work-type normalization:

| Jurisdiction | Records | Source | Coverage |
|---|---|---|---|
| **City of Phoenix** | ~728,000 | PDD CSV Export | 2004–present |
| **Maricopa County** | ~150,000 | Weekly XLSX reports | 2012–present |
| **City of Tempe** | ~19,000 | ArcGIS FeatureServer | 2019–present |
| **City of Chandler** | ~1,700 | DSActiveProjects | 2017–2025 |

Phoenix provides the richest dataset with valuation, zoning, parcel numbers,
contractor, owner, and completion dates. Data is normalized into a shared
category model (Residential, Commercial, Industrial, Mixed-Use, Other) and
work type model (New Construction, Addition, Alteration, Trade, Demolition,
Infrastructure, Unknown).

### Web App

Bootstrap 5 Flask UI with:
- Permit overview with summary charts and category breakdowns
- Year, jurisdiction, category, and work-type filter panel
- Summary and raw-permit views with pagination
- Meeting browser with sync status badges and detail pages
- Jurisdiction-aware member rosters and voting records

## Requirements

- Python 3.9+
- Playwright (with Chromium browser) — for browser-backed scraping
- `pdftotext` (poppler-utils) — for P&Z agenda PDF parsing
- SQLAlchemy
- Flask (with Flask-Caching for route-level caching)
- openpyxl (for XLSX permit report parsing)
- xlrd (for legacy XLS permit report parsing)

Install:

```bash
pip install -r requirements.txt
playwright install chromium
brew install poppler        # macOS — provides pdftotext
```

### Dependencies

Core:
```
flask
flask-caching
sqlalchemy
playwright
openpyxl
xlrd
```

Optional (for PDF parsing only):
- `pdftotext` (poppler-utils)

## Usage

### Permits — CLI

```bash
# City of Phoenix — PDD CSV export (rich fields: valuation, zoning, parcel)
python scripts/permit_scraper.py --phoenix --pdd-sync                        # Full sync (2004-today)
python scripts/permit_scraper.py --phoenix --pdd-sync --dry-run              # Preview
python scripts/permit_scraper.py --phoenix --pdd-sync --start-date=2026-01-01 --end-date=2026-02-01
python scripts/permit_scraper.py --phoenix --pdd-inspect                     # Sample a week of data

# City of Phoenix — ArcGIS Planning/Permit Layer (2-year window, coordinates)
python scripts/permit_scraper.py --phoenix --sync                            # Full sync
python scripts/permit_scraper.py --phoenix --inspect-source                  # Sample records

# City of Tempe — ArcGIS Accela Building Permits
python scripts/permit_scraper.py --tempe --sync                              # Full sync
python scripts/permit_scraper.py --tempe --inspect-source                    # Sample records
python scripts/permit_scraper.py --tempe --dry-run                           # Preview

# City of Chandler — DSActiveProjects (high-profile development only)
python scripts/permit_scraper.py --chandler --sync                           # Full sync
python scripts/permit_scraper.py --chandler --inspect-source                 # Sample records

# Maricopa County — Weekly XLSX activity reports
python scripts/permit_scraper.py --discover --download --sync                # Full pipeline
python scripts/permit_scraper.py --summary --by month                        # Aggregate summary
```

### Web App

```bash
python app.py
# Opens at http://127.0.0.1:5001/meetings
```

Browse meetings and permits in a Bootstrap 5 table with sync status badges,
filters by body/type/date/jurisdiction, and pagination.

### Database

```bash
# Initialize/migrate the database
python scripts/agenda_scraper.py --init-db
```

## Board of Supervisors (BOS)

Commands default to BOS when no subcommand is given. The `bos` subcommand is
optional for BOS operations.

### Sync a single meeting

```bash
python scripts/agenda_scraper.py --sync --meeting-id=4449
```

Or with explicit subcommand:

```bash
python scripts/agenda_scraper.py bos --sync --meeting-id=4449
```

### Sync a date range

```bash
# Discover and sync all meetings between two dates
python scripts/agenda_scraper.py bos --sync --start-date=2025-01-01 --end-date=2025-01-31

# Resume previously failed/partial/pending meetings (no date range needed)
python scripts/agenda_scraper.py bos --sync --retry-failed
```

### Sync resumable flags

| Flag | Description |
|---|---|
| `--retry-failed` | Only process meetings with `failed`, `partial`, or `pending` status |
| `--force` | Re-sync everything, including already-complete meetings |
| `--retry-count N` | Max retry attempts per meeting (default 3) |
| `--skip-complete` | Skip complete meetings when using `--meeting-id` |
| `--include-manual-review` | Include `manual_review` meetings in retry operations |

### Status & inspection

```bash
# Summary of sync status across all meetings
python scripts/agenda_scraper.py --status

# List failed/partial meetings
python scripts/agenda_scraper.py --failed

# List meetings needing manual review (image-based agendas)
python scripts/agenda_scraper.py --failed --include-manual-review
```

### Vote syncing

```bash
# Extract roll-call votes from a meeting's summary page
python scripts/agenda_scraper.py bos --sync-votes --meeting-id=4449
```

## Planning & Zoning (PZ)

Use the `pz` subcommand. All date flags use YYYY-MM-DD format.

```bash
# Sync P&Z meetings by date range
python scripts/agenda_scraper.py pz --sync --start-date=2026-01-01 --end-date=2026-05-01

# Sync a single P&Z meeting by ID
python scripts/agenda_scraper.py pz --sync --meeting-id=3734

# Limit the number of meetings from a date range search
python scripts/agenda_scraper.py pz --sync --start-date=2026-01-01 --limit=5
```

When no start/end date is given, PZ defaults to the last 90 days.

### How P&Z sync works

1. **Search** — queries the AgendaCenter search page for PZ meetings
2. **Overview page** — visits the meeting's document-index page
3. **Agenda PDF** — identifies the actual agenda document (not staff reports)
4. **PDF parsing** — downloads the agenda PDF, extracts real agenda items
   (numbered items with case numbers, project names, applicants, etc.)
5. **Staff reports** — staff-report documents from the overview page are linked
   to agenda items by case number as supporting documents

The overview page (document index) is **not** treated as the agenda. ZIPPOR
meetings (Zoning Infrastructure Policy Procedure Ordinance Review) are
supported but use a different PDF format with slightly different item
structure.

## Body-Scoped Identity

All bodies (BOS, PZ, ADJ, DRAIN, Health, TAB, IDA, Tempe bodies) use separate
`body` namespaces so meeting IDs never collide. This allows one-click
cross-referencing: when a PZ case number appears on a BOS agenda item (or vice
versa), both meetings can be found without ID prefix hacks.

The `--body` filter is available on inspect commands and in the web UI.

## Inspect the database

```bash
# List all meetings with item counts (shows body column)
python scripts/inspect_db.py meetings

# Filter by body
python scripts/inspect_db.py meetings --body=pz

# Show sync metadata for one meeting
python scripts/inspect_db.py meeting 4669

# List agenda items for a meeting
python scripts/inspect_db.py agenda 4669

# Show full record for one agenda item
python scripts/inspect_db.py item bos 4669 1     # with body scope
python scripts/inspect_db.py item 4669 1         # auto-detect

# Search agenda items
python scripts/inspect_db.py search "SETTLEMENT"

# List supporting documents for a meeting
python scripts/inspect_db.py docs 4669

# List supporting documents for a specific item
python scripts/inspect_db.py docs 4669 --body=bos

# Sync status summary
python scripts/inspect_db.py status

# List failed/partial meetings
python scripts/inspect_db.py failed

# Show vote summary for a meeting
python scripts/inspect_db.py votes 4669

# Show vote detail for one item
python scripts/inspect_db.py vote 4669 1

# Show all votes cast by a supervisor
python scripts/inspect_db.py votes-by-supervisor "Thomas Galvin"

# Search voted items
python scripts/inspect_db.py votes-search "C-86-25-001"

# List all supervisors
python scripts/inspect_db.py supervisors

# List all cases with event counts
python scripts/inspect_db.py cases

# Show full detail for a case (with cross-referenced meetings)
python scripts/inspect_db.py case CPA250011
python scripts/inspect_db.py case-history CPA250011      # alias
```

## Routes

| Path | Description |
|---|---|
| `/` | Homepage — navigate to Meetings, Members, or Permits |
| `/meetings` | Meeting list with search, filter, and pagination |
| `/meetings/<body>/<meeting_id>` | Meeting detail — agenda items, documents, votes |
| `/bodies` | Public bodies index — all jurisdictions and their bodies |
| `/bodies/<slug>` | Body detail — paginated member roster |
| `/members` | Unified member index (redirects to /bodies) |
| `/members/<id>` | Individual member profile and voting record |
| `/members/<slug>/analytics` | Voting analytics for a member |
| `/permits` | Permit overview with summary, charts, detail table, and raw list |
| `/permits/category/<name>` | Year-over-year breakdown for a single permit category |
| `/c-number/<c_number_base>` | Case number revision history |

## Data Model

See `scripts/db.py` for the full SQLAlchemy model definitions.

Key entities:
- **Jurisdiction** — A county, city, or town (e.g., Maricopa County, City of Phoenix)
- **PublicBody** — A board, commission, or committee within a jurisdiction
- **PublicBodyMember** — A person who serves or served on a public body (title, district/seat, date range)
- **Meeting** — A meeting of a public body with agendas, documents, and voting records
- **Permit** — A permit extracted from jurisdiction-specific sources (source_system-tagged, jurisdiction-aware)

### Permit data sources

| `source_system` | Jurisdiction | Integration method |
|---|---|---|
| `tempe_arcgis_accela_building_permits` | City of Tempe | ArcGIS FeatureServer REST |
| `phoenix_pdd` | City of Phoenix | PDD Excel-to-CSV export endpoint |
| `phoenix_arcgis_planning_permit` | City of Phoenix | ArcGIS MapServer (2-year window) |
| `chandler_arcgis_dsactiveprojects` | City of Chandler | ArcGIS MapServer (high-profile only) |
| `unknown` (default) | Maricopa County | XLSX report extraction |

Permit schemas differ by source. The `native_type` and `native_category` fields
preserve the original label, while `normalized_category` provides
cross-jurisdiction filtering (Residential, Commercial, Industrial, Mixed-Use,
Other).

## Database Schema (Legacy)

- **meetings** — sync status, item counts, retry tracking, body scope
- **agenda_items** — individual agenda items with C-numbers and case numbers
- **supporting_documents** — attachment documents linked to items
- **meeting_supervisors** — supervisor attendance per meeting
- **agenda_item_votes** — roll-call vote results per item
- **supervisor_votes** — individual supervisor votes
- **pz_item_details** — structured P&Z metadata (case number, district, project
  name, applicant, request, location, recommendation)
- **cases** — case numbers tracked across meetings
- **case_events** — event history per case (agenda appearance, hearings, votes)
- **jurisdictions** — Government jurisdictions (counties, cities, towns)
- **public_bodies** — Boards, commissions, committees within a jurisdiction
- **permits** — Permit records extracted from jurisdiction-specific pipelines
- **permit_reports** — Weekly permit activity report (XLSX) metadata
- **public_body_members** — Membership roster for public bodies
- **meeting_attendance** — Per-meeting attendance records
- **member_votes** — Generalized vote records for non-BOS bodies
- **executive_session_participants** — BOS executive session advisors

The `sync_status` field tracks:
- `complete` — successfully extracted and persisted
- `partial` — items extracted but supporting docs failed
- `failed` — network/parse error, worth retrying
- `manual_review` — page loaded but image-based/unparseable format
- `pending` — discovered but not yet synced

## Project Structure

```
scripts/
  scraper/
    tempe_permits.py       City of Tempe ArcGIS permit scraper
    chandler_permits.py    City of Chandler DSActiveProjects scraper
    phoenix_permits.py     City of Phoenix ArcGIS + PDD CSV scraper
    agenda_scraper.py      Main agenda/meeting scraper CLI
    ...
  permit_scraper.py        Unified permit scraping CLI (--tempe, --chandler, --phoenix)
  db.py                    Persistence layer (SQLAlchemy models)
  inspect_db.py            Database inspection CLI
app.py                     Flask web application
templates/
  base.html                Base template
  permits.html             Permit overview with charts, filters, tables
  permit_category.html     Year-over-year category breakdown
  meetings.html            Meeting list with pagination
  meeting_detail.html      Meeting detail with items/docs/votes
  ...
data/
  maricopa.sqlite          SQLite database
  phoenix/                 Phoenix discovery artifacts (ArcGIS metadata, samples)
```

## Tests

```bash
# Run the full test suite
python -m unittest discover -s tests
# or
python -m pytest tests/

# Run permit-specific tests
python -m pytest tests/test_tempe_permits.py -v
```

## Performance Optimizations

### SQLite PRAGMAs

The following PRAGMAs are applied automatically on every connection:

| PRAGMA | Value | Effect |
|---|---|---|
| `journal_mode` | WAL | Concurrent reads + writes without lock contention |
| `synchronous` | NORMAL | Reduces fsync calls without risking corruption |
| `temp_store` | MEMORY | Temp tables/indices live in RAM |
| `cache_size` | -20000 | 20 MB page cache |
| `foreign_keys` | ON | Enforce referential integrity |

### Cold-Render Optimization

The permits aggregate query uses a single dedup CTE with SQL GROUP BY instead
of loading all matching rows into Python memory (reduced from ~170k rows to
~500 aggregate rows). The result is cached with Flask-Caching (7-day TTL).

### Server-Side Caching

Flask-Caching caches the following routes:

| Route | Cache TTL | Notes |
|---|---|---|
| `/permits` | 7 days | Varies by query string |
| `/meetings` | 60s | Varies by query string (body, type, date, page) |
| `/meetings/<id>` | 120s | Per-meeting detail |
| `/members` | 120s | Member directory |

The cache directory is `.cache/flask-cache/` and is auto-created.

### Request Timing

Every request over 1 second is logged as a warning with the elapsed time:
```
WARNING:/permits 1.4s
```

### Benchmarking

```bash
# Requires the Flask app to be running on :5001
python scripts/benchmark.py
```

## Recommended sync workflow (weekly)

```bash
# Maricopa County permit reports
python scripts/permit_scraper.py --discover --download --sync

# City of Tempe — incremental (syncs only new records)
python scripts/permit_scraper.py --tempe --sync

# City of Chandler — re-sync (DSActiveProjects is curated, not incremental)
python scripts/permit_scraper.py --chandler --sync

# City of Phoenix — incremental PDD (date-range loop handles dedup)
python scripts/permit_scraper.py --phoenix --pdd-sync

# Pre-warm Flask cache
python scripts/permit_scraper.py --phoenix --pdd-sync --start-date=2026-05-01 --end-date=2026-05-16
```
