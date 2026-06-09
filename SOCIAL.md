# SOCIAL.md — Bluesky Posting Workflow

> **This is a workflow file.** It describes how to publish article announcements
> on Bluesky. For the social posting implementation details, see
> [scripts/social.py](../scripts/social.py) and
> [bsky.py](../bsky.py).

---

## Overview

Every published article gets a Bluesky post (a "skeet"). The workflow is:

1. **Draft** — feed the article + STYLE.md §7 to an LLM to generate skeet pitches
2. **Select** — pick the best pitch (300 chars max, preferably 80–120)
3. **Post** — pass the article ID and text to `bsky.py`

That's it. No queue, no cron, no skeet drafts table. Direct posting from the CLI.

---

## Prerequisites

- Article is published in the database (status = published)
- Article has been deployed to production (see [SYNC.md](SYNC.md))
- Environment variables set:
  - `BLUESKY_HANDLE` — e.g., `poliscopic.bsky.social`
  - `BLUESKY_APP_PASSWORD` — generated from Bluesky Settings > App Passwords
    (never use your account password)

---

## Workflow

### 1. Generate skeet pitches

Feed the published article and STYLE.md §7 (Social Media Hooks) to an LLM.
The prompt should include:

- The article title, summary, and body
- STYLE.md §7 rules (lead with specifics, create a knowledge gap, max 300 chars,
  prefer 80–120, no editorializing)
- A request for 3–5 options covering different angles

The LLM should return options like:

```
1. "$48 million in bonds for a 144-unit apartment complex. Whether rents
    will remain affordable is the key question." (118 chars)

2. "A Hilton hotel could be coming to Mesa's Cannon Beach surf park." (62 chars)

3. "Chandler is tightening bird-feeding rules. What counts as a violation?"
    (72 chars)
```

### 2. Select the best option

Pick the option that:

- Leads with the most specific, concrete fact (dollar amounts > descriptions)
- Creates a knowledge gap the article answers
- Fits in 300 characters (80–120 is ideal)
- Does not editorialize (no "significant" or "notable")

### 3. Post to Bluesky

```bash
cd /Users/pmains/Code/openclaw/maricopa-agendas
python bsky.py --article <id> --text "Your skeet text here"
```

The tool:
- Reads the article (title, summary, featured image) from the database
- Constructs the article URL from the slug
- Builds a rich link card with the featured image, title, and summary
- Posts directly to Bluesky
- Records the post URI in the tracking database (prevents accidental duplicates)

**Example:**

```bash
python bsky.py --article 60 --text "Glendale's 10-year transportation plan is out. Here's what's in it."
```

Output:
```
Posting article 60: Glendale renews regional bus agreement, keeping West Valley connected
  URL:    https://poliscopic.com/articles/2026-06-09-glendale-valley-metro-agreement
  Text:   Glendale's 10-year transportation plan is out. Here's what's in it.
  ✓ Posted: https://bsky.app/profile/poliscopic.bsky.social/post/3abc123
```

---

## Dedup and reposting

The tracking database (`data/bluesky_tracking.sqlite`) records every post's
article ID and AT URI. If you try to post an article that's already been
posted, `bsky.py` prints a warning but still posts — the dedup is advisory,
not enforced.

To repost an article (e.g., after correcting the skeet text), run the command
again. The old tracking record remains but a new post is created.

---

## Command reference

```bash
python bsky.py --article <id> --text "<skeet>"
python bsky.py --article 60 --text "Glendale renewed its bus agreement with Valley Metro."
python bsky.py --article 60 --text "$2.1M for street repairs in Glendale's latest CIP." --url https://staging.poliscopic.com
```

| Flag | Required | Default | Description |
|---|---|---|---|
| `--article`, `-a` | Yes | — | Article ID in the database |
| `--text`, `-t` | Yes | — | Skeet body text (max 300 chars) |
| `--url` | No | `https://poliscopic.com` | Base URL for article links (used for staging) |

---

## What happens when you post

1. `bsky.py` loads the article from `data/maricopa.sqlite`
2. Fetches the featured image (from disk or URL)
3. Resizes and compresses it for Bluesky's 1MB blob limit (1200×630, JPEG)
4. Authenticates with Bluesky via `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD`
5. Sends the post with an embedded link card (image + title + summary)
6. Records the post in `data/bluesky_tracking.sqlite`
7. Prints a Bluesky URL you can open to verify

---

## Posting workflow within the article lifecycle

```
Article drafted → Article deployed → Skeet drafted (LLM) → bsky.py → Done
                                          │
                                   STYLE.md §7 rules
```

The full lifecycle (draft → deploy → social) is three sequential steps across
three workflow files. Each hands off to the next:

1. [ARTICLES.md](ARTICLES.md) — draft and verify the article
2. [SYNC.md](SYNC.md) — deploy the article to production
3. This file — post to Bluesky

---

## Revision History

| Date | Change |
|---|---|
| 2026-06-09 | Initial workflow — direct posting via bsky.py (replaces old skeet-draft + cron system) |
