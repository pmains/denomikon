# WORKFLOWS.md — Process Index

> **This is the hub for every repeatable process in this project.** If you're
> doing something more than once, it should have a documented workflow. If you
> find a better way to do it, update the workflow file and update this index.

---

## Quick Reference

| Workflow | File | When you do it | Last updated |
|---|---|---|---|
| **Draft an article** | [ARTICLES.md](ARTICLES.md) | A newsworthy item is found in meeting data | 2026-06-09 |
| **Deploy to production** | [SYNC.md](SYNC.md) | Ready to publish articles to poliscopic.com | 2026-06-04 |
| **Post to Bluesky** | [SOCIAL.md](SOCIAL.md) | After an article is published | _Not yet extracted_ |
| **Triage sync failures** | [SCRAPERS.md](SCRAPERS.md)§Monitor | Sync report shows failures, stuck meetings, or orphans | _See scripts/sync_monitor.py_ |
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
Sync (cron) ──→ Meeting data in DB ──→ Story idea
                                            │
                                            ▼
                                     ARTICLES.md
                                    (draft + verify)
                                            │
                                            ▼
                                     SYNC.md
                                    (publish to site)
                                            │
                                            ▼
                                     SOCIAL.md
                                    (bluesky posting)
```

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
| Article drafting | ✅ ARTICLES.md | Partially (Berry verify) | Berry MCP tools only |
| Deployment | ✅ SYNC.md | Partially (sync.sh) | sync.sh script |
| Social posting | ⬜ In OPS.md, needs extraction | Yes (bluesky_sync.py) | Production cron |
| Sync monitoring | ⬜ Partial (SCRAPERS.md) | Yes (sync_monitor.py) | Monitor script, no triage doc |
| Lesson capture | ⬜ This section only | No | — |

---

## Revision History

| Date | Change |
|---|---|
| 2026-06-09 | Initial index — created to organize growing set of workflow documents |
