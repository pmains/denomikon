# OPS.md — Operations & Architecture Reference

This file documents the web application, article system, deploy pipeline, and
social posting infrastructure.  For scraper reference, see [SCRAPERS.md](SCRAPERS.md).

---

## Web Application

### Entry Point

`app.py` — creates the Flask app and runs the dev server.

```python
FLASK_PORT=5001 python app.py   # Dev server (port 5000 is OpenClaw Control Center)
```

Production runs via **gunicorn** behind systemd on `poliscopic.com`:

```
poliscopic.service → gunicorn -w 1 -b 127.0.0.1:5000
```

### Route Structure (`routes/`)

| File | Purpose |
|---|---|
| `routes/__init__.py` | App factory, cache setup, blueprints |
| `routes/meetings.py` | `/meetings` — meeting list/detail pages |
| `routes/articles.py` | `/articles` — article detail, `/search` — full-text search |
| `routes/admin.py` | `/admin/` — article management, featured widget, notifications |
| `routes/bodies.py` | `/bodies` — public body listing |
| `routes/members.py` | Body member/seats info |
| `routes/codes.py` | Code/data lookups |
| `routes/themes.py` | Theme/settings routes |
| `routes/auth.py` | Login/logout for admin |

### Templates (`templates/`)

| Template | Purpose |
|---|---|
| `base.html` | Base layout with nav, favicon, JS includes |
| `meetings.html` | Meeting list with filters (jurisdiction, body, type, date range) |
| `meeting_detail.html` | Single meeting with agenda items table |
| `search.html` | Full-text search across articles + agenda items |
| `article.html` | Article detail page (featured image, body, sources) |
| `admin/dashboard.html` | Admin dashboard with Draft/Published/Archived/Featured tabs |
| `admin/article_form.html` | Article create/edit form (title, body, image, sources, tags) |
| `admin/suggestions.html` | AI story suggestions |

### Caching

Flask-Caching caches `/meetings` and `/meetings/{body}/{id}` routes:

- Meeting list: 60s cache (query-string aware)
- Meeting detail: 120s cache
- Cache file: `data/flask_cache.sqlite` (production only, removed on deploy)

---

## Database Schema

### Core Tables (`scripts/db/models.py`)

| Table | Purpose | Key Fields |
|---|---|---|
| `meetings` | Meeting metadata | `body`, `meeting_id`, `meeting_date`, `meeting_type`, `source_url`, `minutes_url`, `sync_status`, `item_count_actual`, `jurisdiction_id` |
| `agenda_items` | Individual agenda items | `body`, `meeting_id`, `agenda_item_number`, `agenda_item_title`, `item_type`, `section_level`, `sort_order` |
| `public_bodies` | Body definitions | `body_code`, `name`, `slug`, `jurisdiction_id` |
| `jurisdictions` | City/county definitions | `slug`, `name` |

### Article System (`scripts/db/newsroom.py`)

| Table | Purpose | Key Fields |
|---|---|---|
| `articles` | News articles | `title`, `slug`, `summary`, `body`, `status` (draft/published/archived), `featured_image`, `image_credit`, `is_featured` |
| `article_sources` | Source links for articles | `article_id`, `source_url`, `source_type`, `item_title` |
| `tags` | Article tags (15 available) | `name`, `slug` |
| `article_tags` | M2M: articles → tags | `article_id`, `tag_id` |
| `social_posts` | Social cross-post tracking | `article_id`, `platform`, `post_url`, `posted_at` |

### Full-Text Search

Two FTS5 virtual tables, rebuilt after article saves and agenda syncs:

- `agenda_items_fts` — indexes `agenda_item_title` and `agenda_item_text`
- `articles_fts` — indexes `title`, `summary`, `body`

---

## Deploy Pipeline

### sync.sh

The only authorized way to push to production:

```
bash sync.sh
```

**What it does:**

1. **Verifies** local database has at least 10 meetings (safety check)
2. **Backs up** production database (`maricopa.sqlite.bak.{timestamp}`)
3. **Snapshots** local database (`snapshots/maricopa.sqlite.{timestamp}`, keeps last 10)
4. **Rsyncs** files to `root@poliscopic.com:/opt/poliscopic/`:
   - `scripts/` (scrapers + db modules + bluesky)
   - `db/` (newsroom, queries, persist, models, minutes_check)
   - `app.py`
   - `data/maricopa.sqlite` (⚠ replaces production database)
   - `static/` (images, uploads)
   - `templates/` (HTML templates)
