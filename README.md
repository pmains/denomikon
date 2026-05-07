# Maricopa County Board of Supervisors & Planning & Zoning Agenda Tracker

Extract, organize, and persist Maricopa County public governance materials
from the Agenda Online and AgendaCenter platforms.

## Features

- **Meeting discovery** — search for BOS and P&Z meetings by date range
- **Agenda item extraction** — parse structured agenda items (items, C-numbers,
  titles, descriptions, case numbers)
- **Supporting document extraction** — discover and link attachment PDFs to
  agenda items
- **P&Z Agenda PDF parsing** — for Planning & Zoning meetings, downloads the
  actual agenda PDF and extracts real agenda items (case numbers, project
  names, applicants, recommendations, etc.)
- **Body-scoped identity** — BOS and P&Z use separate `body` namespaces
  (`bos`/`pz`) so meeting IDs never collide
- **Resumable sync** — tracks sync status per meeting (`complete`, `partial`,
  `failed`, `manual_review`, `pending`). Retry only meetings that need it.
- **Vote tracking** — extract roll-call votes from meeting summaries and track
  individual supervisor votes
- **Case tracking** — track case numbers across meetings (BOS → P&Z cross-references)
- **Web app** — Flask web UI with filtering, pagination, and meeting detail pages
- **Inspect & query** — CLI tools to explore the database

## Requirements

- Python 3.9+
- Playwright (with Chromium browser) — for browser-backed scraping
- `pdftotext` (poppler-utils) — for P&Z agenda PDF parsing
- SQLAlchemy
- Flask

Install:

```bash
pip install -r requirements.txt
playwright install chromium
brew install poppler        # macOS — provides pdftotext
```

## Usage

### Web App

```bash
python app.py
# Opens at http://127.0.0.1:5000/meetings
```

Browse meetings in a Bootstrap 5 table with sync status badges, filters by
body/type/date, and pagination.

### Database

```bash
# Initialize/migrate the database
python scripts/maricopa_agenda_scraper.py --init-db
```

## Board of Supervisors (BOS)

Commands default to BOS when no subcommand is given. The `bos` subcommand is
optional for BOS operations.

### Sync a single meeting

```bash
python scripts/maricopa_agenda_scraper.py --sync --meeting-id=4449
```

Or with explicit subcommand:

```bash
python scripts/maricopa_agenda_scraper.py bos --sync --meeting-id=4449
```

### Sync a date range

```bash
# Discover and sync all meetings between two dates
python scripts/maricopa_agenda_scraper.py bos --sync --start-date=2025-01-01 --end-date=2025-01-31

# Resume previously failed/partial/pending meetings (no date range needed)
python scripts/maricopa_agenda_scraper.py bos --sync --retry-failed
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
python scripts/maricopa_agenda_scraper.py --status

# List failed/partial meetings
python scripts/maricopa_agenda_scraper.py --failed

# List meetings needing manual review (image-based agendas)
python scripts/maricopa_agenda_scraper.py --failed --include-manual-review
```

### Vote syncing

```bash
# Extract roll-call votes from a meeting's summary page
python scripts/maricopa_agenda_scraper.py bos --sync-votes --meeting-id=4449
```

## Planning & Zoning (PZ)

Use the `pz` subcommand. All date flags use YYYY-MM-DD format.

```bash
# Sync P&Z meetings by date range
python scripts/maricopa_agenda_scraper.py pz --sync --start-date=2026-01-01 --end-date=2026-05-01

# Sync a single P&Z meeting by ID
python scripts/maricopa_agenda_scraper.py pz --sync --meeting-id=3734

# Limit the number of meetings from a date range search
python scripts/maricopa_agenda_scraper.py pz --sync --start-date=2026-01-01 --limit=5
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

## Legacy --sync-pz (deprecated)

The old `--sync-pz` flag still works but prints a deprecation warning:

```bash
# Old style (deprecated, prints warning):
python scripts/maricopa_agenda_scraper.py --sync-pz --pz-start-date=01/01/2026

# New style (preferred):
python scripts/maricopa_agenda_scraper.py pz --sync --start-date=2026-01-01
```

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

## Body-Scoped Identity

BOS and P&Z meetings share the same meeting_id namespace when stored in the
database, differentiated by the `body` column:

- `body="bos"` — Board of Supervisors
- `body="pz"` — Planning & Zoning

This allows one-click cross-referencing: when a PZ case number appears on a BOS
agenda item (or vice versa), both meetings can be found without ID prefix hacks.

The `--body` filter is available on inspect commands and in the web UI.

## Database Schema

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

The `sync_status` field tracks:
- `complete` — successfully extracted and persisted
- `partial` — items extracted but supporting docs failed
- `failed` — network/parse error, worth retrying
- `manual_review` — page loaded but image-based/unparseable format
- `pending` — discovered but not yet synced

## Project Structure

```
scripts/
  maricopa_agenda_scraper.py   Main scraper tool
  db.py                        Persistence layer (SQLAlchemy models)
  inspect_db.py                Database inspection CLI
app.py                         Flask web application
templates/
  base.html                    Base template
  meetings.html                Meeting list with pagination
  meeting_detail.html          Meeting detail with items/docs/votes
  c_number.html                C-number revision history
data/
  maricopa.sqlite              SQLite database
  agendas/                     Downloaded agenda PDFs
  agenda-items/                Extracted CSV exports
  supporting-materials/        Downloaded supporting documents
tests/
  test_maricopa_agenda_scraper.py
  test_persistence.py
  test_cli.py
  test_inspect_db.py
  test_meeting_normalization.py
  test_metadata_parsing.py
  test_supporting_docs.py
  test_capture_fixtures.py
  test_tiers.py
  test_agenda_item_extraction.py
README.md
requirements.txt
```

## Tests

```bash
# Run the full test suite (199 tests)
python -m unittest discover -s tests
```
