# MARICOPA-REPORT.md — County Boards & Commissions Report Checklist

This file documents the specification for the **County Boards & Commissions Weekly Report**,
covering Maricopa County boards and commissions **except** the Board of Supervisors
(BOS is the last stop for policy — the point is monitoring what's happening at the other bodies).

---

## Process Order

When generating the report, follow this sequence:

1. **Query the database** for the date range
2. **Gather supporting documents** — download and extract PDFs for all BOA and P&Z items
3. **Classify each item's data source** — for each agenda item, determine whether the available evidence is a *staff recommendation* or a *commission vote* (see Data Source Classification below)
4. **Build the report** with complete information, clearly labeling staff recommendations vs. confirmed outcomes
5. **Audit factual claims with Berry** — before finalizing, run `berry__audit_claims` or `berry__detect_hallucination` on the report text against the evidence spans, with provenance-level checks
6. **Run unverified-action scan** — flag any items where a past meeting has no vote data but the report states an outcome

---

## Report Format

### Header

- Title: **Maricopa County Boards & Commissions — [Date Range]**
- Date generated

### Section Order

1. **Notable Stories & Trends** (top — the headlines)
2. **Past Month** (per body, most recent first)
3. **Coming Month** (per body, chronological)
4. **Pipeline Status** (cases moving between bodies)

---

---

## URL Rules

- [ ] All meeting links use the `meeting_id` VARCHAR column, **not** the internal `id` (INTEGER PK)
- [ ] URL format: `https://poliscopic.com/meetings/{body}/{meeting_id}`
- [ ] Local dev: `http://127.0.0.1:5001/meetings/{body}/{meeting_id}`
- [ ] Verify at least one URL is functional before publishing
- [ ] The routes/meetings.py handler queries `WHERE Meeting.meeting_id == meeting_id AND Meeting.body == body` — if a URL with the DB `id` (PK integer) is used instead, the page will show "Meeting not found"

---

## Supporting Document Inspection

- [ ] For **every BOA item**: download the staff report PDF from the `supporting_documents` table and extract the specific variance type/dimensions. The agenda item title alone is insufficient (e.g., "Coffey Property" doesn't tell you it's a retaining wall height variance).
- [ ] For **P&Z items where district is missing from the DB text**: download the staff report PDF. The header always contains `Supervisor District: N` as the first page.
- [ ] For **IDA items mentioning bond amounts**: download the staff report to get the exact project name, bond amount, and developer.
- [ ] Extraction tool: use `pdftotext -raw` (fastest) or PyMuPDF (`.venv/bin/python3 -c "import fitz; ..."`)
- [ ] Staff reports are on `maricopa.gov/AgendaCenter/ViewFile/Item/...` — may return large files. Use Range headers or short timeouts for large PDFs.
- [ ] If a PDF is too large or slow to download (< 500KB acquired in 10s), note the limitation rather than leaving blank.

---

## Factual Claim Validation (Berry)

- [ ] Before saving the final report, run factual audit on all claims derived from supporting documents
- [ ] Capture evidence spans for key facts:
    - District numbers
    - Variance types and dimensions
    - Bond amounts and project names
    - Recommendation/outcome (approve, deny, continue)
    - Dates (hearing, continuance, next hearing)
- [ ] Use `berry__add_span` to capture key evidence text from PDFs/database
- [ ] Use `berry__create_claim` for each factual assertion in the report
- [ ] Use `berry__link_claim_evidence` to connect claims to evidence spans
- [ ] Use `berry__audit_claims` to validate against the evidence (hallbayes scoring)
- [ ] Fix or flag any claims that score below target before publishing
- [ ] Reference: [ARTICLES.md](ARTICLES.md) for Berry workflow patterns
- [ ] At minimum, run `berry__detect_hallucination` on the final report text against the captured evidence spans

### Evidence Provenance (New Requirement)

Every Berry evidence span must include a `source_type` tag identifying what kind of document it came from:

| Tag | When to use |
|-----|-------------|
| `staff_report` | Evidence from a staff report PDF (staff recommendation) |
| `meeting_minutes` | Evidence from published meeting minutes (actual vote) |
| `handout_memo` | Pre- or post-meeting handout memos |
| `agenda_item_text` | From the database agenda item text field |
| `budget_resolution` | Approved budget or resolution |
| `enacted_ordinance` | Enacted ordinance (not draft) |
| `court_filing` | Court docket or official filing |
| `transcript` | Meeting transcript or video review |

- [ ] Tag every Berry evidence span with its `source_type` using the `meta` parameter
- [ ] Create **separate claims** for staff recommendations vs. commission actions — never use a staff report span to support a commission-action claim
- [ ] Run Berry audit with `context_mode: "cited"` to ensure each claim is only evaluated against its own evidence

### Claim Strength Check

- [ ] Verify the article doesn't express more certainty than the source allows
- [ ] "Staff expects" → not "will". "Proposed" → not "approved". "Draft" → not "the plan".
- [ ] If the gap between source and claim could mislead a reader, flag as a publication blocker.

### Negative Claims

- [ ] Any claim that something did NOT happen ("the board did not discuss", "no public comment") must cite meeting minutes, video, or transcript
- [ ] An agenda alone is insufficient — absence from an agenda ≠ absence from reality

### Source Precedence

When multiple sources exist, prefer:

1. **Adopted minutes / official vote record** (highest)
2. **Enacted ordinance / resolution / budget**
3. **Meeting transcript or video**
4. **Staff report** (staff recommendation only)
5. **Agenda** (what was proposed, not what happened)
6. **Handout memo** (late-breaking, may not reflect outcome)
7. **News coverage** (lowest — secondary source)

### Expanded Cross-Type Audit

Check the entire report, not just commission actions:

- [ ] Every **commission vote or outcome** claim uses `meeting_minutes` or `vote_table` evidence — not `staff_report`
- [ ] Every **ordinance adoption** claim uses enacted ordinance or minutes — not draft
- [ ] Every **budget allocation** claim uses approved budget or resolution — not proposal
- [ ] Every **staff recommendation** claim uses `staff_report` evidence — not minutes
- [ ] Every **negative claim** uses minutes, video, or transcript — not agenda
- [ ] Every **certainty/future** claim matches the source's level of certainty

If a mismatch is found, flag it: **"Claim: [text] is supported only by [wrong evidence type]. Revise to match the evidence or change the source."**

Correct the report text before publishing.

---

## Per-Body Requirements

### Planning & Zoning Commission

- [ ] Supervisorial district for each case (county-wide vs. district-specific)
- [ ] Case number (CPA, Z, SU, MCP)
- [ ] Location / area description
- [ ] What the applicant is requesting (rezoning, special use, etc.)
- [ ] Current zoning vs. proposed zoning
- [ ] **Staff Recommendation** — from the staff report PDF (always available pre-meeting)
- [ ] **Commission Action** — from minutes or vote data if available; otherwise "Minutes pending"
- [ ] Link to poliscopic.com meeting agenda
- [ ] Any supporting documents (staff reports) worth noting

> **⚠ CRITICAL RULE:** The staff report contains staff's *recommendation to the commission*, not the commission's decision. These are different things. If the meeting date is in the past and no vote data exists (`votes_extracted == False` and `vote_or_action` is empty), the report MUST NOT state the commission outcome as fact. Use "Staff recommends [x]" or "Commission action: pending" instead.

### Board of Adjustment

- [ ] Case number (BA)
- [ ] Property address / location
- [ ] Type of variance requested (area, setback, use, height, etc.)
- [ ] Current zoning of the property
- [ ] Staff report links (supporting documents) — use these to fill in variance details
- [ ] **Staff Recommendation** — from staff report
- [ ] **Commission Action** — from minutes or vote data if available; otherwise "Minutes pending"
- [ ] Link to poliscopic.com meeting agenda

> Same rule applies: past meetings without vote data must not present outcomes as confirmed.

### Industrial Development Authority

- [ ] Bond issuances (amount, project name, type: multifamily/healthcare/etc.)
- [ ] Legislative updates affecting bond authority
- [ ] Link to poliscopic.com meeting agenda

### Transportation Advisory Board

- [ ] Major agenda items (plans, studies, funding decisions)
- [ ] Link to poliscopic.com meeting agenda

### Board of Health

- [ ] Policy items, regulations, health advisories
- [ ] Link to poliscopic.com meeting agenda

### HOME Consortium / CDAC

- [ ] Funding allocations (HOME, CDBG, ESG)
- [ ] Annual Action Plan status
- [ ] Performance monitoring results
- [ ] Link to poliscopic.com meeting agenda

### Other Boards (Flood Control, Stadium District, etc.)

- [ ] Brief note if anything material happened
- [ ] Link if available

---

## Data Quality Rules

- [ ] If a supporting document URL exists for a BOA variance, **download and read the staff report** (use `pdftotext` or `web_fetch`) to extract the specific variance type and dimensions. The agenda item title alone doesn't tell you if it's an area variance, setback variance, use variance, etc.
- [ ] For P&Z rezoning cases where the full text is truncated, open the staff report to get complete location, district, and zoning info.
- [ ] District may be embedded in agenda_item_text as "Supervisorial District: X" — regex search the full text, not just the title.
- [ ] County-wide changes (code amendments, policy updates) should be flagged with **(Countywide)**.
- [ ] Cases continued from a prior hearing should note the original date.

## Hyperlinking Rules

Source references and meeting links in the report body must be clickable.

### Meeting Links

- [ ] Every meeting link uses **descriptive text** — not "View on poliscopic.com"
- [ ] Format: `[Planning & Zoning Commission Meeting, June 11, 2026](http://127.0.0.1:5001/meetings/pz/3770)`
- [ ] Descriptive text should be: **"[Body Name] Meeting, [Date]"**
- [ ] If the meeting was a special type (Executive, Informal), include it: "Board of Supervisors Executive Session, June 8, 2026"

### Source References

- [ ] Every staff report reference should be hyperlinked to the actual PDF on the source system
- [ ] Format: `[PZ Staff Report Z260015, 8 pages](https://www.maricopa.gov/AgendaCenter/ViewFile/Item/10337)`
- [ ] Handout memos should link to their specific PDFs, not just the main staff report
- [ ] Use the case number (Z260015, SU250040, BA260038) to look up the document URL from the `supporting_documents` table

### Automated Post-Processing

If building the report programmatically, run a post-processing step:

1. Scan the body for case number patterns (`[A-Z]{2,4}\d{5,8}`)
2. Look up each case number in the `supporting_documents` table by matching on `agenda_item_number` or `file_name`
3. Replace plain text staff report references with markdown links `[text](url)`
4. Replace bare meeting URLs with descriptive link text

### Base URL — No Localhost Links

- [ ] Email links must use the production base URL (`https://poliscopic.com`), not `127.0.0.1`
- [ ] `scripts/email_article.py` defaults to `https://poliscopic.com` via `BASE_URL` config
- [ ] Set `POLISCOPIC_BASE_URL=http://127.0.0.1:5001` in `.env` for local testing only
- [ ] The URL substitution happens at send time — the database record keeps local URLs for admin previews

See `scripts/email_article.py` for the automated hyperlinking implementation.

## Data Source Classification (Do Not Confuse)

Before writing any outcome in the report, classify the evidence:

| Source | What it tells you | Can it prove a commission vote? |
|--------|-------------------|--------------------------------|
| Staff report | Staff's recommendation to the commission | **No** |
| Handout memo (pre-meeting) | Late-breaking info before the hearing | **No** |
| Handout memo (post-meeting) | May contain the outcome if it's a results memo | **Check carefully** |
| Meeting minutes | The actual commission vote/action | **Yes** |
| Vote table (agenda_item_votes) | Structured vote data per item | **Yes** |
| Agenda item text | May say "Staff recommends..." — same constraint as staff report | **No** |

**Rule of thumb:** If the meeting date is in the past, check `meeting.votes_extracted` and `agenda_item.vote_or_action` in the database. If `votes_extracted` is `False` and `vote_or_action` is empty, the commission outcome is unknown and must not be asserted as fact.

- [ ] For each past meeting, check `votes_extracted` and `vote_or_action` before writing outcomes
- [ ] Where outcome is unconfirmed, use "Staff recommends [x]" with the staff report as source
- [ ] Never write a bare "Approve" / "Deny" in a cell if it could be mistaken for a commission action

## Unverified Action Scan (Pre-Publication Check)

Before saving the report, run a scan over every row:

- [ ] For each item where `meeting_date < today` and `vote_or_action` is empty and `votes_extracted` is False:
  - Flag the row
  - Label the outcome column as **"Minutes pending"**
  - In the staff recommendation column, note what staff recommended
  - Add a note to the Notable Stories section: "[X] cases from [body/date] have no published minutes yet — outcomes for [case names] are pending."

- [ ] For each item where `meeting_date < today` and `vote_or_action` IS populated:
  - Cross-reference against the staff report to see if the commission agreed or overruled staff
  - Note discrepancies in the Notable Stories section

---

## Upcoming Meetings

- [ ] Check all county bodies for upcoming meetings with `sync_status != 'pending'`
- [ ] Some meetings won't be published yet — note expected schedule pattern
- [ ] Cases continued to a future date should appear under the upcoming section

---

## Pipeline Tracking

- [ ] Cases that move from P&Z to BOS (recommendation → formal adoption)
- [ ] Cases continued within P&Z (note next hearing date)
- [ ] BOA variance denials that might go to BOS on appeal
- [ ] IDA bond issuances that need BOS concurrence

---

## Exclusion

- **Board of Supervisors** is explicitly excluded unless a case that originated at a lower board is being tracked through to final adoption. BOS is covered separately in the housing pipeline review.

---

## Output

- Save as `drafts/YYYY-MM-DD-county-boards-review.md`
- Follow the report format above
- If supporting documents require downloading PDFs for variance details, download to `/tmp/` and extract with `pdftotext` or PyMuPDF.
