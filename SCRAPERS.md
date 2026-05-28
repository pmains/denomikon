# SCRAPERS.md — Maricopa Governance Data Scrapers

This file documents every scraper, platform, and data-quality rule for the
Maricopa County governance data project.  Use it when adding a new scraper,
debugging sync issues, or evaluating data quality.

---

## Platforms

| Platform | Jurisdictions |
|---|---|
| **OnBase Agenda Online** | Tempe, Maricopa BOS (via `scripts/scraper/onbase.py`) |
| **Legistar (Granicus)** | Phoenix, Mesa, Glendale |
| **Granicus ViewPublisher** | Buckeye, Peoria, Scottsdale, Avondale, Goodyear, El Mirage |
| **AgendaQuick** | Chandler |
| **NovusAgenda** | Peoria (legacy), Buckeye (deprecated — no data) |
| **CivicClerk** | Surprise (P&Z, additional bodies) |
| **Phoenix AEM API** | Phoenix (city-published PDFs via JSON endpoint) |

---

## Scraper Data Quality Rules

- Cancelled meetings (notices of cancellation) should **not** be synced. Detect
  and skip "Notice of Cancellation", "Cancelled", "Vacated" entries.
- "Upcoming Agenda Items" placeholder entries should be skipped — they are not
  real meetings.
- Meetings with `sync_status=pending` and `meeting_date >= today` are future
  meetings whose agendas haven't been published yet. They are not errors.
- If a scraper produces a suspicious meeting type or title, inspect the source
  HTML to understand the actual format before fixing.

---

## Adding a New Scraper

1. Research the city's agenda platform (Legistar, NovusAgenda, AgendaQuick,
   CivicClerk, OnBase, Granicus, Phoenix AEM, etc.)
2. Check if an existing scraper targets the same platform (see above)
3. Build the scraper in `scripts/scraper/{city}.py`
4. Register in `scripts/scraper/cli.py` (help text + source routing)
5. Register in `scripts/scraper/main.py` (sync dispatch block)
6. Add jurisdiction to the database and `routes/meetings.py` jurisdiction mapping
7. Add body options and jurisdiction to `templates/meetings.html`
8. Test with `--limit=2` before running a full sync

---

## Per-Scraper Reference

### OnBase Adapter (`scripts/scraper/onbase.py`)

Reusable Hyland OnBase Agenda Online platform adapter.  Supports any OnBase
Agenda Online instance with per-jurisdiction configuration.

**`OnBaseConfig`** — dataclass holding per-instance settings:
- `host`, `base_path`: instance URL
- `search_method`: `"GET"` (Maricopa) or `"POST"` (Tempe)
- `csrf_required`: whether search requires `__RequestVerificationToken`
- `meeting_types`: mapping of `public_body_slug → [OnBase type IDs]`
- `download_url_pat`, `agenda_view_path`, `meeting_view_path`: URL templates

**`OnBaseAgendaClient(config)`** — wraps module-level functions:
- `search(page, start, end, type_ids)` — search meetings via GET/POST
- `fetch_agenda(page, meeting_id)` — get agenda-items HTML
- `parse_agenda(html, meeting_id)` — parse into section + item dicts
- `build_download_url()`, `build_agenda_download_url()`, `build_packet_download_url()`

Module-level functions (usable without class):
- `parse_meetings_from_html()` — parse search results
- `parse_agenda_html()` — parse agenda sections/items
- `search_meetings()` — full Playwright search workflow
- `fetch_csrf_token()` / `extract_csrf_token_from_html()`

**Pre-built Configs:**
- `TEMPE_CONFIG` — City of Tempe (`tempe.hylandcloud.com`, POST search with CSRF,
  type IDs: 109=Regular CC, 101=Work Study, etc.)
- `MARICOPA_BOS_CONFIG` — Maricopa BOS (`mccobagenda.databankcloud.com`, GET search)

