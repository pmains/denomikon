# Poliscopic Style Guide

This document governs the tone, framing, editorial voice, and editorial process
for all articles published on Poliscopic. Refer to it before every draft.

> For scraper building and data quality rules, see [SCRAPERS.md](SCRAPERS.md).
> For operations, architecture, and deploy instructions, see [OPS.md](OPS.md).

---

## Editorial Process

Every article follows this workflow. No step is optional. Before drafting, ask: does this meeting item have a genuine news angle — a public hearing, a decision with winners and losers, a dollar figure that raises questions, a policy change that affects people? If the answer is no, skip it. Not every agenda item earns an article, and forcing one leads to fabrication.

1. **Draft from the agenda.** Start with what the meeting item says — the
date, the body, the action, the dollar amount.

2. **Find additional sources.** Search for news coverage, prior related meetings,
statutes, studies, or government data that add context. Three sources minimum.

3. **Augment.** Add context from the additional sources. A reader should
understand not just what happened, but why it matters.

4. **Explain jargon.** Scan the draft for any term a neighbor wouldn't know.
Every technical concept gets a plain-language sentence or paragraph before the
article moves on. Do this before finalizing, not after a reader flags it.

5. **Verify every factual claim.** Dollar amounts, dates, vote counts, quotes,
statutory requirements — check each one against a source. If you cannot verify
it, remove it. Never connect two facts to imply a relationship unless a source
explicitly makes that connection. A spring training facility upgrade and a
team's regular-season relocation are about the same team but are unrelated.
Do not fabricate causal links.

6. **Link every claim inline.** Every factual claim gets a link to its source
in the article body. Internal links use relative paths (`/meetings/...`).
The meeting tracker link goes in the first paragraph where the meeting is
introduced.

7. **Add sources once.** Every source cited inline goes in the sources box.
No source appears more than once. Remove stale sources.

8. **Publish.** Mark as published, generate a skeet draft, review and post.

---

## 1. No "It's Not X, It's Y" Framing

**Prohibited:** Any sentence structure that defines something by what it *isn't* before stating what it *is*.

| ❌ Banned | ✅ Preferred |
|---|---|
| "Framework 2040 is not a zoning map — it is a policy document." | "Framework 2040 functions as a policy document, establishing criteria against which every rezoning will be evaluated." |
| "The plan doesn't stop growth — it directs it." | "The plan directs growth rather than stopping it." |
| "This isn't just a routine approval — it's a signal of changing priorities." | "The approval signals changing priorities." |
| "The commission isn't blocking development — it's demanding better infrastructure." | "The commission is demanding better infrastructure before approving further development." |
| "The story isn't about the building — it's about who benefits." | "The central question is who benefits." |
| "This isn't just about parking — it's about the future of the neighborhood." | "The parking dispute reflects deeper questions about the neighborhood's future." |
| "The rezoning isn't about density — it's about infrastructure capacity." | "The rezoning turns on whether infrastructure can support the proposed density." |

**The pattern to avoid:** `[subject] isn't [X], it's [Y]` or `[subject] doesn't [X], it [Y]` or `not [X], [Y]` or any structural variation where you negate one thing to assert another.

**Why:** It's a cliché of AI-generated and editorial writing. Readers don't need the negation — just state what it *is*.

---

## 2. No Opaque Body Codes on the Front Page

**Prohibited:** Displaying internal body codes (`chandler-cc`, `tempe-drc`, `bos`) anywhere on the public-facing site.

- Always resolve to human-readable names: "Chandler City Council," "Tempe Development Review Commission," "Maricopa County Board of Supervisors."
- The body-name mapping covers all Chandler boards/commissions, Tempe, Mesa, Maricopa, Scottsdale, and Gilbert bodies. If a new body appears without a mapping, flag it for addition.

---

## 3. Minimum Sources

Every article should have at least three sources when possible. A single agenda item may not provide enough context for readers to evaluate the reporting. Additional sources — related meetings, staff reports, news coverage, or statutes — give readers a fuller picture and make the article more useful.

