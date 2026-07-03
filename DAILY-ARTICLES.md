# DAILY-ARTICLES.md — Morning Article Pipeline (4 AM → 4:30 AM)

This document is the specification for the automated daily article pipeline.
It runs as a **Python script** (`scripts/daily_articles.py`) triggered by a cron
job at 4 AM MST. The script runs locally, calls the OpenAI API directly for
article drafting, and saves up to 4 draft articles to the database by ~4:30 AM.

**Database:** Writes to the Postgres `poliscopic_dev` database (shared with the
Flask dev server at `127.0.0.1:5001`). Production and dev share the same Postgres
instance — the production site reads from `poliscopic`, dev writes to
`poliscopic_dev`. Code/assets are synced via `sync.sh`; the database is not.

**Why this architecture:** The old agent-based pipeline used DeepSeek via an
OpenClaw isolated session, which consistently timed out after ~6 minutes —
too short for the full pipeline. By running operations locally in Python
and only connecting to OpenAI when drafting articles, we avoid any agent
session timeout issues. The cron job's agent session just wraps a shell
command that completes in seconds.

**What we lose:** No Berry verification (MCP tools not available from Python).
Articles are saved as `status="draft"` for Pete's review. The quality gates
shift from automated Berry checks to editor review.

## Manual Steps After the Pipeline

After the pipeline runs (check Slack for the summary), the manual workflow is:

1. **Berry verify** — ask the OpenClaw agent in Webchat to Berry-verify the
   day's draft articles. Berry MCP runs instantly (~2 seconds) from the main
   session. The agent reports which claims pass/fail and recommends fixes.
2. **Choose images** — select a feature image for each article. Images live
   in `static/uploads/` and `static/images/`.
3. **Review + publish** — when you're satisfied, ask the agent to publish.
   This updates article status to `"published"`, runs `sync.sh` to push code
   and assets, and optionally posts to Bluesky.

**Writing rules sourced from:** [STYLE.md](STYLE.md) (sourcing, avoid section,
article structure, titles/summaries/ledes, inline linking, tags) and
[EDITORIAL.md](EDITORIAL.md) (editorial values, scope, newsworthiness filter).

---

## Architecture

```
cron trigger (4 AM) ──→ daily_articles.py ──→ OpenAI API (drafting)
                           │                        │
                           ▼                        ▼
                     DB queries            Articles generated
                     (SQLAlchemy)          (gpt-4o-mini)
                           │                        │
                           └───────┬───────────────┘
                                   ▼
                           Hyperlink + save to DB
                                   │
                                   ▼
                           Slack summary (via cron delivery)
```

All operations run in a single Python process:
- DB queries via SQLAlchemy against `data/maricopa.sqlite`
- LLM calls via `openai` Python library (using `OPENAI_API_KEY` from `.env`)
- No agent session model calls — the only network call to an LLM is through the script

---

## Pipeline Overview

```
Stage 1: Story Selection  ──→  Query DB for recent/upcoming meetings
    │                              Generate full candidate list (all meetings)
    │                              Score each by newsworthiness (0-7 scale)
    │                              Apply diversity + dedup rules
    │                              Select top 4 items
    ▼
Stage 2: Drafting          ──→  For each item:
    │                              Gather context (agenda text, docs, prior history)
    │                              Call OpenAI API with structured context
    │                              Get back JSON with title, summary, body
    ▼
Stage 3: Hyperlink         ──→  Convert source references to inline links
    │                              Replace [S-label] tags with meeting page links
    │                              Hyperlink staff report PDFs
    ▼
Stage 4: Save to DB        ──→  Create Article record (status="draft") with tags
    │                              Auto-detect topic tags from body keywords
    │                              Create ArticleSource records
    ▼
Complete                    ──→  Summary message posted to #maricopa via cron delivery
```

---

## Stage 1: Story Selection

### Database Query