**Agenda Parsing:**
`parse_agenda_html()` handles the OnBase `accessible-section` / `accessible-item`
DOM structure.  Returns two item types:
- `item_type="section"` — structural headings (e.g. `4A Approval of Minutes`)
- `item_type="item"` — actionable items (e.g. `7B3 Authorize contract...`)
Items at level 0 (root content) are parsed first, then sub-items.

---

### Mesa Legistar Scraper (`scripts/scraper/mesa.py`)

Uses `mesa.legistar.com`.  Key differences from OnBase scrapers:
- Telerik RadGrid HTML table on Calendar.aspx for meeting discovery
- MeetingDetail.aspx shows agenda items with links to LegislationDetail.aspx
- LegislationDetail pages contain attachments (staff reports, exhibits)
- Year navigation via ASP.NET postback

**Architecture:**
- `search_mesa_meetings(body_slugs)` — synchronous HTTP fetch of Calendar.aspx
- `parse_meetings_from_html(html)` — parse RadGrid table into meeting dicts
- `parse_agenda_items_from_html(html)` — parse MeetingDetail agenda table
- `fetch_agenda_items_async(url, meeting_id)` — async HTTP fetch of agenda items
- `parse_legislation_detail_from_html(html)` — extract item details + attachments

**Supported bodies:**
- `mesa-city-council` (Council regular + study sessions)
- `mesa-planning-zoning` (Planning & Zoning Board)
- `mesa-design-review-board` (Design Review Board)
- `mesa-board-of-adjustment` (Board of Adjustment)
- `mesa-historic-preservation-board` (Historic Preservation Board)
- `mesa-cadence-cfd`, `mesa-eastmark-cfd-1`, `mesa-eastmark-cfd-2` (CFD Boards)

Usage: `python agenda_scraper.py mesa --sync --bodies=mesa-city-council`
Only City Council meetings are synced by default.

---

### Phoenix Legistar Scraper (`scripts/scraper/phoenix.py`)

