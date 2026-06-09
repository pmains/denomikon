# ROADMAP.md — Product & Data Ingestion Roadmap

Features and data sources we want to build, ordered by editorial value.
This is a living document — add, reprioritize, and check off as items land.

---

## Platform Features

*These are product/UX features rather than new data sources. They make
what we already have more useful and differentiate the site from
agenda-portal competitors like Citizen Portal AI.*

### F1. Meeting Video Links

**What:** Display a video play icon/link on meeting detail pages for meetings
that have a recorded video available from the jurisdiction's streaming platform.
No autoplay, no embed — just a link that opens the video in a new tab or a
lightbox at the user's choice.

**Why:** Citizen Portal AI's primary differentiator is video + transcripts.
We don't need to match them on transcripts, but providing a one-click path
to the meeting video (where it exists) closes a glaring gap. A user reading
agenda items should be able to watch the corresponding meeting segment with
one click.

**Implementation approach:**

| Platform | Video URL pattern | Ease |
|---|---|---|
| **Granicus** (Buckeye, Scottsdale, Gilbert, Goodyear, Paradise Valley, Queen Creek) | Derive `MediaPlayer.php` from `AgendaViewer.php` URL | Trivial — URL transform |
| **Destiny** (Glendale, El Mirage) | Stream/recording links embedded in agenda HTML page | Medium — scrape from page we already load |
| **Legistar** (Phoenix, Mesa) | Meeting video URL in Legistar calendar HTML | Medium — scrape from calendar detail |
| **OnBase/DataBank** (Maricopa County, Tempe, Gilbert) | Some have video links in meeting detail HTML | Medium — per-instance discovery |
| **CivicClerk** (Avondale, Surprise) | Video links in event API responses | Low — discoverable from existing API |
| **NovusAgenda** (Peoria) | Meeting link may point to video stream | Low — UI exploration needed |
| **Phoenix AEM** | Video embedded on meeting archive pages | Low — scrape from page |

**Steps:**
1. Add `video_url VARCHAR(512)` column to `meetings` table
2. Create a `derive_video_url()` function per platform in a shared module
3. Run a one-time backfill for ~5,000 existing meetings with identifiable URLs
4. Integrate into all scrapers so new meetings get `video_url` on sync
5. Add a small play-button icon (Bootstrap bi-camera-video or similar) on
   meeting detail pages, rendered only when `video_url` is non-null
6. Video opens in a new tab (target="_blank") — zero autoplay, zero embed,
   zero JavaScript tracking

**Priority:** High — low implementation effort, high visibility improvement

---

### F2. AI-Generated Meeting Summaries

**What:** Generate concise, objective summaries of meetings from the structured
agenda item data we already have. Display on meeting detail pages alongside
(or above) the agenda item table.

**Why:** We have 8,200+ meetings and 38 human-written articles. AI summaries
fill the gap — they let every meeting have a "what happened" overview without
requiring a journalist. Citizen Portal AI does this from transcripts; we can
do it from agenda item titles/descriptions without any speech-to-text.

**Design constraints (per your input):**
- Only generate for meetings that are **complete and stable** — past meeting
  date, sync_status = 'synced', not expected to be re-scraped
- Never regenerate after initial generation unless re-scraped
- Cache the result to avoid repeat API costs

**Implementation:**
1. Add `summary TEXT` and `summary_generated_at DATETIME` columns to `meetings`
2. Guard logic in a sync phase or standalone command:
   - Only generate when `meeting_date < today` AND `sync_status = 'synced'`
     AND `summary_generated_at IS NULL`
   - On re-scrape (sync_status → 'pending'), clear `summary_generated_at`
3. LLM prompt: feed agenda item numbers + titles + descriptions + vote outcomes
   Instruct the model to produce a factual, neutral, ~3-paragraph summary
   organized thematically (not by item number)
4. Store generated text in `summary` column
5. Render on meeting_detail.html before the agenda items table, with a small
   "AI-generated" badge and a disclaimer

**Cost estimate:** ~10K input tokens × 8K meetings max = ~2M total input tokens =
~$0.30 total at current pricing (even less with a cheaper fast model).
This is essentially free.