Exceptions: breaking news where no additional sources are available yet, or very short items (under 200 words) that summarize a single action.

---

## 4. Source Links Must Have Descriptive Text

- Never display raw body codes or meeting IDs as link text.
- External sources (news articles, government pages): use the publication name and article title. E.g., "Grand Canyon Times: County Board adopts Framework 2040"
- Agenda/official sources: use a descriptive label. E.g., "Maricopa County BOS Agenda — May 20, 2026 (Item 8: Framework 2040)"
- All link text is stored in the `item_title` field of `ArticleSource`.

---

## 5. Attribution and Quotes

- Always attribute quotes to the speaker by full name and title/body where relevant.
- When paraphrasing a board member or official, indicate which body they represent: "Chair Kate Brophy McGee (District 3) said..."
- Do not fabricate or paraphrase quotes from sources you haven't read. If you're summarizing a news article's coverage of what someone said, cite the article.

---

## 6. Claims Need Sources

- Every factual claim about a meeting, vote, project, or policy must be traceable to a source (agenda item, official document, news article, staff report).
- Every claim must have an inline link in the article body. No exceptions for dollar figures, dates, votes, or government actions.
- The source should also appear in the sources box at the bottom of the article.
- If a claim comes from a news article about a meeting, cite the article, not the meeting directly — unless you verified it from the meeting record yourself.
- Sources must be listed once and only once in the sources box. No duplicate entries.

---

## 7. Location Verification

- Addresses and locations are the most commonly invented detail in draft articles.
  An address that is not verified **will** be wrong. Verify every location against
  the supporting documents — not just the agenda item title.
