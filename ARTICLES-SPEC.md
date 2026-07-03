# ARTICLES-SPEC.md — Article Drafting Specification

This file documents the specification for drafting and publishing news articles
on Poliscopic. It complements [ARTICLES.md](ARTICLES.md) (workflow walkthrough)
and [STYLE.md](STYLE.md) (editorial standards) by providing a single actionable
checklist with data quality rules, Berry verification guardrails, and image
selection guidelines.

---

## Process Order

When drafting an article, follow this sequence:

1. **Identify the story** — apply the newsworthiness filter from STYLE.md §1
2. **Gather sources** — minimum 3 per STYLE.md §1, unless exception applies
3. **Collect key facts and create Berry evidence spans** — for every source
4. **Draft with inline citation labels** — every factual claim gets a label
5. **Verify with Berry** — run `detect_hallucination_run` on the draft
6. **Revise flagged claims** — iterate until all pass
7. **Review for clarity and readability** — read as a reader, not a verifier
8. **Select and prepare a featured image** — literal > metaphorical > none
9. **Save the draft file** — `drafts/YYYY-MM-DD-slug.md` with YAML frontmatter
10. **Submit to database** — SQLAlchemy, not the admin web form
11. **Deploy** — `sync.sh` per [SYNC.md](SYNC.md)
12. **Post to Bluesky** — per [SOCIAL.md](SOCIAL.md)

---

## Phase 1: Pre-Draft

### 1. Newsworthiness Filter

- [ ] Does the item have a public hearing?
- [ ] A policy change?
- [ ] A significant expenditure?
- [ ] Identifiable winners and losers?
- [ ] Direct impact on residents?
- [ ] A broader public-interest question?

If none of the above, stop. Not every agenda item needs an article.

### 2. Source Gathering — Be Proactive

Minimum three sources (per STYLE.md §1). Do not rely only on what's already in the database — the sync may not have downloaded every supporting document.

#### Check the Database First

- [ ] Agenda item(s) from the database
- [ ] Check `supporting_documents` for existing PDF URLs (staff reports, handouts, exhibits)
- [ ] If a PDF exists but `local_path` is None, download it now via its `document_url`
- [ ] Check for prior meetings on the same case using `c_number` or case number in agenda item text
- [ ] Check for meeting minutes if the meeting has already happened

#### Then Go Beyond the Database

- [ ] Visit the meeting's `source_url` — this is the canonical agenda page
- [ ] Look for staff report links that may not have been captured by the scraper
- [ ] Check for handout memos added after the sync (common for late public comments)
- [ ] For Maricopa County: the Agenda Center HTML view often has "View File" links per item
- [ ] For Legistar (Phoenix/Mesa): legislative detail pages have attachment sections
- [ ] Check for news coverage, press releases, or municipal planning documents

**Exception:** breaking news or very short articles (<200 words) may use fewer sources.

> If a document URL exists but extraction fails (PDF too large, download timeout), note the limitation rather than leaving the source undocumented.

### 3. Berry Evidence Spans — Source Provenance Tagging

Before drafting, open a Berry run and add each source document as an evidence
span. **Tag every span with its source type** in the `meta` parameter:

| `source_type` tag | When to use |
|-------------------|-------------|
| `agenda_item` | From the database agenda item text |
| `staff_report` | Staff report PDF (staff recommendation, not commission action) |
| `meeting_minutes` | Published meeting minutes (actual votes and outcomes) |
| `handout_memo` | Pre- or post-meeting handout memos |
| `news_coverage` | External news articles |
| `statute` | Laws, ordinances, codes |
| `study` | Research reports, data analyses |
| `prior_meeting` | Minutes or agenda from a prior related meeting |

- [ ] Add each source as a Berry evidence span using `berry__add_span` or `berry__add_file_span`
- [ ] Set `meta.source_type` for each span (e.g., `{"source_type": "staff_report"}`)
- [ ] Name spans with descriptive labels you'll reference during drafting
- [ ] Record the SIDs Berry assigns — you'll use these as citation labels

