# ARTICLES.md — Article Drafting Workflow

> **This is a workflow file.** It describes the step-by-step process for drafting
> articles. For editorial standards (voice, sourcing rules, values), see
> [STYLE.md](STYLE.md). For publishing and deployment, see
> [OPS.md](OPS.md) and [SYNC.md](SYNC.md).

---

## Overview

Every article goes through three phases:

1. **Pre-draft** — decide whether an item is worth covering, collect sources
2. **Draft + verify** — write with inline citations, verify every claim against evidence via Berry
3. **Publish** — save to database, deploy, post to Bluesky

The verification step (phase 2) is the core of this workflow. It uses **Berry**
(Hallbayes) to check whether each factual claim in the article is supported by
the evidence it cites. Unsupported claims must be revised or removed before
publishing.

---

## Before You Start

- [ ] Read the newsworthiness criteria in [STYLE.md §1](STYLE.md#1-editorial-process)
- [ ] Confirm the meeting has been synced and its agenda items are in the database
- [ ] Gather source documents (staff reports, supporting docs, prior meetings)
- [ ] Open a Berry run for this article

---

## Phase 1: Pre-Draft

### 1. Identify the story

Not every agenda item needs an article. Apply the STYLE.md filter: does it have
a public hearing, a policy change, a significant expenditure, identifiable
winners and losers, or a broader public-interest question? If not, stop.

### 2. Gather sources

Minimum three, per STYLE.md:

- The agenda item and its supporting documents
- Staff reports (linked from agenda items)
- Prior meetings on the same topic
- Government documents, statutes, or studies
- News coverage (when relevant)

**Exception:** breaking news or very short articles (<200 words) may use fewer.

### 3. Collect key facts from sources

Extract and record the factual material you'll need:

- Dollar amounts, dates, vote counts, addresses
- Ordinance and resolution numbers
- Quotes and attributions
- Agency actions and statutory references

**Berry step:** Add each source document as an evidence span in the active Berry
run using `add_span` or `add_file_span`. Name the spans with short labels
(`ev-source-1`, `ev-staff-report`) that you'll reference during drafting.

---

## Phase 2: Draft + Verify

This is an iterative loop. Write a section, verify it, fix what's flagged, move on.

### 4. Develop your thesis

Before drafting, know what point the article makes. The thesis is your answer to:

> What happened, why does it matter, and who is affected?

If you can't state the thesis in one sentence, you're not ready to draft.

### 5. Draft with inline citation labels

Write the article following STYLE.md's structure (Action → Details → Context →
Related Developments). As you write, tag every factual claim with a citation
label that points to one of your evidence spans:

```markdown
The council approved a $2.1 million contract for street repairs [ev-staff-report].
```

The label in `[brackets]` corresponds to a span you added in step 3. Use
descriptive labels — `[ev-staff-report]`, `[ev-agenda-item-4]`, `[ev-resolution]`.

**Rules:**
- Every factual claim gets a citation label. No exceptions.
- Dollar amounts, dates, vote counts, addresses, quotes — all labeled.
- Link text in the final article goes where the label is. See STYLE.md §6 for
  inline linking rules.

### 6. Verify with Berry

At minimum, verify in these situations:

- **After drafting** — full article verification before review
- **After a significant edit** — if you substantially rewrote a section
- **When uncertain** — if a claim feels like it might not be fully supported

**Workflow:**

```
1. Create_claim for each factual claim in the draft
2. Link_claim_evidence to connect each claim to its evidence span
3. Run audit_trace_budget_run to score every claim against its evidence
4. Review the results:
   ├── CLAIMS PASS → proceed
   └── CLAIMS FLAGGED → go to step 7
```

**What Berry's verdict means:**

| Result | Meaning | Action |
|---|---|---|
| **PASS** | The cited evidence carries the claim | Move on |
| **FLAG (no evidence)** | Claim has no citation label | Add a label or remove the claim |
| **FLAG (not entailed)** | The evidence doesn't support the claim | Revise the claim or add better evidence |
| **FLAG (uncited span)** | The label points to something that wasn't added as evidence | Fix the label or add the span |

### 7. Revise flagged claims

For each flagged claim, decide:

- **Fix the claim** — rewrite to match what the evidence actually says
- **Add better evidence** — find a source that supports the claim and add it as a span
- **Remove the claim** — if neither option works, the claim doesn't belong in the article

After revision, re-verify the affected claims. Repeat until all pass.

### 8. Review for clarity and readability

Once verification passes, step back from the Berry check and read the article as
a reader. Apply the STYLE.md rules:

- [ ] Every factual claim has a citation label?
- [ ] Citation labels map to sources in the sources box?
- [ ] No editorializing or rhetorical questions?
- [ ] No opaque body codes (`chandler-cc` → "Chandler City Council")?
- [ ] Progressive disclosure in title → summary → lede?
- [ ] Meeting tracker link in first paragraph?
- [ ] Item-level links for specific agenda items?
- [ ] No closing moral paragraph?

---

## Phase 3: Publish

### 9. Save the draft

Create a draft markdown file in `drafts/` with YAML frontmatter:

```markdown
---
title: "Glendale renews regional bus agreement"
summary: "Glendale renewed its agreement with Valley Metro for the 14th time..."
published: false
tags: Transportation, Budget
slug: 2026-06-09-glendale-valley-metro-agreement
---

Article body with [citation labels][ev-source] goes here.
```

The draft file is the recoverable, versionable record. It goes in git.

### 10. Submit via database

Use SQLAlchemy against `data/maricopa.sqlite` to create or update the Article
record. DO NOT use the admin web form — see TOOLS.md.

```python
from db.newsroom import Article, ArticleSource
from db.core import get_engine, Session

engine = get_engine()
s = Session(engine)

article = Article(
    title="...",
    slug="...",
    summary="...",
    body="...",  # Article body with inline links, not citation labels
    status="published",  # or "draft" for review
    featured_image="/static/uploads/....jpg",
    image_credit="Photo by..."
)
article.tags.append(tag)  # Tag objects from tags table
s.add(article)

# Add sources
for url, title in sources:
    s.add(ArticleSource(article_id=article.id, source_url=url, item_title=title))
s.commit()
s.close()
```

### 11. Deploy

Follow the deployment checklist in [SYNC.md](SYNC.md):

- [ ] `./sync.sh --db-only` (or full sync if new images)
- [ ] Restart the web service
- [ ] Verify the article renders at its public URL

### 12. Post to Bluesky

Follow the social posting workflow in [SOCIAL.md](SOCIAL.md):

- [ ] Draft a skeet following STYLE.md §7 rules
- [ ] Post via admin dashboard or `bluesky_sync.py --post <article-id>`

---

## Article Lifecycle

```
Scraper → Meeting identified → Thesis developed → Draft written
                                                        ↓
        ┌──────────────────── Berry verify ───────────→ Revise
        │                                                    ↓
        │                                           Verify passes
        │                                                    ↓
        └──────────────────────────────────→ Review readability
                                                     ↓
                                              Draft saved (.md)
                                                     ↓
                                              DB submitted
                                                     ↓
                                              Deploy → Bluesky
```

---

## Revision History

| Date | Change |
|---|---|
| 2026-06-09 | Initial workflow — added Berry verification loop |