Uses `phoenix.legistar.com`.  Same Legistar platform as Mesa.
- Calendar.aspx with year POST selection
- MeetingDetail.aspx returns 410 Gone for detail pages
- **Agenda items cannot be extracted** from Legistar directly
- Primary source for Phoenix data is the **[AEM JSON API](#phoenix-aem-api)** below

---

### Phoenix AEM API / PDF Scraper (`scripts/scraper/phoenix_pdf.py`)

Phoenix publishes Agenda and Results PDFs at predictable URLs discovered via
a JSON API endpoint on the city's AEM website.

**JSON API endpoint:**
```
.../city-council-meetings/_jcr_content/root/container/container/
    container-content/dynamic_table.table-results.json?offset=N
```
Returns 10 meetings per page with `properties` containing:
- `agendaDocumentLinkPdf` — Agenda PDF URL
- `resultsDocumentLinkPDF` — Results PDF URL (vote outcomes)
- `minutesDocumentLinkPDF` — Approved minutes PDF URL
- `meetingType` — "Formal Meeting", "Policy Session", "Work Study", etc.
- `meetingDatetime` — ISO 8601 datetime

**PDF URL pattern:**
```
/content/dam/phoenix/cityclerksite/city-council-meeting-files/{year}/
    {M-D-YY}%20{Type}%20Agenda-FINAL.pdf
```

**Item parsing:** Uses "Item No. X" markers in the PDF text to identify agenda
items.  Results PDF shows vote outcomes ("This item was adopted").

**612 total meetings** in the API — includes Council, subcommittees, and info
packets going back to 2021.

---

### Buckeye Granicus Scraper (`scripts/scraper/buckeye_granicus.py`)

Uses `buckeyeaz.granicus.com` (Granicus ViewPublisher).
- `ViewPublisher.php?view_id=1` — HTML meeting list (full history, ~600 meetings)
- `ViewPublisherRSS.php?view_id=1&mode=agendas` — Agenda RSS feed (~100 recent)
- `ViewPublisherRSS.php?view_id=1&mode=minutes` — Minutes RSS feed
- Agenda packets are PDF-based via CloudFront CDN

**Body mapping:** 12 bodies mapped from Granicus names.  Buckeye's old
NovusAgenda platform is deprecated with no data.

---

### Chandler AgendaQuick Scraper (`scripts/scraper/chandler.py`)

Uses `public.destinyhosted.com` (AgendaQuick / CivicClerk platform).
- Daily calendar view with month navigation
- Individual meeting detail pages with full agenda items
- Attachments endpoint (`dsp=atf`) for minutes PDFs
- 26 bodies covered — the most of any jurisdiction

---

### Scottsdale Scraper (`scripts/scraper/scottsdale.py`)

Uses Scottsdale's city website with PDF-based agendas.
- 6 bodies: City Council, Planning Commission, Board of Adjustment,
  Development Review Board, Historic Preservation, Building Appeals Board

---

### Surprise CivicClerk Platform

Surprise uses **CivicClerk** for Planning & Zoning Commission and other
boards.  City Council data comes from Granicus (`surprise.py`).

**Portal:** `https://surpriseaz.portal.civicclerk.com/event/{eventId}/overview`

**OData API:** `https://surpriseaz.api.civicclerk.com/v1`

**Event discovery:**
- `GET /Events?$filter=eventDate ge 2026-01-01&$top=100` — filter by date (URL-encoded)
- Each event returns: `id`, `eventName`, `eventDate`, `agendaId`, `categoryName`
- `@odata.nextLink` for pagination
- `$orderby=eventDate desc` for reverse chronological
- `$filter=contains(eventName,'Planning')` to filter by body name
- Works with `$top`, `$skip`, `$skiptoken` for pagination

**54 events found for 2026** across 16 bodies (P&Z, Council, Arts, Veterans, Library, Parks, PSPRS, Audit, etc.)

**Event document files:**
Each event has a `publishedFiles[]` array containing:
```json
{
  "fileId": 8336,
  "type": "Agenda",
  "name": "05/21/2026 Planning and Zoning Commission Meeting Agenda",
  "url": "stream/SURPRISEAZ/...pdf",
  "fileType": 1
}
```
- `fileType=1` — Agenda PDF
- `fileType=2` — Agenda Packet
- `fileType=4` — Minutes PDF

**File download endpoints:**
- `GET /v1/Meetings/GetMeetingFileStream(fileId={fileId},plainText=false)` — direct PDF download
- `GET /v1/Meetings/GetMeetingFile(fileId={fileId},plainText=false)` — JSON with `blobUri`

**Body mapping:**
CivicClerk `categoryName` → internal body code:
- "Planning and Zoning Commission" → `surprise-pz`
- "Regular City Council Meeting" → `surprise-cc` (synced via Granicus instead)
- "Regular City Council Work Session" → `surprise-cc` (synced via Granicus)
- "Arts and Cultural Advisory Commission" → `surprise-arts`
- "Veteran, Disability and Human Service Commission" → `surprise-veterans`
- "Parks and Recreation Commission" → `surprise-parks`
- "Library Commission" → `surprise-library`
- "Public Safety Personnel Retirement System Commission – Fire" → `surprise-psprs-fire`
- "Public Safety Personnel Retirement System Commission – Police" → `surprise-psprs-police`
- "Health Benefits Trust Fund Board" → `surprise-health-benefits`
- "Boards and Commissions Nominations Committee" → `surprise-nominations`
- "City Audit Committee" → `surprise-audit`
- "Tourism Fund Subcommittee" → `surprise-tourism`

**To build a scraper:**
1. Query `/Events` with `$filter=eventDate ge YYYY-MM-DD` and paginate
2. For each event, map `categoryName` to a body code
3. Find the Agenda file in `publishedFiles[]` (fileType=1)
4. Download the agenda PDF via `GetMeetingFileStream(fileId,plainText=false)`
5. Parse PDF text for agenda items
6. Persist meeting + items via `replace_meeting_data_safe`

### Avondale CivicClerk Platform (via `scripts/scraper/civicclerk.py`)

Avondale uses **CivicClerk** for all boards. The same reusable `civicclerk.py`
module drives both Surprise and Avondale with different configs.

**Portal:** `https://avondaleaz.portal.civicclerk.com/?category_id=26`

**OData API:** `https://avondaleaz.api.civicclerk.com/v1`

API structure is identical to Surprise (same CivicClerk platform). Key differences:
- Subdomain: `avondaleaz` instead of `surpriseaz`
- Body mapping covers all 13 Avondale bodies, not just boards
- CLI: `agenda_scraper.py avondale --sync` (current)
- Legacy: `agenda_scraper.py avondale-granicus --sync` (Granicus RSS, no items)

**Body mapping:**
| CivicClerk Category | Body Code | Notes |
|---|---|---|
| City Council | `avondale-cc` | 41 meetings, 346 items |
| City Council Subcommittee | `avondale-cc` | Subsumed under CC |
| Planning Commission | `avondale-pz` | 6 meetings, 20 items |
| Board of Adjustment | `avondale-boa` | 3 meetings, 9 items |
| Art Committee | `avondale-arts` | 4 meetings, 44 items |
| Audit Committee | `avondale-audit` | 2 meetings, 18 items |
| Sustainability Commission | `avondale-sustainability` | 4 meetings, 26 items |
| PSPRS Board | `avondale-psprs` | 3 meetings, 21 items |
| CFD Board (Alamar/Lakin) | `avondale-cfd` | 2 meetings, 10 items |
| Judicial Advisory Board | `avondale-judicial` | 5 meetings, 38 items |
| Parks, Rec & Libraries Board | `avondale-parks` | 2 meetings, 17 items |
| Neighborhood & Family Services | `avondale-neighborhood` | 1 meeting, 10 items |
| Employee Benefit Trust Board | `avondale-benefits` | 1 meeting, 9 items |
| Risk Management Trust Fund | `avondale-risk` | 1 meeting, 10 items |

**Usage:**
```sh
# Full sync (all bodies, current year)
scripts/agenda_scraper.py avondale --sync

# Single body
scripts/agenda_scraper.py avondale --sync --bodies=avondale-cc

# Legacy Granicus sync (metadata only)
scripts/agenda_scraper.py avondale-granicus --sync
```

### Other Scrapers

| Scraper | Platform | Bodies |
|---|---|---|
| `glendale.py` | Legistar | 1 (City Council) |
| `surprise.py` | Granicus | 1 (City Council) |
| `peoria.py` | NovusAgenda | 4 (CC, P&Z, BOA, Subcommittee) |
| `avondale.py` | Granicus (legacy) | 3 (CC, P&Z, BOA) — metadata only, no items |
| `civicclerk.py` (avondale_config) | CivicClerk | 13 bodies (CC, P&Z, BOA + 10 boards) — 578+ items |
| `goodyear.py` | Granicus | 7 bodies |
| `el_mirage.py` | Granicus | 3 (CC, P&Z, BOA) |
| `gilbert.py` | Granicus | 1 (Town Council) |

---

## Tests

### Unit Tests

Unit tests live in `scripts/tests/` and test individual scraper parsing.
Tests should:
- Use fixture HTML/PDF files instead of live network requests
- Run without requiring database access
- Be fast and deterministic

### Database Isolation

All tests that use a temporary database must use the `set_database_url()`
pattern from `db.py`:

```python
from db import set_database_url
set_database_url("sqlite:///path/to/temp.sqlite")
init_db()
```

Never modify `os.environ["DATABASE_URL"]` — it leaks across processes.

### End-to-End Tests

End-to-end tests validate complete ingestion workflows against real or
archived meeting sources. These may include:
- Playwright/browser automation
- dynamic Agenda Online extraction
- AgendaCenter ingestion  
- PDF parsing
- document downloads
- database persistence verification

Because end-to-end tests are slower and more fragile, they should run less
frequently than unit tests.