5. **Rsyncs** `routes/` separately (avoids trailing-slash path scattering)
6. **Chowns** files to `poliscopic:poliscopic`

**Post-deploy (manual):**

```bash
ssh root@poliscopic.com "systemctl restart poliscopic"
```

**Post-deploy (Bluesky):**

```bash
ssh root@poliscopic.com "cd /opt/poliscopic && \
  POLISCOPIC_DB_TIER=development source .env && \
  .venv/bin/python scripts/bluesky_sync.py --limit=10"
```

---

## Bluesky Social Pipeline

### files

- `scripts/social.py` — Bluesky API wrapper (`post_to_bluesky()`)
- `scripts/bluesky_sync.py` — CLI tool that finds unposted articles and posts them

### How it works

```
bluesky_sync.py --limit=10
           ↓
get_unposted_articles() → queries `social_posts` table for articles not yet posted
           ↓
post_article() → calls post_to_bluesky() with title + summary + URL
           ↓
mark_posted() → inserts into `social_posts` table
```

### Dedup

The `social_posts` table tracks which articles have been posted.  The database
is replaced on each deploy (via `sync.sh`), so `social_posts` must be
backfilled before deployment if new articles were added since the last deploy.

---

## Minutes Check Pass

### File

`scripts/db/minutes_check.py`

### How it works

Called by `daily_sync.py` after the main sync.  Re-visits completed meetings
to discover minutes PDFs that were published after the initial scrape.

**Granicus:** Re-fetches RSS minutes feed → matches by `clip_id` → updates
`minutes_url` on matching meetings.

**Chandler (AgendaQuick):** Re-fetches attachments page (`dsp=atf`) for each
meeting → looks for PDFs with "minutes" in the filename → updates
`minutes_url`.

**Tempe (OnBase):** Re-fetches Legal Action Summary PDFs for each meeting →
updates `minutes_url`.

### Column

`meetings.minutes_url` — set to the URL of the published minutes/results PDF.
Also `meetings.votes_extracted` (boolean, for the vote parsing pass).

---

## Daily Sync Cron

### File

`scripts/daily_sync.py`

### Schedule

| Tier | When | What |
|---|---|---|
| Tier 1 | Every run | Chandler, Tempe (most active jurisdictions) |
| Tier 2 | Every run | All other jurisdictions |
| Tier 3 | Sunday 4 AM | Historical backfill (2025 data for BOS, IDA, TAB, P&Z, ADJ, Health) |

### Cron Jobs

| Name | Schedule | Type |
|---|---|---|
| `maricopa-daily-sync` | 5 AM Phoenix time | OpenClaw isolated agentTurn |
| `maricopa-weekly-backfill` | Sunday 4 AM Phoenix time | OpenClaw isolated agentTurn |

---

## Featured Articles

### Mechanism

The `articles.is_featured` boolean field controls front-page placement.
Limited to 3 articles at a time.  Admin dashboard has a dedicated Featured
tab with live search.

### Widget

The featured widget on the admin dashboard:
- Shows currently featured articles (with remove buttons)
- Search input (2+ chars) with 300ms debounce → `/admin/articles/search?q=...`
- Click "Feature +" on a result → POST to `/admin/articles/{id}/feature`
- Server enforces the 3-article limit (auto-removes oldest)

---

## Image System

Articles can have a `featured_image` (URL path) and `image_credit`
(attribution text, shown as `<figcaption>` on the article page).

**Sourcing order of preference:**
1. Wikimedia Commons (CC BY / CC BY-SA / public domain)
2. Flickr with a CC license (avoid No-Derivs and Non-Commercial)
3. U.S. government sources (federal is generally public domain)
4. Subject's own press kit or website (check terms)
5. City government websites (use with attribution)

**Prohibited:** AI-generated images, news article photos (copyrighted), stock
photo sites, screenshots of agenda documents.

---

## Port Configuration

| Service | Port | Purpose |
|---|---|---|
| OpenClaw Control Center | 5000 | OpenClaw web UI |
| Flask dev server | 5001 | Local Poliscopic app |
| Gunicorn (production) | 5000 | poliscopic.com behind nginx |

Override Flask port: `FLASK_PORT=9000 python app.py`

---

## Production SSH

```bash
ssh root@poliscopic.com
```

Production app path: `/opt/poliscopic/`
Production database: `/opt/poliscopic/data/maricopa.sqlite`
Production service: `poliscopic.service` (systemd)
Bluesky credentials: `/opt/poliscopic/.env`