Queries meetings in `data/maricopa.sqlite` that are either:
- Recent past (last 7 days) with interesting decisions
- Upcoming (next 3 days) with noteworthy public hearings
- **Today's meetings** — always checked first

Uses `agenda_item_text` and `agenda_item_title` fields to identify items.
Skips items where both fields are empty.

### Administrative Item Filter

The following item types are filtered out automatically:
- Minutes approval, roll call, adjournment
- Call to order, pledge of allegiance, land acknowledgement
- Invocation, moment of silence
- Consent agenda / routine approvals
- Recognitions, introductions, closed session
- Recess, calendar review, council/staff comments
- Director's report, chair's report

### Newsworthiness Scoring (0–7 scale)

Each candidate is scored using keyword matching:

| Criterion | Matches | Points |
|-----------|---------|--------|
| Public hearing | "public hearing" in text | +1 |
| Policy change | zoning amendment, ordinance, code change, general plan, comprehensive plan | +1 |
| Winners/losers | rezone, rezoning, variance, conditional use, development agreement, special use, subdivision, appeal | +1 |
| Dollar amount | Contains `$` amount (e.g., $1.6M, $500K) | +1 |
| Resident impact | housing, affordable, water, transportation, traffic, safety, park, school, road, utility, fee, tax, budget | +1 |
| Timeliness | Meeting is TODAY or TOMORROW | +1 |
| Follow-up potential | Continued from, continuance, prior meeting, previously, rescheduled, appeal of | +1 |

Max score = 7. Candidates scoring 0 are discarded.

### Deduplication

1. **Case number dedup**: If two candidates share the same case number (e.g., Z260015), only the first wins.
2. **Existing articles**: Checks `ArticleSource` records for prior coverage of the same case number — skips if already covered.
3. **Similar title dedup**: Same text at different bodies is deduplicated.

### Selection Rules

1. **Top 4 by score.** Select the 4 highest-scoring candidates.
2. **Geographic diversity.** No more than 2 articles from the same jurisdiction.
3. **Meeting diversity.** No more than 2 articles from the same meeting.

---

## Stage 2: Drafting

For each selected item:

### Context Building

The script builds a structured context string including:
- Meeting context: jurisdiction, body name, date, type, source URL
- Agenda item: title and full text
- Supporting documents: file names (if found in DB)
- Prior case history: all appearances of the same case number across bodies
- Minutes URL: if available

### OpenAI API Call

System prompt instructs the model (default: `gpt-4o-mini`) to write
articles following [STYLE.md](STYLE.md) writing craft rules. The prompt
explicitly enforces:

1. **500-600 words** — the model is told the target length.
2. **Structure**: Action (lede) → Details → Context → Related Developments.
3. **Progressive disclosure**: Title → Summary → Lede each add new info.
   Summary creates a knowledge gap / stakes question; Lede delivers on it.
4. **Inline linking**: Meeting URL paths are provided in context. The model
   writes markdown links on descriptive text (`[Chandler City Council Meeting, June 11](...)`), not bare URLs or "click here" labels.
5. **Writing rules from STYLE.md**: plain English, active voice, short paragraphs,
   no editorializing, no rhetorical questions, no closing moral, no repetition.
6. **Evidence matching**: Agenda text → "staff recommends" / "will consider" —
   not "approved" or "voted" unless minutes are provided.

### Return Format

```json
{
  "title": "...",
  "summary": "...",
  "body": "..."
}
```

The model is configured with `response_format={"type": "json_object"}` for reliable parsing.

---

## Stage 3: Hyperlinking

The hyperlink function does minimal cleanup since the model is expected to
write inline links during drafting:

1. **Strip stray [S-label] artifacts** — removed if any remain.
2. **Strip bad link labels** — removes "View source:", "Source:", "Agenda:",
   "Click here:" link labels if the model produced them.
3. **Fallback link** — if no `/meetings/` link was written at all, adds one
   at the end of the first paragraph.
