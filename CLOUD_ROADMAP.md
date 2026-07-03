# Cloud Roadmap — Maricopa Governance Data Platform

> Goal: Make the agent, database, and articles accessible to a volunteer team
> with minimal recurring cost — **with strict dev/prod separation and a single
> gatekeeper for production pushes.**

---

## Design Principle

> **Only Pete pushes to production. Invalid data never reaches the public site.**

Volunteers work in a local/dev environment. All changes flow through a single
gate: Pete's review and explicit push. This is the same pattern `sync.sh`
already enforces — it remains the **only** way to push data to poliscopic.com.

> **The Slack-facing gateway has no git access.** The environment volunteers
> interact with through Slack can query the database, run scrapes, and answer
> questions — but cannot push code, create PRs, trigger deploys, or touch any
> git remotes. Git operations are laptop-only.

---

## Current State

| Component | Location | Accessible to volunteers? |
|---|---|---|
| OpenClaw agent (me) | Pete's MacBook Air | Slack chat only (works now) |
| Flask web app | Pete's MacBook Air, `127.0.0.1:5001` | No |
| SQLite database | `data/maricopa.sqlite` | No (local file) |
| Scrapers / sync scripts | Pete's MacBook Air | No (local cron) |
| Articles | SQLite + Flask admin UI | No |
| `poliscopic.com` | Namecheap parked domain | Nothing behind it yet |

---

## Phase 1 — Static Articles on CloudFlare Pages ($0/mo)

### What

Render published articles to static HTML and serve them from CloudFlare's edge
network. This is the **public face** of the site — no server needed.

### Architecture

```
Dev (Pete's laptop):
  SQLite ─→ export script ─→ /tmp/articles/

Production (CloudFlare Pages):
  git push (Pete only) ─→ CF Pages build ─→ CDN ─→ visitor
```

### What

1. A Python script queries published articles from SQLite
2. Renders each to a standalone HTML page (using existing Flask templates)
3. Generates index pages: front page (featured), tag pages, per-body pages
4. Outputs to a directory that can be pushed to the CF Pages git repo

### Separation

| Layer | Access |
|---|---|
| Export script runs locally | Pete |
| Git push to CF Pages repo | Pete (only authorized deployer) |
| Published pages on CDN | Everyone (read-only) |

### Cost

| Item | Cost |
|---|---|
| CloudFlare Pages free tier (500 builds/mo) | $0 |
| Bandwidth (unlimited) | $0 |
| **Total** | **$0/mo** |

### What this unblocks

- Articles are publicly readable without any server
- Links can be shared (`poliscopic.com/articles/...`)
- CDN-cached, fast anywhere
- Poliscopic.com is no longer a parked domain

### Effort

One-time: build the export script (~4h). Templates reuse existing Flask CSS.

---

## Phase 2 — Production Database + Flask in the Cloud (~$6/mo)

### Why

A permanent home for the production database so syncs don't depend on Pete's
laptop being awake. Also gives volunteers a place to draft articles via the
admin UI without touching production.

### Dev / Prod split

```
┌─ Dev (Pete's laptop) ─────────────────────┐
│  data/maricopa.sqlite   (local SQLite)     │
│  Flask dev server       127.0.0.1:5001     │
│  Scrapers / sync        daily cron         │
│  Volunteer article drafts                  │
└────────────────────────────────────────────┘
           │
           │ Pete runs ./sync.sh (only path to prod)
           ▼
┌─ Prod (DigitalOcean droplet) ─────────────┐
│  /opt/poliscopic/data/maricopa.sqlite      │
│  Flask (gunicorn + nginx)                  │
│  Public article CDN (CloudFlare Pages)     │
└────────────────────────────────────────────┘
```

### How it works

| Activity | Environment | Who |
|---|---|---|
| Scrape new meetings | Dev (laptop cron) | Automated |
| Draft / edit articles | Dev (Flask admin UI) | Volunteers + Pete |
| Review & approve articles | Dev (Flask admin UI) | Pete |
| Sync DB to production | `./sync.sh` | Pete only |
| Deploy static articles | git push to CF Pages | Pete only |
| Public reads | Prod (CF Pages + droplet) | Anyone |