**Priority:** Medium-high — near-zero cost, scalable content, competitive parity

---

### F3. GPT-Style Q&A Over Meeting Data (Premium Feature)

**What:** A natural-language query interface on top of the full meeting corpus:
"What zoning cases are coming before Chandler P&Z next month?" or "How did
Councilmember X vote on short-term rental regulation?"

**Why:** This is Citizen Portal AI's marquee feature. Gating it as premium
turns a competitive threat into a revenue opportunity for a project that
currently has no monetization path.

**Implementation approach (phase 1 — RAG over structured data):**
1. Build a lightweight RAG pipeline:
   - Index: meetings, agenda_items, supporting_doc titles, votes, articles
   - Embed with a small model (text-embedding-3-small)
   - Query → retrieve relevant chunks → LLM answer with source citations
2. No transcripts needed — we're querying agenda structure, which is often
   more useful for "what was decided" questions than "what was said" questions
3. Results include links back to meeting detail pages as source citations

**Gating:**
- Phase 1: API-key gated, accessible from admin dashboard or a `/api/ask`
  endpoint. Free trial (10 queries) then pay.
- Phase 2: If the project needs revenue, a simple subscription tier
  ($10/mo individual, $50/mo institutional)
- Phase 3: Could expand to include transcript search if we ever add
  speech-to-text

**Priority:** Medium — higher effort, but the only item with a direct
monetization path. Could build a working prototype in a weekend if
existing data is already indexed for FTS5.

---

### Priority Rebalance

