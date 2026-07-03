# WORKFLOWS.md — Process Index

> **This is the hub for every repeatable process in this project.** If you're
> doing something more than once, it should have a documented workflow. If you
> find a better way to do it, update the workflow file and update this index.

---

## Quick Reference

| Workflow | File | When you do it | Last updated |
|---|---|---|---|
| **Daily morning articles** | [DAILY-ARTICLES.md](DAILY-ARTICLES.md) | Automated pipeline at 4 AM, manual review after | 2026-07-02 |
| **Berry verify articles** | [ARTICLES.md](ARTICLES.md), [DAILY-ARTICLES.md](DAILY-ARTICLES.md)§Manual | After pipeline runs (ask agent in Webchat) | 2026-07-02 |
| **Publish + deploy** | [SYNC.md](SYNC.md) | After Berry verify passes, images chosen | 2026-07-02 |
| **Post to Bluesky** | [SOCIAL.md](SOCIAL.md) | After article is published to production | 2026-06-09 |
| **Draft an article** (manual) | [ARTICLES.md](ARTICLES.md), [ARTICLES-SPEC.md](ARTICLES-SPEC.md) | Ad-hoc story from meeting data | 2026-06-30 |
| **Triage sync failures** | [SCRAPERS.md](SCRAPERS.md)§Monitor | Sync report shows failures | _See scripts/sync_monitor.py_ |
| **Record a lesson** | This file §Post-Mortem | Something broke, got fixed, or could have been better | _Always current_ |

---

## How to Use This

1. **Find the workflow you need** from the table above.
2. **Open the linked file.** It will walk you through step by step, with
   checklists, Berry verification steps, and references to related standards.
3. **If the workflow is missing or wrong,** fix it and update the date here.

---

## Workflow Relationships

```
                         ┌───────────────────┐
                         │ Daily sync (3 AM)  │
                         │ scrapes meetings   │ ──→  Meeting data
                         └────────┬──────────┘       in Postgres
                                  │
                                  ▼
                         ┌───────────────────────┐
                         │ Daily pipeline (4 AM) │
                         │ scripts/daily_articles │
                         │ drafts up to 4 articles│
                         └────────┬──────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │ Manual: Berry verify  │ ← Ask agent in Webchat
                         │ via OpenClaw MCP      │   (runs in ~2 sec)
                         └────────┬─────────────┘
                                  │
                                  ▼
                         ┌──────────────────────┐
                         │ Choose images         │
                         │ Review + approve      │
                         └────────┬─────────────┘
                                  │
                                  ▼
                         ┌──────────────────────────┐
                         │ Publish (agent updates   │
                         │ article status, sync.sh, │
                         │ then Bluesky if desired) │
                         └──────────────────────────┘
```

**Database:** Shared Postgres instance on Windows box via Tailscale. No DB sync.
`sync.sh` only pushes code, templates, and static assets.

Each workflow completes a handoff to the next. Article drafting produces a
database record and a draft file. Deployment pushes that record to production.
Social posting tells people it exists.

---

## Post-Mortem / Lesson Capture

Every time something breaks, gets fixed, or reveals a better way to work,
document it here. This is how we improve.

### When to write a lesson

- A workflow step failed (e.g., sync produced bad data)
- A manual workaround became necessary (e.g., template syntax error)
- A script or tool changed behavior (e.g., a scraper platform updated)
- You found a faster way to do something that was slow

### How to write a lesson

1. **Find the workflow file** that the lesson relates to
2. **Add a "Lessons Learned" section** at the bottom (or append to the existing one)
3. **Use this format:**

```markdown
### N. Short Title (YYYY-MM-DD)

**What happened:** One sentence.

**Why:** Root cause in one or two sentences.

**Symptoms:** How you knew something was wrong.

**Fix:** What you did to resolve it.

**Prevention:** What changed in the workflow to stop it from happening again.
```

4. **Update the date** in this file's quick-reference table if the workflow changed

### Example from SYNC.md:

> **One.** TemplateSyntaxError (2026-05-27) — Jinja2 comparisons with dot syntax
> instead of quoted strings caused `/meetings` to 500. Fix: replace `== .variable.`
> with `== 'variable'` in all templates. Prevention: verify with `curl` after every
> template change.

### The rule

**If it happened once, it can happen again. If it can happen again, the workflow
should catch it.** Every lesson should produce either a new checklist item in the
workflow or a new automated check.

---

## Workflow Status

| Workflow | Documented | Automatable | Automated |
|---|---|---|---|
| Article drafting | ✅ ARTICLES.md + ARTICLES-SPEC.md | Partially (Berry verify) | Berry MCP tools only |
| Daily morning articles | ✅ DAILY-ARTICLES.md | Python script (OpenAI API direct) | Cron job (4 AM MST) |
| County boards report | ✅ MARICOPA-REPORT.md | Partially (Berry audit) | Manual PDF download + write |
| Deployment | ✅ SYNC.md | Partially (sync.sh) | sync.sh script |
| Social posting | ✅ SOCIAL.md | Yes (bsky.py) | bsky.py CLI tool |
| Sync monitoring | ⬜ Partial (SCRAPERS.md) | Yes (sync_monitor.py) | Monitor script, no triage doc |
| Lesson capture | ⬜ This section only | No | — |

---

## Revision History

| Date | Change |
|---|---|
| 2026-07-01 | DAILY-ARTICLES.md: removed email send step. Pipeline now saves drafts to DB only — reviewed at /admin/drafts. Updated cron job prompt. |
| 2026-06-30 | Added DAILY-ARTICLES.md, ARTICLES-SPEC.md, MARICOPA-REPORT.md updates; email_article.py script; lesson capture below |

### Lesson: Don't re-hyperlink already-linked content (2026-06-30)

**What happened:** The `hyperlink_body()` function in `scripts/email_article.py` used
regex patterns to find case numbers and source references, then wrapped them in
markdown links. But the report body already had proper hyperlinks inserted during
drafting. The regex patterns were unanchored and matched giant text blocks,
mangling already-linked URLs and sweeping up unrelated content. Tables on mobile
were unreadable because they lacked responsive styles.

**Symptoms:** Broken link formatting in emails ("1 Z2022077](<url|url>)"), tables
that required horizontal scrolling on phones with no scroll wrapper.

**Fix:**
1. Stripped the `_link_source_ref()` and `_link_inline_ref()` regex functions
   from `hyperlink_body()` — they were re-processing already-linked content
2. Added responsive table styles and `<div class="table-wrap">` scrollable
   containers to the HTML email template
3. Made base URL configurable via `POLISCOPIC_BASE_URL` env var
4. Added URL substitution step to replace `DEV_URL` with `BASE_URL` at send time

**Prevention:** The email_article.py script now does three things only: replace
meeting URL labels, substitute production base URL, and wrap tables for mobile.
It does NOT attempt to detect or re-hyperlink content that's already linked.
Source references must be hyperlinked during the drafting stage, not at email
time.