### What the production droplet runs

- `gunicorn` serving the Flask app (read-only article routes, plus admin API
  restricted to Pete's IP / Cloudflare Access)
- `nginx` reverse proxy + TLS termination
- SQLite database (read by Flask, written only by `sync.sh`)

### Migration plan

1. Provision a $6 DigitalOcean droplet (1 GB RAM, 25 GB SSD)
2. Install Python, nginx, gunicorn, SQLite
3. Deploy Flask app behind nginx
4. Point `poliscopic.com` DNS at CloudFlare → proxies the droplet
5. Restrict admin routes (`/admin/*`) with Cloudflare Access (free for up
   to 50 users) so only Pete can log in
6. `sync.sh` stays the single deploy path — runs from Pete's laptop, `rsync`s
   the DB to the droplet, then triggers the static article export

### Cost

| Item | Cost |
|---|---|
| DO $6 droplet | $6 |
| CloudFlare Pages (articles) | $0 |
| Cloudflare Access (admin auth, free for ≤50 users) | $0 |
| Domain (already owned) | $0 |
| **Total** | **~$6/mo** |

---

## Phase 3 — OpenClaw Gateway in the Cloud (~$6-12/mo extra)

### Why

Right now I (the agent) run on Pete's laptop. If it sleeps or loses internet,
the bot goes offline. Volunteers also can't trigger scrapes or run diagnostics.

### Options

| Setup | Monthly | Notes |
|---|---|---|
| **Colocate** on the $6 droplet (Phase 2) | +$0 | Tight — 1 GB RAM shared with Flask + Playwright |
| **Upgrade to $12 droplet** (2 GB RAM) | +$6 | Comfortable headroom for gateway + Flask |
| **Separate $12 droplet** | +$12 | Fully isolated, can restart independently |

The $12 colocated setup is the sweet spot. The gateway needs ~256-512 MB plus
headroom for Playwright browser processes during scrapes.

### What moves to cloud

- OpenClaw gateway (systemd service on the droplet)
- Scraper scripts (run as subprocesses via the gateway)
- Cron jobs (daily sync, doc check, doc check seeding)
- Slack connection (already configured)

### What stays on Pete's laptop

- Development editing (files edited locally, deployed via git)
- Volatile experiments and scraper development
- Dev SQLite database (for testing scrapes before they hit prod)
- The `sync.sh` push-to-prod authority

### Security boundary

```
Volunteer
  │
  ▼  (Slack message)
Gateway (droplet)
  │
  ├── Reads prod DB (read-only for queries)
  ├── Runs scrapes → writes to prod DB
  ├── No git credentials
  ├── No SSH keys
  └── Can sync.sh?  →  NO. sync.sh only runs from Pete's laptop.
```

| Capability | Gateway in cloud | Pete's laptop |
|---|---|---|
| Query production DB | ✅ | Via sync.sh |
| Run scrapes | ✅ | ✅ (dev) |
| Answer Slack questions | ✅ | — |
| Create git commits | ❌ (no git creds) | ✅ |
| Push to CF Pages | ❌ | ✅ |
| Run sync.sh | ❌ | ✅ (sole gate) |
| Create PRs / issues | ❌ | ✅ |

The gateway on the droplet can read and write to the production database
(for scraping, querying, article management), but **cannot push data from dev
to prod**. That authority stays in `sync.sh` on Pete's machine.

Critically, the Slack-facing environment has **no git credentials at all** —
no SSH keys, no GitHub tokens, no CF API tokens for Pages deploys. If the
droplet is compromised, git remains untouched.

### Cost (colocated on $12 droplet)

| Item | Cost |
|---|---|
| DO droplet (2 GB) — hosts Flask + gateway | $12 |
| CloudFlare Pages | $0 |
| Domain | $0 |
| **Total** | **$12/mo** |

---

## Phase 4 — Volunteer Workflow ($0 extra)

### Dev environment for volunteers

Volunteers get a **dev-only** environment. They never touch production.

| Activity | How | Environment |
|---|---|---|
| Chat with the agent | Slack #maricopa | The agent runs in cloud (Phase 3) |
| Propose a new jurisdiction | Agent creates a draft issue | GitHub |
| Submit scraper code / fixes | PR against `maricopa-agendas` | GitHub → CI validates |
| Draft an article | Markdown in `drafts/` (git) or dev Flask admin | Local / dev only |
| Request article review | Slack ping + agent previews | Dev |
| **Publish to production** | Pete runs `sync.sh` + `git push` | **Pete only** |

### How volunteers interact with the gateway (Slack only, no git)

When a volunteer asks about a scraper bug, a new jurisdiction, or an article
review through Slack — the gateway answers, investigates the DB, runs
diagnostics, and drafts text. But it **cannot create PRs**, push code, or
deploy. The gateway has no git credentials.

### Git-based contribution flow (Pete-only on laptop)

```
Volunteer opens PR (via GitHub web)
       ↓
CI runs tests + lint (GitHub Actions, free)
       ↓
Pete reviews
       ↓
Merge to main (Pete on laptop)
       ↓
Pete: pull main → test locally → run sync.sh → prod
```

The gateway never touches this pipeline. Volunteers interact with it through
Slack for questions and diagnostics, but their code contributions go through
normal GitHub PRs that Pete gates.

### What's needed

- GitHub repo (already have one) with CI workflow
- `CONTRIBUTING.md` explaining the dev/prod split and the no-git gateway
- Brief onboarding doc for new volunteers

### Cost

| Item | Cost |
|---|---|
| GitHub (free tier) | $0 |
| CI minutes (free, 2000/mo) | $0 |
| Slack (free tier) | $0 |
| **Total** | **$0/mo** |

---

## Full Budget Summary

| Phase | What | Monthly | One-time setup |
|---|---|---|---|
| 1 | Static articles on CloudFlare Pages | $0 | ~4h (export script) |
| 2 | Production DB + Flask on $6 DO droplet | $6 | ~4h (deploy & configure) |
| 3 | OpenClaw gateway on same droplet, upgraded to $12 | +$6 | ~2h (systemd + config) |
| 4 | Volunteer workflow tooling | $0 | ~2h (docs, CI config) |
| **Total** | **Full stack** | **$12/mo** | **~12h** |

Cheapest viable: **$6/mo** (articles + prod DB, agent stays tethered to laptop).
Full volunteer-ready: **$12/mo** (all phases, gateway in cloud, dev/prod wall).

---

## Decisions Made

| Decision | Choice |
|---|---|
| **Who can push to production?** | Pete only, via `sync.sh` and git push to CF Pages |
| **Volunteer access to production?** | None. Volunteers work in dev/git only. |
| **Gateway authority?** | Gateway is cloud-hosted, reads/writes prod DB for scrapes & queries, CANNOT run `sync.sh`, CANNOT access git |
| **Dev database?** | Local SQLite on Pete's laptop. Separate from production. |
| **Production database location?** | DO droplet (Flask sidecar) |

## Open Questions

1. **Static articles first?** Fastest win. Detaches read traffic from the Flask app.
2. **Agent colocated or separate?** $12 colocated (recommended) vs. splitting to two droplets for full isolation.
3. **Sync.sh still the gatekeeper?** Current model fits perfectly — Pete runs it from local. Prod DB is written only through `rsync` from `sync.sh`. The gateway can scrape directly to prod DB, but dev→prod pushes are laptop-only.
4. **Namecheap → CloudFlare DNS?** `poliscopic.com` needs nameservers pointed at CloudFlare (free plan).
5. **Droplet IP protection?** Cloudflare Access for admin routes, or just IP-restrict + Tailscale?
