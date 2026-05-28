# Housing Pipeline Dashboard — Architecture Plan

## Goal
Replace `/permits` with views that track housing construction across Maricopa County: completed units (Assessor), approved projects (meeting data), and upcoming hearings.

## Server Constraint
2GB RAM → pre-aggregate everything at ingest time. No raw table scans at page load.

---

## Phase 1: Completed Units (Assessor Data)

### Data Sources
| File | What | Est. Size |
|---|---|---|
| Apartment Master (pipe-delimited) | All apartment complexes — units, rents, year built, address | ~5-10MB (10K records) |
| Residential Master (pipe-delimited) | All SF homes, condos, townhouses — beds, baths, year built, address | ~500MB-1GB (800K+ records) |
| Secured Master (pipe-delimited) | Valuation for everything — assessed value, land value | ~500MB-1GB |

### Database Schema
```sql
-- Pre-aggregated by jurisdiction + year + type
CREATE TABLE housing_units_summary (
    jurisdiction_slug TEXT NOT NULL,
    year INTEGER NOT NULL,
    unit_type TEXT NOT NULL,  -- 'apartment', 'sf', 'condo', 'townhouse'
    units INTEGER NOT NULL,
    avg_rent REAL,             -- NULL for SF
    avg_sqft REAL,
    avg_land_value REAL,
    avg_improvement_value REAL,
    PRIMARY KEY (jurisdiction_slug, year, unit_type)
);

-- Pre-aggregated by year only (county-wide)
CREATE TABLE housing_units_county (
    year INTEGER PRIMARY KEY,
    total_units INTEGER,
    apartment_units INTEGER,
    sf_units INTEGER
);
```

### Ingestion Pipeline
1. Download ZIPs (need user's help — ArcGIS requires interactive session)
2. Parse pipe-delimited files
3. Map SitusCity → jurisdiction_slug
4. Aggregate by (city, year, type)
5. Index and compact DB

### Query Performance
- Dashboard queries hit summary tables (100-200 rows) — not raw parcel data
- Raw parcel data stays in a separate file (`data/parcels.sqlite`) for drill-down queries
- Main `maricopa.sqlite` only gets the compact summaries

---

## Phase 2: Approved / In-Review (Meeting Data — Already Own)

This comes from our existing meeting database:
- Rezonings approved (council votes)
- PAD overlays approved
- Development agreements signed
- Use permits for housing

Query: `meetings + agenda_items WHERE action_type = 'VOTE' AND housing_keywords`

---

## Phase 3: Frontend

### Views
| View | Data Source | Render Approach |
|---|---|---|
| **Map** | Pre-computed GeoJSON per city (not individual parcels) | Leaflet — server sends ~500KB max |
| **Chart** | `housing_units_summary` table (~2000 rows total) | Chart.js — client-side, small payload |
| **Table** | Same summary table | Server-side pagination, max 100 rows per page |

### Caching Strategy
- Summary data is updated weekly (after Assessor file refresh)
- Flask cache at `/housing` endpoint: 1 hour TTL
- Chart data endpoint: 30 min TTL
- Map data endpoint: 1 hour TTL

---

## Bottleneck Analysis

| Risk | Mitigation |
|---|---|
| Residential Master = 800K+ rows | Store in separate DB, never query in-memory |
| Multiple concurrent users hitting same query | Pre-aggregated tables + aggressive caching |
| Map with 800K points | Aggregate to city-level. "Recent" view shows last 3 years only |
| 2GB RAM during ingestion | Run ingestion locally, sync the summary to production |
| Old `/permits` still running | Keep it read-only on the old data, no new data feeds |

## Next Step
Download the data files so I can build the ingestion script and schema.

