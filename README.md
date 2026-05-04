# Maricopa County Board of Supervisors Agenda Tracker

Extract, organize, and persist Maricopa County Board of Supervisors meeting
agendas, supporting materials, and governance records from the Agenda Online
platform.

## Features

- **Meeting discovery** — search Agenda Online by date range
- **Agenda item extraction** — parse structured agenda items from HTML agenda
  pages (items, C-numbers, titles, descriptions)
- **Supporting document extraction** — dynamically click through each agenda
  item to discover attachment links (PDFs, spreadsheets, etc.)
- **Resumable sync** — tracks sync status per meeting (`complete`, `partial`,
  `failed`, `manual_review`, `pending`). Retry only failed meetings.
- **SQLAlchemy persistence** — stores meetings, agenda items, and supporting
  documents in a SQLite database (Postgres-compatible schema)
- **Inspect & query** — CLI tools to explore the database

## Requirements

- Python 3.9+
- Playwright (with Chromium browser)
- SQLAlchemy
- Flask

Install:

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

### Web App

```bash
python app.py
# Opens at http://127.0.0.1:5000/meetings
```

Browse meetings in a Bootstrap 5 table with sync status badges (green = complete, red = failed, etc).

### Database

```bash
# Initialize/migrate the database
python scripts/maricopa_agenda_scraper.py --init-db
```

### Sync a single meeting

```bash
python scripts/maricopa_agenda_scraper.py --sync --meeting-id=4449
```

### Sync a date range

```bash
# Discover and sync all meetings between two dates
python scripts/maricopa_agenda_scraper.py --sync --start-date=2025-01-01 --end-date=2025-01-31

# Resume previously failed/partial/pending meetings
python scripts/maricopa_agenda_scraper.py --sync --start-date=2025-01-01 --end-date=2025-01-31 --retry-failed
```

### Resumable sync flags

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

### Inspect the database

```bash
# List all meetings with item counts
python scripts/inspect_db.py meetings

# Show sync metadata for one meeting
python scripts/inspect_db.py meeting 4449

# List agenda items for a meeting
python scripts/inspect_db.py agenda 4449

# Show full record for one agenda item
python scripts/inspect_db.py item 4449 5

# Search agenda items
python scripts/inspect_db.py search "SETTLEMENT"

# List supporting documents for a meeting
python scripts/inspect_db.py docs 4449

# Sync status summary
python scripts/inspect_db.py status

# List failed/partial meetings
python scripts/inspect_db.py failed
```

## Database Schema

- **meetings** — sync status, item counts, retry tracking
- **agenda_items** — individual agenda items with C-numbers and text
- **supporting_documents** — attachments linked to agenda items

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
README.md
requirements.txt
```
