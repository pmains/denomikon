# SYNC.md — Deployment to Poliscopic.com

## Quick Reference

```bash
./sync.sh                                    # Full sync (db + code + templates)
ssh root@poliscopic.com "systemctl restart poliscopic.service"
ssh root@poliscopic.com "curl -s -o /dev/null -w '%{http_code}' http://localhost:5000"
```

## Pre-flight Checklist

- [ ] `data/maricopa.sqlite` has > 10 meetings (sync.sh enforces this automatically)
- [ ] No old scrapes or test databases leaked into production (see "Three-DB Policy" in AGENTS.md)
- [ ] Static assets (uploaded images in `static/uploads/`) are present
- [ ] Run `git diff --name-only HEAD` to verify which files changed
- [ ] If templates changed: verify the deployed template renders correctly (no TemplateSyntaxError)
- [ ] If routes changed: check for import errors or missing endpoint references

## Sync Process

### 1. Run sync.sh

```bash
./sync.sh
```

This does:
- Verifies local database has >= 10 meetings
- Backs up production database to `maricopa.sqlite.bak.{timestamp}`
- Creates local snapshot in `snapshots/` (pruned to 10 latest)
- Rsyncs `app.py`, `requirements.txt`, `scripts/`, `static/`, `templates/`, `data/`, `routes/`

**CRITICAL:** sync.sh uses `--exclude` for data directories that shouldn't be synced
(permit-activity/, snapshots/, agendas/, agenda-items/, supporting-materials/).
It does NOT use `--delete`. Server-side files like `.venv/`, node configs, etc.
are preserved.

### 2. Restart the web service

```bash
ssh root@poliscopic.com "systemctl restart poliscopic.service"
```

### 3. Cache management

```bash
# Flush the regular Flask cache (article pages, front page, meeting lists)
ssh root@poliscopic.com "curl -s http://localhost:5000/cache/clear"
# ^ This route is defined in routes/__init__.py and clears the articles cache

# DO NOT flush the permit cache. Permit data is expensive to rebuild.
```

### 4. Verify the site is up

```bash
ssh root@poliscopic.com "curl -s -o /dev/null -w '%{http_code}' http://localhost:5000"
# Should return 200
```

Also verify remotely:
```bash
curl -s -o /dev/null -w '%{http_code}' https://poliscopic.com
# Should return 200
```

## Lessons Learned

### 1. TemplateSyntaxError (May 27, 2026)
Jinja2 comparisons with `.variable.` syntax (dot-delimited) instead of `'variable'`
(quoted strings) caused the /meetings page to error. The error was:
```
jinja2.exceptions.TemplateSyntaxError: unexpected '.'
```
Fix: Search for `== .variable.` patterns in all templates and replace with `== 'variable'`.
After fixing, verify with `curl` before deploying.

### 2. Database Environment Leak (May 2026 — production outage)
A previous change made `get_engine()` re-read `os.environ.get("DATABASE_URL")` at
runtime so test fixtures could swap databases. This caused a production outage:
test modules leaked `DATABASE_URL` into the environment, and when Flask started
in the same terminal session, it picked up the stale path and connected to a test
database — returning 4 meetings instead of the full dataset.

Fix: `get_engine()` is now a module-level constant. Use `set_database_url()` for
test database switching instead of modifying `os.environ`.

### 3. Empty Sync After Safeguard Failure (2026)
The sync safety check (meeting count < 10 → abort) exists because an earlier sync
attempt pushed an empty database to production. Always verify the local database
has real data before running sync.sh.

### 4. Template Changes Need Both Routes and Templates
If you modify `routes/articles.py` (e.g., front page query logic), you must also
rsync `routes/`. The sync.sh does this in a separate rsync command because of
trailing-slash behavior.

### 5. Static Assets Must Be Verified
New uploaded images in `static/uploads/` are synced as part of the `static/`
directory. After sync, verify the images are present on the server:
```bash
ssh root@poliscopic.com "ls -la /opt/poliscopic/static/uploads/"
```

### 6. Service Restart After Sync
The sync.sh script does NOT restart the web service. This is intentional — you
may want to run multiple syncs (e.g., templates first, then code) before
restarting. But **always restart** as a separate step, or the old code will still
be running.

### 7. Cache Granularity
The articles cache is coarse — clearing it resets all cached pages. There's no
per-article cache invalidation yet. The permit cache is expensive to rebuild
and should never be cleared during a normal deployment.

### 8. Database Path Verification After Deploy
After sync and restart, verify the production database has the expected meeting
count and the latest articles:
```bash
ssh root@poliscopic.com "sqlite3 /opt/poliscopic/data/maricopa.sqlite \
  'SELECT COUNT(*) FROM meetings; SELECT id, title FROM articles ORDER BY id DESC LIMIT 5;'"
```