> **Evidence hierarchy:** Every claim type requires a matching evidence type.
>
> | Claim type | Must use | Must NOT use |
> |------------|----------|--------------|
> | Commission voted / decided | meeting minutes, vote record | staff report, agenda |
> | Council adopted ordinance | enacted ordinance, minutes | draft ordinance, recommendation |
> | Budget allocates funds | approved budget, resolution | proposed budget, draft |
> | Lawsuit / court ruling | court docket, official filing | news article citing unnamed source |
> | Staff recommended | staff report | meeting minutes (records the vote, not staff's position) |
> | Something did NOT happen | minutes, video, transcript | agenda (absence ≠ absence) |
> | "Will" / future tense | enacted policy | proposal, draft, staff expectation |
>
> **Source precedence:** When multiple sources exist, prefer adopted minutes >
> enacted ordinance > transcript > staff report > agenda > handout > news coverage.
>
> **Claim strength:** Do not express more certainty than the source. "Staff expects" →
> not "will." "Proposed" → not "approved." "Draft" → not "the plan."
>
> **Negative claims:** If the article says something did *not* happen, the source
> must be minutes, video, or transcript — not an agenda.

---

## Phase 2: Draft + Verify

### 4. Develop Your Thesis

Before drafting, state the thesis in one sentence:

> What happened, why does it matter, and who is affected?

- [ ] Thesis is clear before writing begins
- [ ] Thesis passes the "one sentence" test

### 5. Article Structure

Follow the progressive disclosure pattern from STYLE.md §4–5:

| Element | Purpose |
|---------|---------|
| **Title** | Core action in plain language. No unexplained acronyms. |
| **Summary** | Adds stakes or tension the title didn't provide. One to two sentences. |
| **Lede** (first ¶) | Delivers on the promise. Specific fact, person, or place. |
| **Action** section | What decision was made? |
| **Details** section | How does it work? What does it cost? Where is it located? |
| **Context** section | Why is this happening? Larger trend? |
| **Related Developments** | What else should readers know? |

- [ ] Title → Summary → Lede follows progressive disclosure (each layer adds new info)
- [ ] Body follows Action → Details → Context → Related Developments
- [ ] No closing moral paragraph (per STYLE.md §3)
- [ ] No opaque body codes (resolve `chandler-cc` → "Chandler City Council")

### 6. Draft with Inline Citation Labels

As you write, tag every factual claim with a citation label pointing to your
Berry evidence span SIDs:

```markdown
The council approved a $2.1 million contract for street repairs [S2].
```

- [ ] Dollar amounts get a citation label
- [ ] Dates get a citation label
- [ ] Vote counts get a citation label
- [ ] Addresses get a citation label
- [ ] Quotes get a citation label
- [ ] Ordinance/resolution numbers get a citation label
- [ ] Each item in a list gets its own `[S-label]` (Berry splits lists into sub-claims)

### 7. Meeting Tracker Link

- [ ] Meeting tracker link in the first paragraph where the meeting is introduced
- [ ] Format: `/meetings/{body}/{meeting_id}`
- [ ] Uses the `meeting_id` VARCHAR column, not the internal `id` (INTEGER PK)
- [ ] Item-level links for specific agenda items: `/meetings/{body}/{meeting_id}?item={item_number}`
- [ ] Meeting-level link appears once. Item-level links supplement it.
- [ ] No separate "view agenda" section at the end

### 8. Verify with Berry

- [ ] Use `berry__detect_hallucination_run` (NOT `detect_hallucination` — known MCP bug)
- [ ] Pass the article body as the `answer` parameter
- [ ] Set `require_citations=True`
- [ ] Review results for all sub-claims

| Result | Action |
|--------|--------|
| **PASS** | Move on |
| **FLAG (missing citation)** | Add citation label to that claim |
| **FLAG (not entailed)** | Revise claim or add better evidence |
| **FLAG (verifier_error)** | Check API key and retry |

- [ ] All sub-claims pass before proceeding

### 9. Cross-Source Audit (Expanded)

After Berry verification passes, run a full cross-type audit:

- [ ] Every *commission vote or outcome* claim uses meeting minutes or vote record evidence (not staff report)
- [ ] Every *ordinance adoption* claim uses enacted ordinance or minutes (not draft)
- [ ] Every *budget allocation* claim uses approved budget or resolution (not proposal)
- [ ] Every *staff recommendation* claim uses staff report evidence (not minutes)
- [ ] Every *negative claim* (something did not happen) uses minutes, video, or transcript (not agenda)
- [ ] Every *future/certainty* claim matches the source's level of certainty (draft ≠ enacted, proposed ≠ approved)
- [ ] Every overstatement is flagged: article does not express more certainty than the source

If a mismatch is found, revise the text or change the source. If the gap could
mislead a reader, mark it as a publication blocker.

This catches the same class of error as the Trulieve incident.

### 10. Readability Review

Read the article as a reader, not a verifier:

- [ ] No editorializing (`critical`, `significant`, `notable`, `important`, `worth watching`)
- [ ] No rhetorical questions
- [ ] No "It's not X, it's Y" constructions
- [ ] No excessive em-dashes
- [ ] No repetition
- [ ] Plain English, active voice, short paragraphs
- [ ] Acronyms defined on first reference
- [ ] Inline links use descriptive text (not "click here")
- [ ] Each source linked only once in the body (except meeting tracker in first ¶)

---

## Phase 3: Image Selection

### 11. Featured Image Rules

- [ ] Include a featured image whenever a suitable freely-licensed image exists

**Preferred sources (in order):**
1. [ ] Gage Skidmore (CC BY-SA 4.0)
2. [ ] Wikimedia Commons
3. [ ] Flickr (CC BY or CC BY-SA only)
4. [ ] U.S. government sources
5. [ ] Official press kits with compatible licenses

**When no literal image is available:**
- [ ] A metaphorical image is acceptable if it represents the theme accurately, looks geographically plausible for Arizona, and does not imply it depicts the actual subject
- [ ] AI-generated images are permitted when no freely-licensed alternative exists, provided the image is labeled as AI-generated in `image_credit`

**Never use:**
- [ ] News publication photos (copyright violation)
- [ ] Screenshots of agendas or PDFs
- [ ] Generic stock photography (unless CC-licensed)

**Metadata:**
- [ ] Save to `static/uploads/` with a descriptive filename
- [ ] Set `featured_image` path in the article record
- [ ] Set `image_credit` with attribution text

---

## Phase 4: Publish

### 12. Save Draft File

- [ ] Create `drafts/YYYY-MM-DD-slug.md` with YAML frontmatter
- [ ] Frontmatter includes: `title`, `summary`, `published: false`, `tags`, `slug`
- [ ] Tags include at least one topic tag from STYLE.md §8
- [ ] Tags include a jurisdiction tag where relevant
- [ ] YAML frontmatter is valid

### 13. Submit to Database

- [ ] Use SQLAlchemy against `data/maricopa.sqlite`
- [ ] Do NOT use the admin web form (see TOOLS.md)
- [ ] Create `Article` record with title, slug, summary, body, status, featured_image, image_credit
- [ ] Attach `Tag` records (by ID)
- [ ] Create `ArticleSource` records for each source URL
- [ ] Commit and close the session

### 14. Deploy

- [ ] `./sync.sh --db-only` (or `--full` if images were added)
- [ ] Verify the article renders at its public URL
- [ ] Check that all inline links resolve

### 15. Post to Bluesky

- [ ] Generate skeet per STYLE.md §7 (lead with specifics, create a knowledge gap, 300 char max)
- [ ] `python bsky.py --article <id> --text "..."`

---

## Article Lifecycle Summary

```
Story identified → Sources gathered → Berry spans created (tagged with source_type)
                                         ↓
                                  Draft with [S-label] citations
                                         ↓
                                  detect_hallucination_run
                                         ↓
                                        ┌─── PASS ──→ Readability review
                                        │
                                   ┌────┴────┐
                                   │ FLAGGED │ → Revise → re-verify
                                   └─────────┘
                                         ↓
                                  Image selected
                                         ↓
                                  Draft saved (.md)
                                         ↓
                                  DB submitted
                                         ↓
                                  Deploy → Bluesky
```

---

## Data Quality Rules

- [ ] Verify every address — supporting documents are more reliable than memory
- [ ] Verify every dollar amount against the source document
- [ ] Verify every date against the source document
- [ ] Do not imply causation unless a source explicitly makes the connection
- [ ] Verify vote counts against meeting minutes, not staff reports
- [ ] If a fact cannot be verified, remove it

## Hyperlinking Rules

All source references and meeting links must be clickable.

### Meeting Links
- [ ] Use descriptive link text: `[Planning & Zoning Commission Meeting, June 11, 2026](url)`, not `[View on poliscopic.com](url)`
- [ ] Format: `{Body Name} Meeting, {Date}` — include meeting type if not regular
- [ ] Query the Meeting and PublicBody tables to build labels dynamically

### Source References
- [ ] Every staff report reference should hyperlink to the actual PDF
- [ ] Format: `[PZ Staff Report Z260015, 8 pages](https://www.maricopa.gov/AgendaCenter/ViewFile/Item/10337)`
- [ ] Use case numbers (`Z260015`, `BA260038`) to look up `document_url` from `supporting_documents`
- [ ] `scripts/email_article.py` has reusable `build_case_url_map()` and `hyperlink_body()` functions

## Base URL Configuration

All links in emailed reports must use production URLs, not localhost.

- [ ] **Default (production):** `https://poliscopic.com` — hardcoded in `scripts/email_article.py`
- [ ] **Override for testing:** Set `POLISCOPIC_BASE_URL` env var (e.g., `http://127.0.0.1:5001`)
- [ ] `scripts/email_article.py` automatically replaces `127.0.0.1:5001` in the body with `BASE_URL` at send time
- [ ] The database keeps local URLs for admin viewing — the substitution happens only when emailing

## Berry Provenance Rules (Guardrails)

- [ ] Every evidence span has a `source_type` in `meta` (`staff_report`, `meeting_minutes`, `agenda_item`, etc.)
- [ ] Staff recommendation claims use `staff_report` spans only
- [ ] Commission action claims use `meeting_minutes` spans only
- [ ] Ordinance adoption claims use enacted ordinance or minutes spans only
- [ ] Budget allocation claims use approved budget/resolution spans only
- [ ] Negative claims (something did not happen) use minutes, video, or transcript spans — not agenda
- [ ] Claim strength matches source strength ("proposed" → not "approved")
- [ ] Cross-type claims are flagged before publishing
- [ ] `detect_hallucination_run` is used (not `detect_hallucination`)

## Common Mistakes

- **Conflating staff recommendation with commission vote** — the staff report says what staff wants; the minutes say what happened. These are different. See Trulieve incident.
- **Berry list citation bug** — each item in a list needs its own `[S-label]` next to it. A single citation at the end of a list won't carry through.
- **API key encoding** — OpenAI API keys must be pure ASCII. Masked display strings with `…` (U+2026) cause `UnicodeEncodeError`.
- **Opaque body codes** — `chandler-cc` is not public-facing text. Always resolve to "Chandler City Council."