| Priority | Item | Why |
|---|---|---|
| **1** | **F1. Video links** | Trivial effort, closes biggest gap vs. CP |
| **2** | **F2. AI summaries** | Free content at scale, fills the article gap |
| **3** | **D1. P&Z/DRC/HPC data** | Core housing pipeline data (see below) |
| **4** | Housing data (existing #1) | Still highest editorial value |
| **5** | Budget data (existing #2) | Budget season is now (June) |
| **6** | Vote tallies (existing #5) | Core accountability data |
| **7** | **F3. GPT Q&A** | Monetization pathway, higher effort |
| 8–12 | Existing #3–4, 6–11 | Held as-is |

---

## Data Ingestion — Priority Additions

### D1. Planning & Zoning / DRC / HPC Deep Parsing

**What:** Extract supporting documents, vote records, and "next hearing"
dates from Planning & Zoning, Development Review Commission, and
Historic Preservation Commission meetings across all jurisdictions.

**Why:** The housing pipeline runs through these bodies...

### D2. Tempe Council Subcommittees ✅

**Status:** ✅ Completed (June 1, 2026).

**What:** Add coverage for Tempe City Council subcommittees — ad-hoc
working groups formed by councilmembers to develop policy on specific
topics (animal welfare, housing, public safety, etc.).

**Why:** These subcommittees meet regularly and produce policy
recommendations that feed directly into the full City Council agenda.
Missing them means missing early signals on emerging policy directions.
The subcommittees are also public meetings under Arizona Open Meeting
Law and publish agendas on the Tempe website.

**Implementation:**
1. Build a hybrid scraper (Python + Node.js helper) that bypasses
   tempe.gov's Akamai WAF using node's native fetch()
2. Discovered folder IDs for all 8 subcommittees by scraping their
   document management folder pages (CivicPlus CMS)
3. Each subcommittee has separate Agenda and Minutes folders with
   breadcrumb navigation linking parent folders
4. Scraper matches agenda documents to corresponding minutes
   documents by date, creating meeting records with supporting docs
5. Integrated into `main.py` as `tempe-subcommittees` sync target

**Discovered folder IDs:**

| Subcommittee | Slug | Parent | Agenda | Minutes |
|---|---|---|---|---|
| Animal Welfare | `tempe-animal-welfare-subcommittee` | 7910 | 7911 | 7912 |
| Community Engagement | `tempe-community-engagement-subcommittee` | 7913 | 7914 | 7915 |
| Drink Spiking | `tempe-drink-spiking-subcommittee` | 7842 | 7843 | 7844 |
| Mixed-Use Space | `tempe-mixed-use-space-subcommittee` | 7887 | 7888 | 7889 |
| Mobility Safety | `tempe-mobility-safety-subcommittee` | 7917 | 7918 | 7919 |
| Town Lake | `tempe-town-lake-subcommittee` | 7705 | 7706 | 7707 |
| Term Limits | `tempe-term-limits-subcommittee` | 7990 | 7991 | 7992 |
| Advocacy Review | `tempe-advocacy-review-subcommittee` | 7987 | 7988 | 7989 |

**Scraping approach:**
- `scripts/scraper/tempe_subcommittees_helper.mjs` — Node.js helper
  for HTTP requests (bypasses Akamai WAF)
- `scripts/scraper/tempe_subcommittees.py` — Python scraper that
  calls the Node helper via subprocess and persists meeting data
- Documents are fetched from `tempe.gov/home/showpublisheddocument/{id}`
  PDF URLs extracted from folder page HTML
- 35 meetings indexed across 7 active subcommittees (Term Limits
  has no documents yet)

**Files modified/created:**
- `scripts/scraper/tempe_subcommittees.py` (new)
- `scripts/scraper/tempe_subcommittees_helper.mjs` (new)
- `scripts/scraper/main.py` (added sync handler)
- `scripts/scraper/cli.py` (registered source)

**Usage:**
```
python scripts/scraper/main.py tempe-subcommittees --sync
python scripts/scraper/main.py tempe-subcommittees --sync --body=tempe-animal-welfare-subcommittee
```

**Priority:** Medium — useful for editorial pipeline but less impactful
than P&Z/DRC vote extraction for the housing coverage workflow

**What:** Extract supporting documents, vote records, and "next hearing"
dates from Planning & Zoning, Development Review Commission, and
Historic Preservation Commission meetings across all jurisdictions.

**Why:** The housing pipeline runs through these bodies. Staff reports
contain building plans, density calculations, and staff recommendations.
Vote records show how commissioners split. Minutes often state when
the item goes to the city council for final action — that date is the
signal we need to plan coverage.

**Current state:**
- 9 P&Z bodies have meetings scraped (Mesa, Surprise, Chandler,
  Avondale, Buckeye, Peoria, Glendale PC, El Mirage, Paradise Valley)
- Supporting docs extracted for only 3 of them (Chandler, Glendale PC,
  El Mirage)
- Vote records for 1 (El Mirage — 7 votes total)
- Zero bodies have "next council date" extraction
- ✅ Phoenix PC, VPCs, Historic Pres, Zoning Adj, and other boards/commissions (via AEM scraper)
- Missing entirely: Scottsdale PC/DRB, Tempe DRC,
  Goodyear, Gilbert, Queen Creek

**Implementation phases:**
1. Align P&Z meeting body slugs with public_bodies table
2. Add supporting document scraping to remaining P&Z bodies
3. Add vote extraction for P&Z roll calls (per-platform)
4. Parse minutes and staff reports for "scheduled for council" dates
5. Build a P&Z→CC tracking view that shows housing items moving
   through the pipeline

**First targets — Chandler PZ + Tempe DRC:**
- Chandler PZ (15 meetings in 2026, 7 with results PDFs): the results
  PDFs exist but the parser (`parse_minutes_votes` / `parse_results_votes`)
  doesn't handle the PZ "Agenda-Results" format. Needs a PZ-mode parser.
- Tempe DRC (28 meetings in 2026): meetings synced but no minutes URLs
  captured. Need to discover where Tempe publishes DRC minutes and add
  extraction. OnBase-based, may require the AJAX item detail endpoint.

**Priority:** High — this is the editorial pipeline signal we're missing

---

## 1. Budget Data

**What:** Structured budget data for every jurisdiction — revenues, expenditures,
capital improvement programs, property tax levies, and Truth-in-Taxation filings.

**Why:** Budget season (May–June) generates 40+ meetings across jurisdictions.
Currently we rely on agenda text and news reports for dollar figures. Ingesting
budget data as structured fields would let us answer questions like "which city
has the highest per-capita CIP spending" or "how does Buckeye's property tax
rate compare to Goodyear's" without manual research.

**Sources:**
- City budget documents (PDFs published on city websites)
- Arizona Department of Revenue — property tax levy filings
- Truth-in-Taxation notices (required by A.R.S. Title 42, published in newspapers)

**Data model ideas:**
- `budgets` table: jurisdiction_id, fiscal_year, total_revenue, total_expenditure, property_tax_levy, cip_total, status (proposed/adopted)
- `budget_line_items` table: budget_id, department, category, amount
- `capital_projects` table: jurisdiction_id, project_name, fiscal_year, amount, status

---

## 2. Board, Commission, and Council Data for Missing Jurisdictions

**What:** Complete public body coverage for all jurisdictions in Maricopa County.

**Currently covered:** 17 jurisdictions (see AGENTS.md)

**Still missing or partial:**
- Fountain Hills (Town Council, P&Z, boards)
- Carefree (Town Council)
- Cave Creek (Town Council, P&Z)
- Litchfield Park (City Council)
- Tolleson (City Council)
- Wickenburg (Town Council)
- Youngtown (Town Council)
- Guadalupe (Town Council)
- Gila Bend (Town Council)
- Salt River Pima-Maricopa Indian Community (Tribal Council — may not have public agendas)
- Fort McDowell Yavapai Nation (same)
- Gila River Indian Community (same)

**Also needed:** Maricopa County Special Districts — flood control, library district,
community college district governing boards.

**Priority:** Fountain Hills, Cave Creek, Litchfield Park (growing suburbs with
active development agendas). Tribal governments are lower priority due to
sovereignty and limited public agenda access.

---

## 3. Maricopa Association of Governments (MAG) Data

**What:** MAG is the regional planning agency for the Phoenix metro area. It
produces data and plans that bind all member jurisdictions.

**Key MAG products to ingest:**
- Regional Transportation Plan (RTP) — updated every 4 years, dictates which
  projects get federal funding
- Population and employment projections — used by every city for planning
- Housing needs assessments
- Air quality conformity determinations (tied to transportation funding)
- MAG committee and council meeting agendas

**Why:** MAG decisions affect every jurisdiction but receive almost no media
coverage. A MAG vote on regional transportation funding allocation is more
consequential for most residents than a routine city council meeting.

**Sources:**
- https://azmag.gov — meeting agendas, documents, data portal
- MAG Open Data portal

---

## 4. Comprehensive Plans, General Plans, and Area Plans

**What:** Every Arizona city and county is required to adopt a general plan
(comprehensive plan) under the Growing Smarter acts. These plans are adopted
by voter ratification and must be updated every 10 years. They are the
foundational documents that control all zoning.

**What to track:**
- Plan adoption dates and expiration dates
- Plan amendment schedules
- Specific area plans (Chandler Airport Planning Area, Tempe Rio Salado, etc.)
- Plan content summaries (land use maps, growth areas, infrastructure assumptions)

**Why:** General plan updates create windows for activism and development.
A city updating its general plan is making decisions about where growth goes
for the next decade. The Chandler "Evolving the Chandler Way" update (June 2026)
is one example. Maricopa County's Framework 2040 was another. These are
high-value editorial opportunities.

**Known active updates:**
- Chandler: "Evolving the Chandler Way" (2026, EDAB review June 3)
- Maricopa County: Framework 2040 (adopted May 2026)
- Phoenix: General Plan update cycle TBD
- Mesa: 2050 General Plan update cycle TBD

---

## 5. Vote Tallies for Every Jurisdiction

**What:** Per-item, per-member vote records for every public body we track.

**Currently covered:**
- Maricopa County BOS: ✅ (supervisor_votes table)
- P&Z Commission: ✅ (member_votes + pz_minutes parsing)
- Chandler: ✅ (minutes vote extraction)
- Tempe CC: ✅ (OnBase Legal Action Summary PDF — roll-call names + tallies)
- Tempe DRC: ✅ (Summary PDF — aggregate tallies, no member names)
- Tempe BOA: ✅ (Summary PDF — aggregate tallies, no member names)
- Tempe HA: ✅ (Summary PDF — aggregate tallies, no member names)
- All other jurisdictions: not covered

**Why:** Vote records are the most granular accountability data we can provide.
Knowing that a zoning case passed 6-1 with Councilmember X dissenting is more
informative than knowing it passed. Vote patterns over time reveal who votes
with whom, who dissents, and what issues split governing bodies.

**Challenge:** Most jurisdictions publish votes only in adopted minutes (PDF),
not in digital form. Requires minutes PDF parsing for every body.

**Priority bodies for vote ingestion:**
1. Phoenix City Council
2. Mesa City Council
3. Scottsdale City Council
4. Glendale City Council
5. All Planning & Zoning Commissions

---

## 6. Census Data for Maricopa County

**What:** Decennial census data, American Community Survey estimates, and
population projections for every jurisdiction, census tract, and block group
in Maricopa County.

**Key data points:**
- Population (total, by age, by race/ethnicity)
- Households (count, size, tenure — owner/renter)
- Income (median household, poverty rate)
- Housing (units, vacancy rate, year built, value)
- Commuting (mode, travel time)

**Why:** Growth patterns only make sense in context. A rezoning for 46 homes in
Mesa reads differently when you know the census tract added 2,000 residents in
5 years. A budget item for road widening means more when you can show the
commuter shed. Census data is the "why this matters" layer behind every
development and infrastructure story.

**Sources:**
- U.S. Census Bureau API (census.gov)
- ACS 5-year estimates (2020-2024 available in 2025)
- Arizona Office of Economic Opportunity population estimates
- Maricopa Association of Governments projections

**Integration:** Could be ingested as dimension tables (`census_tracts`,
`census_acs_estimates`) and used to enrich meeting-level data views and
article context.

---

## Priority Order (Draft)

| Priority | Item | Editorial Value | Ingest Difficulty |
|---|---|---|---|
| 1 | Housing: Permit data + affordability pipeline | High — every development story | Medium — scraping + API |
| 2 | Budget data | High — budget season now | Medium — PDF parsing |
| 3 | Housing: Zoning + land use data | High — structural context | Hard — GIS ingestion |
| 4 | Vote tallies | High — core accountability | Hard — minutes PDF per body |
| 5 | Housing: Rental market metrics | Medium — enriches stories | Medium — API + scraping |
| 6 | Missing jurisdictions | Medium — broader coverage | Medium — new scraper per city |
| 7 | Housing: Hearings browseability | Medium — makes existing data usable | Low — UI work |
| 8 | General Plan tracking | Medium — long-term planning | Low — manual + calendar |
| 9 | Census data | Medium — enriches everything | Low — API |
| 10 | MAG data | Medium — regional context | Low — API + scraping |
| 11 | Housing: Element/RHNA tracking | Lower — annual reporting cycle | Low — manual + documents |

---

## 7. Housing Data Improvements

**Goal:** Make the housing data pipeline useful as a browsing tool, an activism
resource, and a story-generating engine — not just a raw data dump.

### 7a. Permit Data Coverage

**Currently:** Mesa (Socrata, 36K permits) and Scottsdale (ArcGIS, 1,262 records).

**Needed:** Phoenix, Chandler, Tempe, Glendale, Peoria, Gilbert, Surprise, Buckeye,
Goodyear, Avondale — at minimum the top 10 cities by population.

**Why:** Permit data tells the supply side of the housing story. Without permit
data from Phoenix and Chandler, we're missing the two largest housing markets
in the county.

**Data model:** Standardize `permits` table across cities with normalized
jurisdiction, category (residential/commercial), work type (new/remodel/demo),
square footage, valuation, and issue date.

### 7b. Affordability Pipeline

**What:** Track which housing projects include affordability commitments
(inclusionary zoning, LIHTC, bond-financed affordable units). Link permit data
to meeting agenda items that approved the development.

**Data sources:**
- P&Z and council meeting items with affordability conditions
- County bond issuances for affordable housing (IDA, BOS)
- Arizona Department of Housing LIHTC allocation records
- HUD multifamily property inventory

**Why:** The most important question in every housing story is "will anyone who
needs it be able to afford it?" Currently we have to research this manually
for each article. A structured affordability pipeline would let us say
"Chandler approved 400 new units this year. 32 are deed-restricted affordable."

### 7c. Housing Hearings Browseability

**Currently:** `agenda_scraper.py hearings` CLI finds housing-related agenda items
by keyword. Results are flat text.

**Improvements:**
- Filter by jurisdiction, date range, project type (rezoning, general plan amendment,
  annexation, development agreement)
- Show on a map (Leaflet, already have the infrastructure)
- Timeline view — what's in the pipeline this month vs next quarter
- Email/RSS alerts for new hearings matching criteria

### 7d. Zoning and Land Use

**What:** Ingest zoning maps and land use designations. Know what land is zoned
for what, and where multi-family housing is permitted vs prohibited.

**Why:** Zoning is the structural constraint behind every housing story.
A rezoning for 46 homes in Mesa is only meaningful if you know the current
zoning and what the surrounding parcels allow. "This 40-acre parcel is zoned
agricultural but surrounded by single-family residential" tells a different
story than just listing the agenda item.

**Data sources:**
- City zoning GIS layers (most cities publish shapefiles or ArcGIS services)
- Maricopa County Assessor parcel data (already ingested for apartment/
  residential summaries)
- General Plan land use maps (tied to item 4 above)

### 7e. Rental Market Metrics

**What:** Rents, vacancy rates, eviction filings by jurisdiction and over time.

**Data sources:**
- Maricopa County Justice Court eviction records (public)
- Zillow / Apartment List rental data (may require API or scraping)
- ACS rental data (tied to item 6, Census)
- HUD Fair Market Rents (published annually)

**Why:** A rezoning story is incomplete without context on what housing costs
in that area. "The council approved 46 homes on a 40-acre parcel where median
rent has risen 35% in three years" is a different story than just the approval.

### 7f. Housing Element and RHNA Tracking

**What:** Every Arizona city must include a housing element in its general plan.
Track what each jurisdiction committed to and whether they're meeting it.

**Data sources:**
- City general plans (housing elements)
- Annual progress reports (some cities publish these)
- State housing element review letters

**Why:** Holding cities accountable to their own plans is the most defensible
form of housing activism. When a city denies a multi-family rezoning while
its own housing element says it needs 2,000 new rental units, that contradiction
is the story.

---

*Last updated: 2026-05-30*


---

## Platform Vision — Chat Gateway for Citizen Hackers

*This section captures strategic direction from the June 1, 2026 vision
discussion. These are not near-term action items — they inform how the
project should evolve once Maricopa County is fully built out.*

### Vision

Turn the project into an expandable governance data platform modeled on
Citizen Portal AI and Wikipedia — where:

- **The chat interface is the contributor gateway.** Citizen hackers
  describe the jurisdiction they want to track, and an agent builds the
  scraper iteratively through conversation. No PRs, no dev environment
  setup, no code review. The conversation is the audit trail.
- **The database schema is the portable foundation.** Any city's meeting
  structure maps to the same schema (jurisdiction → body → meeting →
  agenda_item → supporting_document → vote → member).
- **The scraper library is shared.** A scraper written for one city on
  a platform (OnBase, Legistar, Granicus, etc.) becomes available for
  any other city on the same platform.
- **Multiple newsrooms can spin off.** The article system, Bluesky
  pipeline, and featured picks are all jurisdiction-agnostic. A Tucson
  newsroom uses the same codebase pointed at a different tenant filter.
- **The read-only API is the contract.** Any frontend — news site,
  query tool, government dashboard — talks to the same API.

### Sequencing

1. **First:** Build out Maricopa County fully (current work: P&Z/DRC votes,
   Tempe subcommittees, supporting docs, video links, AI summaries)
2. **Second:** Onboard Pima County as the second tenant — smaller, fewer
   jurisdictions, proves the model without building infrastructure first
3. **Third:** Build the platform infrastructure once the model is proven
   (multi-tenant API, read-only query interface, tenant onboarding flow)

### Infrastructure Layers (for later)

| Layer | Current state |
|---|---|
| Chat gateway | Working — this workspace |
| Shared scraper library | Growing — 14 jurisdictions, 180 bodies |
| Multi-tenant DB | Schema ready (jurisdiction_id everywhere), single-tenant today |
| Read-only API | Partial — meeting pages exist, no public API |
| Bolt-on newsrooms | Maricopa works, no tenant routing |
| Query interface for public | Not built |