4. **Whitespace cleanup** — removes double spaces and trailing spaces before
   punctuation.

No "View source" call-out is appended. Source links are embedded in the
narrative text per STYLE.md §5.

---

## Stage 4: Save to Database

### Article Record

```python
Article(
    title=draft.title,
    slug=f"{date}-{slugified-title}",
    summary=draft.summary,
    body=body,  # hyperlinked
    status="draft",
    featured_image="",
    image_credit="",
)
```

### Tags

Auto-detected from body/summary keywords:
- **Topic tags**: matched against keyword lists for budget, housing, zoning,
  development, transportation, water, environment, public-safety, parks,
  economy, education, government, health, data-centers
- **Jurisdiction tag**: automatically derived from the meeting's jurisdiction

### ArticleSources

One record per source:
- The agenda item (source_type="agenda")
- Each staff report with a document URL (source_type="staff_report")

### Database Connection

Uses `data/maricopa.sqlite` with models from `scripts/db/newsroom.py` and
`scripts/db/models.py`. Requires `POLISCOPIC_DB_TIER=development`.

---

## Error Handling

| Failure | Action |
|---------|--------|
| Story selection finds 0 candidates | Skip, output: "No newsworthy items found today." |
| All candidates deduplicated | Skip, output: "All items already covered in existing articles." |
| OpenAI API call fails for 1 article | Retry once. If both attempts fail, skip that article. |
| DB save fails | Retry once. If still fails, skip that article. |
| All 4 articles fail | Output: "Pipeline produced 0 articles. See logs." |

---

## Timing

| Time (MST) | Milestone |
|------------|-----------|
| 4:00 AM | Pipeline starts |
| 4:05 AM | Story selection complete |
| 4:20 AM | Drafting + hyperlinking complete (up to 4 articles) |
| 4:25 AM | DB save complete |
| 4:30 AM | Deadline — summary posted |

---

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | (from .env) | OpenAI API key for article drafting |
| `DAILY_ARTICLE_MODEL` | `gpt-4o-mini` | Model used for drafting |
| `POLISCOPIC_DB_TIER` | `development` | Which database to use |

---

## Revision History

| Date | Change |
|---|---|
| 2026-07-02 | Complete rewrite: replaced agent-based pipeline with Python script. Script calls OpenAI API directly, avoids DeepSeek 6-min timeout. No Berry verification. |
| 2026-07-01 | Removed email send step. Pipeline saves drafts to DB only. |
| 2026-06-30 | Initial document — agent-based pipeline with Berry verification and style critic subagent. |

## Lessons Learned

### 1. DeepSeek Agent Sessions Time Out at ~6 Minutes (2026-07-02)

**What happened:** The agent-based pipeline used a DeepSeek model in an isolated
cron session. Every long-running job — article pipeline, weekly housekeeping,
housing review, weekly backfill — failed with "Request was aborted" after
~384 seconds (6m24s).

**Why:** DeepSeek's API drops long-running connections. The agent session
keeps a model conversation open, and every tool call goes through the same
connection. After ~6 minutes of sustained use, the connection is terminated.

**Symptoms:** Jobs that run shell scripts and exit in <10 seconds (sync-launcher,
sync-checker) worked fine. Jobs that require back-and-forth model conversation
for >6 minutes consistently failed.

**Fix:** Replaced the agent-based pipeline with a Python script that:
1. Runs all data operations locally via SQLAlchemy
2. Calls the OpenAI API directly (not through the agent's model session)
3. Calls OpenAI only during the drafting phase — no long-running conversation

The cron job now runs `python scripts/daily_articles.py 2>&1` directly — the
agent session finishes in ~5 seconds (just wrapping the shell command), so
the DeepSeek timeout is never triggered.

**Prevention:** Any future cron job that needs LLM calls should make direct
API calls from a Python script, not rely on the cron session's model.