- If the agenda item title says "123 Main Street" and that is the only source you
  have, state it. If you are extrapolating a location from context ("the project is
  near the light rail station"), say so.
- When a development project's address is available in the staff report, site plan,
  or legal description, use that. The agenda item title may omit the cross street
  or abbreviate it in a way that misleads.
- If you cannot find a specific address in the supporting documents, flag the
  omission rather than filling it in from memory or assumption.

---

## 8. Article Voice

- Write in plain English. Avoid editorializing or signaling importance with phrases like "critical," "significant," "notable," "worth watching." Let the facts speak.
- No rhetorical questions. ("What does this mean for residents?")
- No closing moral. Don't summarize what the reader should "take away." The article should be structured so the conclusion is clear from the reporting.
- Keep sentences short and paragraphs concise. One idea per paragraph.
- **No redundancy.** Do not restate the same claim in different words within the same paragraph. If you've already made a point, move on. Re-read your draft before saving.
- Use active voice. "The board approved the plan" not "The plan was approved by the board."

---

## 9. Titles and Summaries

- Titles should be specific and informational, not clever or clickbait. They should tell the reader what the article is about.
- Summaries (displayed on the front page) should be one or two sentences that convey the core news — who, what, where, and the key tension or outcome.
- Include dates in summaries where relevant. "The Board of Supervisors approved Framework 2040 on May 20, defining where urban development belongs..."
- Slugs include the meeting date prefix (YYYY-MM-DD) for uniqueness and SEO.
- **Strip stop words from slugs.** Remove common filler words (a, an, the,
  and, or, of, for, in, to, with, on, at, by) to keep slugs short and readable.
  "Phoenix approves $75,000 contract for Central Station improvements" becomes
  ``2026-05-29-phoenix-75000-contract-central-station-improvements``, not
  ``2026-05-29-phoenix-approves-75000-contract-for-central-station-improvements``.

---

### 8b. Title, Summary, and Lede Alignment

The title, summary, and lede serve different audiences but must tell the same
story. They should not repeat each other. Each layer should add something the
previous one didn't, creating progressive disclosure that pulls the reader in.

| Element | Where it appears | What it does |
|---|---|---|
| **Title** | Front page, search results, link previews | States the core action in plain language. No acronyms without explanation. Specific and informational, not clever. |
| **Summary** | Front page, article cards | Adds the tension or stakes. Gives the reader a reason to click that the title alone didn't provide. One or two sentences. |
| **Lede** (first paragraph) | Article body | Delivers on the promise the summary made. Uses a specific fact, tension, or question-implication to create a reason to keep reading. |

**What progressive disclosure looks like:**

| Layer | Example |
|---|---|
| Title | "Glendale renews regional bus agreement, keeping West Valley connected to Valley Metro transit network" |
| Summary | "Glendale renewed its agreement with Valley Metro for the 14th time, funding regional bus and paratransit service for a city that has no light rail and relies on buses to connect residents to jobs and medical care across Maricopa County." |
| Lede | "Glendale residents who rely on regional buses to get to work, medical appointments, or across the Valley will continue to have transit service under an agreement the City Council renewed on May 26." |

The title states the action. The summary adds the tension (no light rail, bus-dependent). The lede delivers by putting real people and real destinations into the story.

**Common mistakes:**

- **The echo chamber:** Title says "Council approves contract," summary says "Council approved a contract on May 26," lede says "The Council approved a contract on May 26..." The reader learns nothing new at each layer. Fix: each layer should add one new piece of information — the what, then the why, then the who-it-affects.
- **The summary-spoiler:** The summary front-loads the entire outcome, leaving the lede as a rephrasing. Fix: the summary should create a question the lede answers, not answer it before the reader clicks.
- **The acronym trap:** Title uses an acronym without explanation ("RPTA," "TNT," "SS4A"). Readers who don't know the acronym have no reason to click. Fix: use the full name in the title or summary on first reference.


### 8a. Social Media Hooks (Skeets)

Every article gets a Bluesky skeet. The skeet text is a hook — its job is to
stop a scroll and make someone click. It is not a summary. It is not a full
sentence about what happened. It is the thing that makes a curious reader
want to know more.

### Principles

**Lead with the most specific fact.** If the article involves a dollar figure,
that figure should be the first thing in the skeet. "$153,400" stops the
scroll. "The council approved a contract" does not.

**Create a knowledge gap.** The best hooks make the reader wonder what comes
next. A question or an incomplete claim (". . . and here's why that matters")
works better than a complete statement. The gap should be honest — the full
answer is in the article.

**Be under 300 characters.** Bluesky's limit is 300. Skeets that use the full
budget will be truncated in feeds. The hook itself should fit in 80–120
characters. The link card (image, headline, description) carries the rest.

**Avoid editorializing.** No "critical," "significant," "notable" — the same
rule as articles. The facts should create their own urgency. "$153,400 for
license plate readers and no state law on data retention" is more effective
than "An important privacy decision in Chandler."

**Don't bait-and-switch.** The hook should promise something the article
delivers. If the skeet raises a question, the article should answer it. If
the skeet leads with a surprising figure, the article should explain it.
Misleading hooks train readers to ignore you.

### By article type

**Spending / contracts / budgets:** Lead with the dollar figure, then state
what it's for, then add the tension or open question.

> $153,400 for automated license plate readers in Chandler. Arizona has no
> state law on how long your plate data is kept.

> $62,849 for another year of gunshot detection in Glendale. The 13th
> amendment to the contract is a routine renewal — but the total spend keeps
> climbing.

> $48M in bonds for a 144-unit Glendale apartment complex. Whether the rents
> will be affordable is the open question.

**Development / rezoning:** Name the project, the location, and the tension.

> A Hilton Tapestry hotel could be coming to Mesa's Cannon Beach surf park.
> The Planning Board is reviewing the proposal now.

> Chandler is weighing a rezoning at Arizona Avenue and Guadalupe Road.
> The intersection is one of the busiest in the city.

**Policy / ordinances:** State the action, then imply a question.

> Chandler is cracking down on problem bird feeding. What counts as a
> violation under the new rules?

> Tempe restructured its council subcommittees. Animal welfare and drink
> spiking prevention are now explicitly on the agenda.

**Water / infrastructure:** Lead with the resource question — it's the
underlying story in the desert.

> Scottsdale is exploring a water purchase in the Harquahala Valley. The
> council discussed it behind closed doors.

### Hook pattern reference

These patterns work for local government news. Pick the one that fits the
story type, not the one that sounds clever. If none fit, use the specific
fact lede (the article title) -- it is often the strongest hook.

| Pattern | What it does | When to use |
|---|---|---|
| **Surprising Statistic** | Lead with a dollar figure, lot count, or other specific number. The number itself creates curiosity. | Budget, contract, bond, and development stories that involve money or units. |
| **Curiosity Gap** | State a fact that implies an unresolved question. The article answers it. | Policy stories with an interesting angle -- privacy implications, loopholes, exceptions. |
| **Question Hook** | State the action, then state what remains unresolved. | Ordinances, rule changes, restructuring -- any story about a decision with open implementation questions. |
| **Problem-Solution** | Name the problem the city is trying to solve, then note what they're doing about it. | Water supply, infrastructure, public safety -- stories where the motive is as important as the action. |
| **Pattern Interrupt** | Start with the perspective of the affected person, not the government. | Stories about how a decision affects residents -- parking, privacy, new fees. |
| **Data/Statistic** | Numbers that show scale or trend. | Bond measures, population growth, budget comparisons. |

### Tone

The same voice rules apply: plain English, active voice, no rhetorical
questions, no editorializing. A skeet should sound like a person telling
another person something interesting — not a press release or a headline.

### Relationship to article summaries

The article summary (displayed on the front page) and the skeet text may
overlap but serve different purposes. The summary tells someone who is
already reading what the article is about. The skeet has to earn a click
from a feed. It is acceptable — often preferable — for the skeet to lead
with a different angle than the summary.

### Auto-generated hooks

`bluesky_sync.py --create-drafts` generates a starting hook for every
published article. It follows the principles above: dollar-first for spending
stories, clean headline for everything else. Treat the auto-generated text
as a draft, not a final. Revise it in the Admin UI panel at
``/admin/skeet-drafts`` before approving.


### 8c. Tags

Every article must have at least one tag, and most should have two or three.
Tags serve two purposes: they help readers find related articles, and they
feed the AI suggestion system that surfaces agenda items worth covering.

**Required:** A topic tag that describes the primary subject. At minimum,
every article needs one of: Budget, Data Centers, Development, Economy,
Education, Enforcement, Environment, Health, Housing, Parks, Public Safety,
Transportation, Water, Zoning.

**Recommended:** A jurisdiction tag (Government) and a second substantive
tag when the article crosses topics. A zoning case that affects housing
supply should have both Zoning and Housing. A budget item that funds parks
should have Budget and Parks.

**How to assign:** Use the tag dropdown in the article edit form in the
Admin UI. Tags are managed at ``/admin/tags``. If a relevant tag doesn't
exist, add it there first.


## 10. Scope

- We cover public meetings of governing bodies in Maricopa County: councils, boards, commissions, authorities, and committees.
- We do not cover private sector decisions unless they intersect with a public meeting agenda.
- We do not cover state or federal government except where Maricopa County bodies interact with them.

---

## 11. Narrative Angle

Every article needs a reason for a casual reader to care. It is not enough to report that a board voted on something. The article must answer: "So what?"

The narrative angle is the through-line that connects the specific action to a broader question the reader might have. It turns a meeting item into a news story.

**Examples:**
- Not: "The council approved a use permit for a parking lot." But: "A strip of church parking near Mill Avenue is about to become paid parking — and that says something about how downtown Tempe is changing."
- Not: "The board approved bonds for an apartment complex." But: "Glendale is getting 144 new apartments financed through a bond program designed for affordable housing. Whether the rents will actually be affordable is the open question."

The narrative angle must be honest — it cannot exaggerate or fabricate significance. If a routine consent item has no real story behind it, that is worth knowing too. Not every meeting item needs an article.

This is consistent with standard journalistic ethics: the reporter's job is to select and frame what matters, not to transcribe everything that happened.

### The lede as a hook

The first paragraph should create a reason to keep reading, not summarize the entire article. A lede that states a specific fact, reveals a tension, or implies a question the article will answer is more effective than one that front-loads the conclusion.

**Specific fact lede:** "The Mesa City Council on May 18 authorized the purchase of 12 new fire apparatuses — nine pumpers, two heavy rescue vehicles, and one aerial platform — funded by voter-approved public safety bonds." The specificity does the work: twelve vehicles, three types, bond-funded. The reader can see the scale of the decision immediately.

**Tension lede:** "A strip of church parking near Mill Avenue is about to become paid parking — and that says something about how downtown Tempe is changing." The tension isn't synthetic — it flows from an observable fact (church lot → paid parking) that implies a larger trend.

**Question-implication lede:** "Glendale is getting 144 new apartments financed through a bond program designed for affordable housing. Whether the rents will actually be affordable is the open question." The paragraph states a fact and then identifies the unresolved question that the article examines.

Avoid ledes that summarize the whole article in the first two sentences. If the reader already knows the outcome and the tension before the second paragraph, there is no reason to keep reading. Let the article unfold.

---

### 10a. Link to Our Meeting Tracker

Every article that references a specific meeting must include a link to that
meeting's detail page, placed in the first paragraph where the meeting is
introduced. Never bury it at the end of the article or in a standalone sentence.

The meeting tracker URL is a relative path:

    /meetings/{body}/{meeting_id}

**The link must appear in the first paragraph.** When the article introduces
the meeting ("The City Council on May 20 approved…"), the date or the action
should link to the meeting tracker. Examples:

> "The Phoenix City Council on May 20 [approved a $75,000 contract](/meetings/phoenix-cc/phoenix-formal-2026-05-20) with…"

> "The [Buckeye City Council holds a Truth-in-Taxation hearing](/meetings/buckeye-cc/1072) June 2 at 6 p.m…"

Do not add a separate "View the full agenda" sentence at the end. The tracker
link is part of the narrative, not an appendix.

Once per article, at the point where the meeting is first named. If the article
covers multiple meetings, each gets one link on first mention.


## 12. Em-Dashes

Use em-dashes sparingly. They come across as artificial. A comma, semicolon, or a new sentence is almost always more natural. If you find yourself using more than one em-dash per article, reconsider each one.

**Preferred:** "The council voted 7-0, with three members abstaining after a long debate."
**Avoid:** "The council voted 7-0 — three members abstained — after a long debate."

---

## 13. Inline Linking

Link to source material directly within the article body, not just in a sources box at the bottom. If an article says "Scottsdale passed an ordinance in July 2025," that phrase should link to the Scottsdale meeting or ordinance. Inline links are how readers verify claims without scrolling and searching.

**Every factual claim must have an inline link.** If you write "the council approved a $75,000 contract" — the phrase "$75,000 contract" links to the meeting agenda. If you write "the station was named for former mayor Greg Stanton" — the naming ordinance links from that claim. No claim about a meeting, vote, dollar amount, date, or ordinance should appear in the article body without a link to its source.

When to inline link:
- Every mention of a specific meeting, vote, or ordinance
- Every dollar figure, date, or factual assertion about a government action
- Every reference to another city's action that has a source in our database
- Every claim that a reader might want to verify

Inline links do not replace the sources box. The sources box at the bottom provides a clean list of every source used. Inline links provide immediate access for readers who want context as they read.

**Use relative links for internal content.** Never use absolute URLs like `https://poliscopic.com/meetings/...` in article bodies or sources. Use relative paths: `/meetings/phoenix-cc/phoenix-formal-2026-05-20`. This ensures links work on both the development server (localhost) and the production site (poliscopic.com). External sources (news articles, government pages outside our domain) use full URLs.

**No redundant inline links.** Each source may be linked only once in the article body. If two different claims come from the same source, link the first claim and do not link the second — the reader can follow the single link or check the sources box. The only exception is the meeting tracker link, which appears once in the first paragraph per `§9b`. Duplicate inline links waste linking credibility and clutter the article. Every link should point to a distinct source.

**Article organization.** An article should follow a logical arc that carries the reader from the specific action to its broader implications. A reliable pattern:

1. **Lead paragraph** — the specific action, the vote, the deadline. (What is happening?)
2. **Details** — dollar amounts, locations, the mechanics of the decision. (What does it involve?)
3. **Context** — why this matters, the background, the trend. (Why should a reader care?)
4. **Connections** — related items on the same agenda, parallel decisions in other cities. (What else is relevant?)

Not every article needs all four layers, but the movement should be from the concrete to the contextual. Do not front-load connections to other meetings or background before establishing what the current meeting actually does. A paragraph listing other agenda items on the same meeting belongs at the end of the article — the reader cannot evaluate what else is happening until they understand the primary story.

**When to inline link vs. when to hold back:** Claims that are observations about the meeting itself — who spoke, what was approved, which resolutions passed — are sourced to the meeting agenda linked in the sources box. Claims that are statistics or facts about another entity — how many other cities do something, how much funding a program has, what a study found — need their own inline link to a specific supporting document or article. If you cannot find a source for an external fact, remove the claim. Do not rely on a general homepage URL (e.g. a city's planning department homepage) as evidentiary support for a specific factual claim.

---

## 14. Write to a 5th-Grade Level

Write for a curious adult who does not follow local government closely. Assume no prior knowledge of:
- Zoning categories, land-use terminology, or planning acronyms
- E-bike classes, vehicle codes, or regulatory frameworks
- Bond mechanics, tax structures, or pension funding
- Board/commission hierarchies or parliamentary procedure
- Any other advanced concept that a normal person who is not heavily engaged with politics might not know

**Rules of thumb:**
- Define every acronym on first use.
- Replace jargon with plain language. Not "tentatively adopted" but "gave initial approval."
- If a concept needs more than one sentence to explain, it needs its own paragraph.
- If a neighbor wouldn't understand it, explain it. Every technical concept — bonds, levies, zoning overlays, pension ratios — gets a plain-language sentence or paragraph before the article moves on.
- Do this before the draft is finalized, not after a reader flags it.

This is not about dumbing down. It is about meeting readers where they are. The most valuable reporting is the kind that makes an opaque process transparent.

---

## 15. Editorial Positions

Poliscopic's coverage is driven by the following editorial values. They inform which stories we pursue, how we frame them, and what questions we ask.

- **Pro-housing:** We treat housing supply as a public good. We favor zoning changes and policies that add housing units at all price points, especially near transit and employment centers. We are skeptical of regulatory barriers that restrict supply.
- **Pro-growth:** Responsible growth accommodates new residents, businesses, and economic activity. We support development that uses land efficiently, pays for its infrastructure, and expands opportunity. We question growth that externalizes costs onto existing residents.
- **Pro-bicycle:** We support bike infrastructure, safe routes, and policies that reduce car dependency. We treat bicycle and pedestrian access as essential components of transportation, not recreation.
- **New urbanist:** We favor walkable, mixed-use, transit-oriented development. We support compact urban form over sprawl. We believe streets should serve people, not just cars.
- **Water & natural resources:** In the arid Southwest, water is the binding constraint on growth. We treat water conservation, groundwater management, and long-term supply planning as critical coverage areas. We favor policies that align development with available water resources.
- **Clean energy:** We support solar, renewable energy, and policies that reduce carbon emissions. We are interested in how local governments facilitate or obstruct clean energy adoption.
- **Public safety:** We treat public safety as a core government function. We cover police, fire, emergency services, and the policy decisions that affect them — pensions, equipment, staffing, and accountability.
- **Pro-transit:** We support light rail, bus rapid transit, and regional transit as essential mobility infrastructure. We cover transit boards, route planning, fare policy, and the land-use decisions that determine whether transit works.
- **Climate resilience:** In addition to water and clean energy, we cover heat mitigation, urban shade planning, extreme weather preparedness, and the built environment's role in making the Valley livable as temperatures rise.
- **Fiscal hawk:** We scrutinize how public money is spent. We favor competitive contracting, transparent procurement, and rigorous cost-benefit analysis for incentives and subsidies. We question sweetheart deals, no-bid contracts, and pension structures that crowd out core services. Supporting growth does not mean supporting wasteful spending.
- **Open government:** We believe public meetings should be accessible, records should be easy to obtain, and decisions should be traceable. We call out when boards obscure their reasoning, bury decisions in consent agendas, or make it hard for the public to participate.

These positions do not dictate outcomes in every story. They guide what we investigate and how we frame the questions. We aim to report accurately even when the facts challenge our priors.

---

---

## 16. Featured Images

Every article should include a featured image when a suitable freely-licensed or public domain photo is available.

**Sources, in order of preference:**

1. **Gage Skidmore (preferred local source)** — Surprise-based photographer ([Flickr](https://www.flickr.com/photos/gageskidmore/)) who licenses his work **CC BY-SA 4.0**. His 140,000+ photos cover Arizona politics, events, development, and city council meetings — directly relevant to Maricopa County coverage. Preferred over generic sources because the photos are local, current, and specific to the subjects we cover. Attribute: "Photo: Gage Skidmore / CC BY-SA 4.0"
2. **Wikimedia Commons** — Search Commons directly for specific subjects. Photos are typically CC BY, CC BY-SA, or public domain. Attribute the photographer and license in the article metadata.
3. **Flickr with a CC license** — Use Flickr's advanced search to filter by Creative Commons license. Acceptable: **CC BY** and **CC BY-SA**. Avoid **CC BY-NC** (non-commercial — incompatible with news publication) and **CC BY-ND** (no derivatives — can't crop or resize).
4. **U.S. government sources** — Photos by federal agencies (NASA, NOAA, NPS) are generally public domain. State and local government photos may or may not be — check before using.
5. **Subject's own press kit or website** — Some organizations provide media kits with permissive licenses. Always check the terms.

**What to avoid:**
- AI-generated images of any kind.
- Photos from news articles — those are owned by the photographer or publication and are not freely licensed.
- Screenshots of agenda documents, PDFs, or meeting videos — these are not useful as article illustrations.
- Generic stock photos ("person shaking hands", "city skyline") — they add nothing.

If a search of these sources does not turn up a suitable image, publish without one. Do not settle for an unrelated or poorly licensed image.

### Metaphorical Imagery

When a literal photo of the subject is unavailable, a metaphorical image from a
freely-licensed source is acceptable. The image should represent the story's
theme, not its exact subject.

| Story theme | Acceptable metaphor | Example |
|---|---|---|
| Water supply, drought, infrastructure | Water — a lake, canal, reservoir, or river. Not a specific water treatment plant unless the story is about that plant. | A CC-licensed photo of the CAP canal or Tempe Town Lake for a water spending story. |
| Transportation, road projects | Traffic — a highway, intersection, or bus. Not a specific intersection unless the story is about that intersection. | A CC-licensed photo of a Phoenix freeway for a transportation bond story. |
| Fire, public safety | Fire truck, firefighter, police vehicle — in a setting that could plausibly be Arizona. | A red fire engine with no visible non-US markings. |
| Housing, development | Construction, a residential street, apartments — not a specific building unless the story is about that building. | A CC-licensed photo of new home construction in a desert setting. |
| Parks, recreation | A park, trail, playground — not a specific park unless named. | A CC-licensed photo of a desert park or playground. |
| Budget, finance | A city hall building, a council chamber, or a municipal building — since the budget is about government spending. | A CC-licensed photo of a municipal building or city hall exterior. |
| General government | A city hall, council chamber, or county building. Can be from any jurisdiction. | A CC-licensed photo of any city hall or county administration building. |

**Rules for metaphorical images:**
1. **Geographic plausibility.** The image must look like it could be in Arizona or the American Southwest. No French fire trucks, European street signs, or tropical vegetation.
2. **No deception.** The caption must not imply the photo is of the specific location or event in the story. Use generic attribution: "Photo: [Photographer] / CC BY 2.0" without a location claim.
3. **Licensing still applies.** The same CC license rules apply — CC BY or CC BY-SA only, no NC or ND.
4. **Prefer literal when available.** A metaphorical image is better than no image, but a literal image from Gage Skidmore or Wikimedia Commons specific to the story is always better.

---

*Last updated: May 28, 2026*
