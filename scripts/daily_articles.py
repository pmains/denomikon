#!/usr/bin/env python3
"""Daily Article Pipeline — runs locally, drafts articles via OpenAI API.

Replaces the old agent-session pipeline that hit DeepSeek's 6-minute timeout.

This script runs operations locally and only connects to OpenAI when drafting
the actual articles. No long-running agent session needed.

Flow:
  1. Query DB for recent (last 7d) and upcoming (next 3d) meetings
  2. Score agenda items by newsworthiness
  3. Deduplicate by case number
  4. Select top 4 (geographic diversity enforced)
  5. Gather context/sources for each candidate
  6. Call OpenAI API to draft article (title, summary, body)
  7. Hyperlink source references in body text
  8. Save to DB as draft articles with tags and sources
  9. Print Slack summary to stdout

Usage:
  POLISCOPIC_DB_TIER=development PYTHONPATH=scripts .venv/bin/python scripts/daily_articles.py
"""

import os
import re
import sys
import json
import textwrap
import datetime as dt
from typing import Optional
from dataclasses import dataclass, field

from sqlalchemy import func, text as sql_text
from dotenv import load_dotenv
from openai import OpenAI

# ── Load env (includes OPENAI_API_KEY) ──
load_dotenv()

os.environ.setdefault("POLISCOPIC_DB_TIER", "development")

from db.core import get_session
from db.newsroom import Article, ArticleSource, Tag
from db.models import Meeting, PublicBody, AgendaItem, SupportingDocument, Jurisdiction


# ── Configuration ──

OPENAI_MODEL = os.environ.get("DAILY_ARTICLE_MODEL", "gpt-4o-mini")
NEWS_DAYS_BACK = 7
NEWS_DAYS_FWD = 3
MAX_ARTICLES = 4
MAX_DRAFT_ATTEMPTS = 2
DRAFT_TIMEOUT_SECONDS = 90


# ── Data types ──

@dataclass
class Candidate:
    """A potential article: a single agenda item from a meeting."""
    meeting_id: int          # meetings.id (PK)
    meeting_vid: str         # meetings.meeting_id (VARCHAR)
    meeting_date: str
    meeting_type: str
    body_code: str
    body_name: str
    jurisdiction: str
    item_number: str
    item_title: str
    item_text: str
    case_number: str
    source_url: str
    agenda_item_url: str
    score: int = 0
    score_breakdown: dict = field(default_factory=dict)
    pitch: str = ""


@dataclass
class ArticleDraft:
    """A completed article draft ready to save."""
    candidate: Candidate
    title: str
    summary: str
    body: str
    tags: list[str]


# ── Scoring ──

NEWSWORTHY_KEYWORDS = {
    "public hearing": "public_hearing",
    "policy change": "policy_change",
    "zoning amendment": "policy_change",
    "ordinance": "policy_change",
    "code change": "policy_change",
    "rezone": "winners_losers",
    "rezoning": "winners_losers",
    "variance": "winners_losers",
    "conditional use": "winners_losers",
    "development agreement": "winners_losers",
    "special use": "winners_losers",
    "general plan": "policy_change",
    "comprehensive plan": "policy_change",
    "subdivision": "winners_losers",
    "appeal": "winners_losers",
}

DOLLAR_PATTERN = re.compile(r'\$[\d,]+[KkMmBb]|\$[\d,]{4,}')


def score_candidate(candidate: Candidate) -> tuple[int, dict]:
    """Score a candidate 0–7 on newsworthiness.

    Returns (total_score, breakdown_dict).
    """
    text = (candidate.item_text + " " + candidate.item_title).lower()
    breakdown = {}

    # Keyword-based scoring (public hearing is included in NEWSWORTHY_KEYWORDS)
    for keyword, key in NEWSWORTHY_KEYWORDS.items():
        if keyword in text:
            if key not in breakdown:
                breakdown[key] = 1

    # Dollar amount
    if DOLLAR_PATTERN.search(candidate.item_text):
        breakdown["dollar_amount"] = 1

    # Resident impact keywords
    impact_kw = ["housing", "affordable", "water", "transportation",
                 "traffic", "safety", "park", "school", "road",
                 "utility", "fee", "tax", "budget"]
    if any(k in text for k in impact_kw):
        breakdown["resident_impact"] = 1

    # Timeliness — today/tomorrow gets +1
    today = dt.date.today().isoformat()
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    if candidate.meeting_date in (today, tomorrow):
        breakdown["timeliness"] = 1

    # Follow-up potential (references prior discussion, continuance, etc.)
    followup_kw = ["continued from", "continuance", "prior meeting",
                   "previously", "rescheduled", "appeal of"]
    if any(k in text for k in followup_kw):
        breakdown["follow_up"] = 1

    total = sum(breakdown.values())
    return total, breakdown


def is_administrative(item_title: str, item_text: str) -> bool:
    """Check if an agenda item is administrative (not newsworthy)."""
    combined = (item_title + " " + item_text).lower()
    admin_phrases = [
        "minutes", "roll call", "adjourn", "call to order",
        "announcement", "pledge of allegiance", "land acknowledgement",
        "invocation", "moment of silence", "consent agenda",
        "routine approval", "acknowledgement", "appreciation",
        "recognize", "introduction of", "closed session",
        "executive session", "recess", "calendar review",
        "future agenda items", "council comments", "staff comments",
        "director's report", "chair's report",
    ]
    return any(p in combined for p in admin_phrases)


# ── Query ──

def fetch_meetings(session, days_back: int, days_fwd: int) -> list:
    """Fetch all meetings in the window with at least one agenda item."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=days_back)).isoformat()
    end = (today + dt.timedelta(days=days_fwd)).isoformat()

    rows = session.execute(
        sql_text("""
            SELECT m.id, m.body, m.meeting_id, m.meeting_date, m.meeting_type,
                   m.source_url,
                   pb.name AS body_name, j.name AS jurisdiction_name
            FROM meetings m
            JOIN public_bodies pb ON pb.id = m.public_body_id
            JOIN jurisdictions j ON j.id = pb.jurisdiction_id
            WHERE m.meeting_date >= :start
              AND m.meeting_date <= :end
            ORDER BY m.meeting_date ASC
        """),
        {"start": start, "end": end}
    ).fetchall()

    return [dict(r._mapping) for r in rows]


def fetch_agenda_items(session, meeting_db_id: int) -> list:
    """Fetch all agenda items for a meeting."""
    rows = session.execute(
        sql_text("""
            SELECT id, agenda_item_number, agenda_item_title, agenda_item_text,
                   case_number, source_url, agenda_item_url
            FROM agenda_items
            WHERE meeting_db_id = :mid
            ORDER BY sort_order ASC, agenda_item_number ASC
        """),
        {"mid": meeting_db_id}
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def fetch_supporting_docs(session, meeting_db_id: int, item_number: str) -> list:
    """Fetch supporting documents for a specific agenda item."""
    rows = session.execute(
        sql_text("""
            SELECT id, file_name, document_url, local_path
            FROM supporting_documents
            WHERE meeting_db_id = :mid
              AND agenda_item_number = :item_num
        """),
        {"mid": meeting_db_id, "item_num": item_number}
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def fetch_minutes_url(session, meeting_db_id: int) -> Optional[str]:
    """Fetch minutes URL if available."""
    row = session.execute(
        sql_text("SELECT minutes_url FROM meetings WHERE id = :mid"),
        {"mid": meeting_db_id}
    ).fetchone()
    return row[0] if row and row[0] else None


def fetch_prior_case_appearances(session, case_number: str) -> list[dict]:
    """Find all meetings where this case number appears."""
    if not case_number:
        return []
    rows = session.execute(
        sql_text("""
            SELECT m.meeting_date, m.meeting_id, m.meeting_type,
                   pb.name AS body_name, ai.agenda_item_number, ai.agenda_item_title
            FROM agenda_items ai
            JOIN meetings m ON m.id = ai.meeting_db_id
            JOIN public_bodies pb ON pb.id = m.public_body_id
            WHERE ai.case_number = :cn
               OR ai.c_number = :cn
               OR ai.agenda_item_text LIKE :cn_like
            ORDER BY m.meeting_date ASC
        """),
        {"cn": case_number, "cn_like": f"%{case_number}%"}
    ).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Candiate Generation ──

def generate_candidates(session) -> list[Candidate]:
    """Generate full list of qualified candidates across all meetings."""
    meetings = fetch_meetings(session, NEWS_DAYS_BACK, NEWS_DAYS_FWD)
    candidates = []

    for m in meetings:
        items = fetch_agenda_items(session, m["id"])
        for item in items:
            title = (item["agenda_item_title"] or "").strip()
            text = (item["agenda_item_text"] or "").strip()
            combined = title + " " + text

            if not title and not text:
                continue
            if is_administrative(title, text):
                continue

            candidate = Candidate(
                meeting_id=m["id"],
                meeting_vid=m["meeting_id"],
                meeting_date=m["meeting_date"],
                meeting_type=m["meeting_type"],
                body_code=m["body"],
                body_name=m["body_name"],
                jurisdiction=m["jurisdiction_name"],
                item_number=item["agenda_item_number"],
                item_title=title[:500],
                item_text=text[:3000],
                case_number=item["case_number"] or "",
                source_url=m["source_url"] or "",
                agenda_item_url=item["agenda_item_url"] or "",
            )
            score, breakdown = score_candidate(candidate)
            candidate.score = score
            candidate.score_breakdown = breakdown

            # Skip items with thin evidence (< 500 chars total) — the model
            # will fabricate details to reach the 500-word target.
            evidence_len = len(candidate.item_text) + len(candidate.item_title)
            if evidence_len < 500:
                print(f"    SKIP ({candidate.jurisdiction}): {candidate.item_title[:60]} — "
                      f"only {evidence_len} chars evidence, need 500+")
                continue

            if score >= 3:
                candidates.append(candidate)

    return candidates


def deduplicate_candidates(session, candidates: list[Candidate]) -> list[Candidate]:
    """Remove duplicates by case number across pipeline stages.

    Returns filtered list with dedup notes printed.
    """
    dedup_stats = {"skipped_existing_article": 0, "skipped_lower_stage": 0}
    seen_case_numbers = set()
    seen_item_titles = set()
    result = []

    # Check existing articles for case number coverage
    existing_articles = session.execute(
        sql_text("SELECT DISTINCT item_title FROM article_sources")
    ).fetchall()
    existing_titles = {r[0].lower().strip() for r in existing_articles if r[0]}

    for c in candidates:
        # Dedup by case number
        if c.case_number:
            if c.case_number in seen_case_numbers:
                continue
            # Check if article already covers this case
            article_check = session.execute(
                sql_text("""
                    SELECT a.id, a.title FROM article_sources a_src
                    JOIN articles a ON a.id = a_src.article_id
                    WHERE a_src.item_title LIKE :cn
                       OR a_src.item_title LIKE :cn2
                    LIMIT 1
                """),
                {"cn": f"%{c.case_number}%", "cn2": f"%{c.case_number}%"}
            ).fetchone()
            if article_check:
                dedup_stats["skipped_existing_article"] += 1
                continue
            seen_case_numbers.add(c.case_number)

        # Dedup by very similar title (same case discussed at diff bodies)
        title_key = c.item_title.lower().strip()[:100]
        if title_key in seen_item_titles:
            dedup_stats["skipped_lower_stage"] += 1
            continue
        seen_item_titles.add(title_key)

        result.append(c)

    # Print dedup summary
    total_skipped = len(candidates) - len(result)
    if total_skipped > 0:
        print(f"  Dedup: {dedup_stats['skipped_existing_article']} existing articles, "
              f"{dedup_stats['skipped_lower_stage']} lower-stage duplicates "
              f"({total_skipped} total removed)")

    return result


def select_top_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Select top 8 candidates with geographic diversity.

    Rules:
    - Up to 8 by score
    - Max 2 per jurisdiction
    - Max 2 per meeting
    """
    candidates.sort(key=lambda c: c.score, reverse=True)
    selected = []
    juris_count = {}
    meeting_count = {}

    for c in candidates:
        if len(selected) >= MAX_ARTICLES * 2:
            break
        j = c.jurisdiction
        mk = (c.jurisdiction, c.meeting_vid)

        if juris_count.get(j, 0) >= 2:
            continue
        if meeting_count.get(mk, 0) >= 2:
            continue

        selected.append(c)
        juris_count[j] = juris_count.get(j, 0) + 1
        meeting_count[mk] = meeting_count.get(mk, 0) + 1

    return selected


def pitch_and_rank(client, session, candidates: list[Candidate]) -> list[Candidate]:
    """Use LLM to evaluate candidates on story quality and return top 4.

    For each candidate, sends the context to gpt-4o-mini for a quick
    editorial pitch evaluation on: stakes, source depth, interesting details.
    Returns only candidates with composite rating >= 3 on all axes,
    sorted by quality.
    """
    scored = []

    for c in candidates:
        context = build_context(session, c)
        context_truncated = context[:4000]  # Keep prompt small for speed

        pitch_prompt = 'You are an editorial assistant evaluating story ideas. ' + \
            'Rate this candidate on three axes (1-5) and respond with ONLY a JSON object. ' + \
            'The JSON keys are: stakes (1-5), source_depth (1-5), details (1-5), pitch (string). ' + \
            'Stakes = is a real decision being made? Does it affect real people? ' + \
            'Source_depth = is there specific detail (dollar amounts, addresses, counts)? ' + \
            'Details = interesting, specific facts to hang a story on? ' + \
            'Be honest. A routine consent item with no detail gets 1s. ' + \
            f'Candidate: {c.jurisdiction} | {c.body_name} | {c.meeting_date}. ' + \
            f'Item {c.item_number}: {c.item_title}. ' + \
            f'Context: {context_truncated[:2500]}'

        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": pitch_prompt}],
                temperature=0.3,
                max_tokens=300,
                timeout=30,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            import json
            data = json.loads(raw)
            avg = (data.get("stakes", 1) + data.get("source_depth", 1) + data.get("details", 1)) / 3.0
            c.pitch = data.get("pitch", "")
            scored.append((avg, c))
            print(f"    Pitch: {c.item_title[:50]}... "
                  f"stakes={data.get('stakes','?')} depth={data.get('source_depth','?')} "
                  f"details={data.get('details','?')} avg={avg:.1f}")
        except Exception as e:
            print(f"    Pitch FAILED: {c.item_title[:50]}... ({e})")
            scored.append((0, c))

    # Sort by score descending, filter to >= 2.0, take top MAX_ARTICLES
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [c for avg, c in scored if avg >= 2.0][:MAX_ARTICLES]

    print(f"    After pitching: {len(selected)} candidates selected")
    if selected:
        for i, c in enumerate(selected):
            print(f"      {i+1}. [{c.jurisdiction}] {c.body_name} "
                  f"\"{c.item_title[:50]}\" \u2014 {getattr(c, 'pitch', '')[:80]}")

    return selected


def extract_pdf_text(url: str, max_chars: int = 5000) -> str:
    """Download and extract text from a PDF URL using pypdf.

    Returns empty string on failure.
    """
    import tempfile
    import urllib.request
    import urllib.error
    from pypdf import PdfReader

    if not url:
        return ""

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Check content type — skip if not PDF
            ct = resp.headers.get("Content-Type", "")
            if "pdf" not in ct and "octet" not in ct:
                return ""
            data = resp.read()
    except Exception:
        return ""

    if len(data) > 10 * 1024 * 1024:  # Skip PDFs > 10MB
        return ""

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            reader = PdfReader(tmp.name)
            text_parts = []
            total = 0
            for page in reader.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                total += len(page_text)
                if total > max_chars:
                    break
            return "\n".join(text_parts)[:max_chars]
    except Exception:
        return ""


# ── Context Gathering ──

def build_context(session, candidate: Candidate) -> str:
    """Build a structured context string for the LLM to draft from."""
    parts = []

    # Meeting context
    parts.append(f"## Meeting Context")
    parts.append(f"- Jurisdiction: {candidate.jurisdiction}")
    parts.append(f"- Body: {candidate.body_name}")
    parts.append(f"- Date: {candidate.meeting_date}")
    parts.append(f"- Type: {candidate.meeting_type}")
    parts.append(f"- Source URL: {candidate.source_url or 'N/A'}")

    # Meeting URL paths for inline links
    meeting_path = f"/meetings/{candidate.body_code}/{candidate.meeting_vid}"
    item_path = f"/meetings/{candidate.body_code}/{candidate.meeting_vid}?item={candidate.item_number}"
    parts.append(f"- Meeting page path (for inline links): {meeting_path}")
    parts.append(f"- Agenda item path (for inline links): {item_path}")
    parts.append("")

    # Agenda item
    parts.append(f"## Agenda Item {candidate.item_number}")
    parts.append(f"- Title: {candidate.item_title}")
    if candidate.item_text:
        parts.append(f"- Text:\n{candidate.item_text}")
    parts.append("")

    # Supporting documents
    try:
        docs = fetch_supporting_docs(session, candidate.meeting_id, candidate.item_number)
        if docs:
            parts.append(f"## Supporting Documents ({len(docs)})")
            for d in docs:
                parts.append(f"- {d['file_name'] or 'Untitled'}")
            parts.append("")
        # Extract text from supporting PDFs when available
        for d in docs:
            url = d.get("document_url") or ""
            if url:
                pdf_text = extract_pdf_text(url, max_chars=3000)
                if pdf_text:
                    parts.append(f"### PDF Content: {d['file_name'] or 'Untitled'}")
                    parts.append(pdf_text[:3000])
                    parts.append("")
    except Exception:
        pass

    # Prior case history
    if candidate.case_number:
        appearances = fetch_prior_case_appearances(session, candidate.case_number)
        if appearances:
            parts.append(f"## Prior Appearances (Case: {candidate.case_number})")
            for a in appearances:
                parts.append(f"- {a['meeting_date']} | {a['body_name']} | "
                           f"Item {a.get('agenda_item_number', '?')}: "
                           f"{a.get('agenda_item_title', 'N/A')}")
            parts.append("")

    # Minutes URL
    minutes_url = fetch_minutes_url(session, candidate.meeting_id)
    if minutes_url:
        parts.append(f"## Minutes URL: {minutes_url}")
        parts.append("")

    return "\n".join(parts)


# ── Article Drafting (OpenAI) ──

DRAFT_SYSTEM_PROMPT = """You are a local-government news writer for Poliscopic, a hyperlocal news site covering municipal government in Maricopa County, Arizona.

Write a news article based on the meeting context below. Follow these rules exactly. The word target depends on how much evidence is available: if the context has rich detail (multiple paragraphs), aim for 500-600 words; if the context is thin (just a title and a sentence or two), write a concise factual summary of 200-300 words.

## Structure

### Action — lede paragraph
Open with a lede that delivers on the promise of the summary. Use a specific fact: the actual development name, the exact dollar amount, the affected neighborhood. Include the meeting tracker link inline: the meeting body name linked to the meeting page.

### Details — what, where, how much
Cost, location, timeline, vote count. Who voted for and against. How the proposal works. Specifics only.

### Context — why this matters
Broader trend, policy history, resident impact. Frame the stakes: who benefits, who pays, what changes. No editorializing — let the facts establish importance.

### Related Developments — what else
Other items from the same meeting that relate to the story. A related case at another body. Upcoming votes.

## Progressive Disclosure
- **Title**: Core action in plain language. No unexplained acronyms. 8-14 words.
- **Summary**: 1-2 sentences that state what’s at stake. Identify the decision being made, what’s changing, or the conflict. State it as a fact that implies stakes — not as a question. Examples:
  - A good summary: “A 300-unit apartment complex would replace the empty Bashas’ grocery on Arizona Avenue, but neighbors say the traffic study doesn’t account for school bus routes.”
  - Another good summary: “Goodyear is rewriting its zoning code for the first time in 27 years, a process that will reshape how housing, data centers, and battery storage get built across the city.”
  - Weak (avoid this): “What do these changes mean for residents? The council will decide Tuesday.”
- **Summary variety**: Do NOT start every summary with a statement followed by a question. Vary the format:
  - Stakes-first: "A proposed 300-unit complex on Arizona Avenue would reshape downtown Chandler's east entrance."
  - Tension: "Neighbors say a traffic study is flawed, but the developer says the project meets all requirements."
  - Trend: "The city is updating its zoning code for the first time since 1999, with implications for housing density across Goodyear."
  - Direct question: "Should Tempe raise property taxes by 2.5%? The council will decide Tuesday."
- **Lede** (body first paragraph): Deliver on the summary's promise. The lede answers the question the summary raised, with a specific person, place, or number.

The title, summary, and lede must each ADD new information. If any layer repeats the previous one, you are writing an echo chamber — fix it.

## LINKING RULES (Critical)
The context block below includes the meeting URL paths. You MUST write inline markdown links on descriptive text:
- Link format: `[Goodyear Planning & Zoning Commission Meeting, July 2, 2026](/meetings/goodyear-pz/2285)` — descriptive label, THEN the URL in parens.
- Link the meeting body name in the FIRST PARAGRAPH to the meeting page.
- Item-level links: `[the proposed zoning ordinance update](/meetings/goodyear-pz/2285?item=3)` — link the subject of a specific claim.
- Staff reports as links only if a document URL is provided in context.
- Do NOT use bare URLs. Do NOT use "click here" or "view source" or "view agenda" as link text.
- Do NOT repeat the meeting link. One meeting link in the first paragraph, item-level links in specific paragraphs. No link dump at the end.
- Each distinct source may be linked only once in the body. The meeting tracker link is the only exception.

## Writing Rules (from Poliscopic Style Guide)
- **Word target**: 500-600 words if the context has rich detail. If the context is thin (just a title and a sentence), write a concise 200-300 word summary. Count as you write.
- Plain English, active voice, short paragraphs (2-4 sentences).
- One idea per paragraph.
- Define acronyms on first use: "Chandler City Council" not "chandler-cc".
- No editorializing: never use "critical", "significant", "notable", "important", "worth watching", "key".
- No rhetorical questions. Just answer the question.
- No "It's not X, it's Y" constructions. State what it is.
- No repetition. Once a point is made, move on.
- No closing moral paragraph. The story's conclusion comes from the structure, not a paragraph that tells the reader what to think.
- No excessive em-dashes. Use commas or new sentences.
- Do not write "This decision" or "This development" to refer back — use the specific name.
- **ANTI-PATTERNS (do not do these):**
  - Do NOT end with "For more details" or "Visit the meeting page" or "View the full agenda" — the meeting link belongs inline in the first paragraph.
  - Do NOT add a "In related developments" section that lists generic or made-up items from the meeting.
  - Do NOT write "council members expressed their support" or "reflected a shared understanding" — that is editorializing.
  - Do NOT say "the council is expected to" unless the context explicitly says something is scheduled.
  - Do NOT add a call-out box, sources list, or "for more information" line at the end.

## Evidence Matching
- If you have agenda text but NOT meeting minutes: use "staff recommends", "the proposal would", "the council will consider" — NOT "approved", "voted", "decided".
- If the agenda item has text describing staff's position: attribute to "staff recommended..."
- If you have a case number: reference what the case covers.
- Do NOT express more certainty than the source supports.

## Format
Return ONLY a JSON object with exactly these keys, no markdown fences, no extra text:
{
  "title": "...",
  "summary": "...",
  "body": "..."
}"""


def draft_article(client: OpenAI, context: str, attempt: int = 1) -> Optional[ArticleDraft]:
    """Call OpenAI API to draft an article from context.

    Returns ArticleDraft or None on failure.
    """
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Write an article based on this context:\n\n{context}"},
            ],
            temperature=0.5,
            max_tokens=2000,
            timeout=DRAFT_TIMEOUT_SECONDS,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"  OpenAI API error: {e}", file=sys.stderr)
        return None

    content = response.choices[0].message.content.strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        print(f"  JSON parse error, raw length={len(content)}", file=sys.stderr)
        return None

    title = data.get("title", "").strip()
    summary = data.get("summary", "").strip()
    body = data.get("body", "").strip()

    if not title or not body:
        print(f"  Empty title or body from LLM", file=sys.stderr)
        return None

    word_count = len(body.split())
    if word_count < 400:
        print(f"  Too short: {word_count} words (< 400), rejecting")
        return None
    if word_count < 500:
        print(f"  Warning: only {word_count} words (target 500-600), accepting but flagging")

    return ArticleDraft(
        candidate=None,  # will be set by caller
        title=title,
        summary=summary,
        body=body,
        tags=[],
    )

# ── Hyperlinking ──

# ── Hyperlinking ──

def hyperlink_body(body: str, candidate: Candidate, session) -> str:
    """Clean up body text post-draft.

    The model should write inline markdown links using meeting URL paths
    from the context. This function:
    1. Strips remaining [S-label] artifacts
    2. Strips bad link labels ("View source:", "Click here", etc.)
    3. Strips "For more details..." paragraphs at the end
    4. Moves meeting link to first paragraph if only at bottom
    5. Cleans whitespace
    """
    result = body
    meeting_path = f"/meetings/{candidate.body_code}/{candidate.meeting_vid}"
    item_path = f"{meeting_path}?item={candidate.item_number}"

    # 1. Strip remaining [S-label] artifacts
    result = re.sub(r'\[S\d+\]', '', result)

    # 2. Strip bad link labels
    bad_prefixes = [
        r'\[View source[^\]]*\]\([^)]+\)',
        r'\[Source[^\]]*\]\([^)]+\)',
        r'\[Agenda[^\]]*\]\([^)]+\)',
        r'\[Click here[^\]]*\]\([^)]+\)',
        r'\[View the[^\]]*\]\([^)]+\)',
        r'\[View[^\]]*\]\([^)]+\)',
    ]
    for pat in bad_prefixes:
        result = re.sub(pat, '', result)

    # 3. Strip call-out paragraphs at the end of the article
    callout_patterns = [
        r'\n\nFor more details[^.]*\.(\s*\[[^\]]+\]\([^)]+\))?',
        r'\n\nFor more information[^.]*\.(\s*\[[^\]]+\]\([^)]+\))?',
        r'\n\nFor those interested[^.]*\.',
        r'\n\nRefer to the[^.]*\.(\s*\[[^\]]+\]\([^)]+\))?',
        r'\n\nVisit the[^.]*\.(\s*\[[^\]]+\]\([^)]+\))?',
        r'\n\nUpcoming discussions[^.]*\.',
        r'\n\nIn addition to the[^.]*\.',
        r'\n\nResidents are encouraged[^.]*\.',
        r'\n\nAs the city prepares[^.]*\.',
    ]
    for cp in callout_patterns:
        result = re.sub(cp, '', result)

    # 3.5 Strip bare URLs (not wrapped in markdown link syntax)
    # Match http/https URLs not inside [text](url) markdown
    bare_url_pattern = r'(?<!\]\( )https?://[^\s)\]>]+'
    result = re.sub(bare_url_pattern, '', result)

    paras = [p for p in result.split('\n\n') if p.strip()]
    first_link_para = -1
    
    # Find all paragraphs with meeting links
    for i, p in enumerate(paras):
        if re.search(r'/meetings/', p):
            first_link_para = i
            break
    
    if first_link_para < 0:
        # No meeting link anywhere — add fallback to first paragraph
        fallback = f"[{candidate.body_name} Meeting, {candidate.meeting_date}]({meeting_path})"
        paras[0] = paras[0].rstrip('.!?') + f'. {fallback}'
    elif first_link_para > 0:
        # Meeting link exists but not in first paragraph — extract and move it
        link_match = re.search(r'\[([^\]]+)\]\(/meetings/[^)]+\)', paras[first_link_para])
        if link_match:
            link_text = link_match.group(0)
            paras[first_link_para] = re.sub(r'\s*\[[^\]]+\]\(/meetings/[^)]+\)', '', paras[first_link_para])
            paras[first_link_para] = paras[first_link_para].strip()
            paras[0] = paras[0].rstrip('.!?') + f'. {link_text}'
    
    # Remove empty paragraphs
    paras = [p for p in paras if p.strip()]
    result = '\n\n'.join(paras)

    # 5. Clean up spacing and trailing whitespace
    result = re.sub(r'  +', ' ', result)
    result = re.sub(r'\s+([.,:;!?])', r'\1', result)
    result = result.strip()

    return result

def get_or_create_tag(session, tag_slug: str, tag_name: str) -> Tag:
    """Get existing tag or create a new one."""
    with session.no_autoflush:
        tag = session.query(Tag).filter(Tag.slug == tag_slug).first()
        if not tag:
            tag = Tag(slug=tag_slug, name=tag_name, description="")
            session.add(tag)
            session.flush()
        return tag


def slugify(text: str) -> str:
    """Create a URL-friendly slug from text."""
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    slug = slug.strip('-')
    date_prefix = dt.date.today().isoformat()
    return f"{date_prefix}-{slug[:80]}"


def save_article(session, draft: ArticleDraft) -> Optional[int]:
    """Save a drafted article to the database.

    Returns article ID or None on failure.
    """
    candidate = draft.candidate
    slug = slugify(draft.title)

    # Check for duplicate slug
    existing = session.query(Article).filter(Article.slug == slug).first()
    if existing:
        slug = slug[:200] + "-2"

    # Determine tags — auto-detect from body/summary
    tag_names = draft.tags or []
    juris_slug = candidate.jurisdiction.lower().replace(" ", "-")
    tag_names.append(juris_slug)

    # Auto-detect topic tags from body + summary
    combined = (draft.title + " " + draft.summary + " " + draft.body).lower()
    topic_rules = [
        ("budget", ["budget", "tax", "fee", "fiscal", "appropriation",
                     "$", "million", "expenditure", "revenue"]),
        ("housing", ["housing", "apartment", "residential", "multi-family",
                      "multifamily", "affordable", "home", "dwelling"]),
        ("zoning", ["zoning", "rezone", "rezoning", "land use",
                     "general plan", "comprehensive plan", "overlay"]),
        ("development", ["development", "subdivision", "plat", "construction",
                          "building", "site plan"]),
        ("transportation", ["transportation", "traffic", "road", "street",
                             "transit", "light rail", "sidewalk", "bike"]),
        ("water", ["water", "drainage", "flood", "wastewater", "sewer",
                    "water resource", "irrigation"]),
        ("environment", ["environment", "sustainability", "energy",
                          "solar", "climate", "emission", "conservation"]),
        ("public-safety", ["police", "fire", "emergency", "safety",
                            "ambulance"]),
        ("parks", ["park", "recreation", "trail", "open space", "playground"]),
        ("economy", ["economic development", "incentive", "job", "business",
                      "commercial", "retail"]),
        ("education", ["school", "library", "education", "student", "teacher"]),
        ("government", ["ordinance", "resolution", "policy", "code",
                         "regulation", "amendment", "adoption"]),
        ("health", ["health", "hospital", "medical", "clinic", "disease"]),
        ("data-centers", ["data center", "hyperscale", "server", "compute"]),
    ]
    for slug_key, keywords in topic_rules:
        if any(k in combined for k in keywords):
            if slug_key not in tag_names:
                tag_names.append(slug_key)

    # Hyperlink body
    body = hyperlink_body(draft.body, candidate, session)

    # Clean trailing spaces before punctuation
    body = re.sub(r'\s+([.,:;!?])', r'\1', body)

    article = Article(
        title=draft.title,
        slug=slug,
        summary=draft.summary,
        body=body,
        status="draft",
        featured_image="",
        image_credit="",
    )

    tag_map = {
        "housing": "Housing",
        "zoning": "Zoning",
        "data-centers": "Data Centers",
        "development": "Development",
        "budget": "Budget",
        "transportation": "Transportation",
        "water": "Water",
        "environment": "Environment",
        "public-safety": "Public Safety",
        "government": "Government",
        "parks": "Parks",
        "economy": "Economy",
        "health": "Health",
        "education": "Education",
    }
    juris_slugs = {
        "Maricopa County": "maricopa-county",
        "City of Phoenix": "phoenix",
        "City of Mesa": "mesa",
        "City of Chandler": "chandler",
        "City of Tempe": "tempe",
        "City of Scottsdale": "scottsdale",
        "City of Glendale": "glendale",
        "City of Peoria": "peoria",
        "City of Surprise": "surprise",
        "City of Buckeye": "buckeye",
        "City of Gilbert": "gilbert",
        "City of Avondale": "avondale",
        "City of Goodyear": "goodyear",
        "City of El Mirage": "el-mirage",
    }

    tags_added = set()
    for tn in tag_names:
        tn_clean = tn.strip().lower()
        if tn_clean in tag_map:
            t = get_or_create_tag(session, tn_clean, tag_map[tn_clean])
            if tn_clean not in tags_added:
                article.tags.append(t)
                tags_added.add(tn_clean)
        elif tn_clean in juris_slugs:
            juris_name = tn_clean
            for full_name, slug_name in juris_slugs.items():
                if slug_name == tn_clean:
                    juris_name = full_name
                    break
            t = get_or_create_tag(session, tn_clean, juris_name)
            if tn_clean not in tags_added:
                article.tags.append(t)
                tags_added.add(tn_clean)
        else:
            display_name = tn_clean.replace("-", " ").title()
            t = get_or_create_tag(session, tn_clean, display_name)
            if tn_clean not in tags_added:
                article.tags.append(t)
                tags_added.add(tn_clean)

    source = ArticleSource(
        body=candidate.body_code,
        meeting_id=candidate.meeting_vid,
        agenda_item_number=candidate.item_number,
        source_url=candidate.source_url or candidate.agenda_item_url,
        source_type="agenda",
        item_title=candidate.item_title,
    )
    article.sources.append(source)

    try:
        session.add(article)
        session.flush()
        article_id = article.id

        try:
            docs = fetch_supporting_docs(session, candidate.meeting_id, candidate.item_number)
            for doc in docs:
                if doc.get("document_url"):
                    doc_source = ArticleSource(
                        article_id=article_id,
                        body=candidate.body_code,
                        meeting_id=candidate.meeting_vid,
                        agenda_item_number=candidate.item_number,
                        source_url=doc["document_url"],
                        source_type="staff_report",
                        item_title=doc["file_name"] or f"Staff report (item {candidate.item_number})",
                    )
                    session.add(doc_source)
        except Exception:
            pass

        session.commit()
        return article_id
    except Exception as e:
        session.rollback()
        print(f"  DB save error: {e}", file=sys.stderr)
        return None


def main():
    print(f"\U0001f4f0 Daily Article Pipeline \u2014 {dt.date.today().isoformat()}")
    print(f"   Window: last {NEWS_DAYS_BACK} days \u2192 next {NEWS_DAYS_FWD} days")
    print(f"   Model: {OPENAI_MODEL}")
    print()

    session = get_session()
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        timeout=DRAFT_TIMEOUT_SECONDS + 10,
    )

    print("Stage 1: Story Selection")
    candidates = generate_candidates(session)
    print(f"  Total candidates: {len(candidates)}")

    if not candidates:
        print("  No candidates found. Skipping.")
        print()
        print("\U0001f4f0 No newsworthy items found today.")
        session.close()
        return

    candidates = deduplicate_candidates(session, candidates)
    print(f"  After dedup: {len(candidates)}")

    if not candidates:
        print("  All candidates deduplicated. Skipping.")
        print()
        print("\U0001f4f0 All items already covered in existing articles.")
        session.close()
        return

    selected = select_top_candidates(candidates)
    print(f"  Selected: {len(selected)} articles")

    if not selected:
        print("  No articles selected. Skipping.")
        session.close()
        return

    for i, c in enumerate(selected):
        print(f"    {i+1}. [{c.jurisdiction}] {c.body_name} \u2014 "
              f"\"{c.item_title[:60]}\" (score={c.score}, items={sorted(c.score_breakdown.keys())})")
    print()

    print("Stage 1b: Pitch Evaluation")
    pitched = pitch_and_rank(client, session, selected)

    if not pitched:
        print("  No stories passed editorial pitch. Skipping.")
        session.close()
        return

    selected = pitched
    print()

    print("Stage 2: Drafting Articles")
    articles_drafted = []

    for i, candidate in enumerate(selected):
        print(f"\n  Draft {i+1}/{len(selected)}: [{candidate.jurisdiction}] {candidate.body_name}")
        print(f"    Item: {candidate.item_title[:80]}")

        context = build_context(session, candidate)
        context_truncated = context
        if len(context_truncated) > 12000:
            context_truncated = context_truncated[:12000] + "\n...[truncated]"

        draft = None
        for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
            print(f"    Drafting (attempt {attempt})...")
            result = draft_article(client, context_truncated, attempt)
            if result:
                wc = len(result.body.split())
                if wc >= 500:
                    draft = result
                    break
                else:
                    print(f"    Only {wc} words (need 500+) — retrying with length emphasis")
            if attempt < MAX_DRAFT_ATTEMPTS:
                print(f"    Re-prompting for more length...")
                length_prompt = (
                    "Your previous article was too short. Aim for 500-600 words if the context has enough detail. If the context is thin, write a concise 200-300 word summary instead of padding with invented specifics."
                    "Stick exactly to what the source evidence says. Do not add dollar amounts, addresses, vote counts, or timelines that are not in the source text."
                )
                try:
                    response = client.chat.completions.create(
                        model=OPENAI_MODEL,
                        messages=[
                            {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                            {"role": "user", "content": f"Write an article based on this context:\n\n{context_truncated}"},
                            {"role": "user", "content": length_prompt},
                        ],
                        temperature=0.5,
                        max_tokens=2500,
                        timeout=DRAFT_TIMEOUT_SECONDS,
                        response_format={"type": "json_object"},
                    )
                    content = response.choices[0].message.content.strip()
                    data = json.loads(content)
                    title = data.get("title", "").strip()
                    summary = data.get("summary", "").strip()
                    body = data.get("body", "").strip()
                    if title and body:
                        wc2 = len(body.split())
                        print(f"    Retry got {wc2} words")
                        draft = ArticleDraft(
                            candidate=None,
                            title=title,
                            summary=summary,
                            body=body,
                            tags=[],
                        )
                        break
                except Exception as e:
                    print(f"    Retry error: {e}")

        if draft is None:
            print(f"    \u274c Failed to draft after {MAX_DRAFT_ATTEMPTS} attempts")
            continue

        draft.candidate = candidate
        articles_drafted.append(draft)
        print(f"    \u2705 \"{draft.title}\" ({len(draft.body)} chars)")

    print()

    if not articles_drafted:
        print("No articles were successfully drafted.")
        session.close()
        return

    print("Stage 5: Saving to Database")
    saved_ids = []

    for i, draft in enumerate(articles_drafted):
        print(f"  Saving {i+1}/{len(articles_drafted)}: \"{draft.title}\"...", end=" ")
        aid = save_article(session, draft)
        if aid:
            saved_ids.append(aid)
            print(f"\u2705 article #{aid}")
        else:
            print("\u274c failed")

    print()
    print("\U0001f4f0 Morning articles ready")
    print()
    for i, aid in enumerate(saved_ids):
        draft = articles_drafted[i]
        print(f"{i+1}. [{draft.candidate.jurisdiction}] {draft.title} \u2014 draft")
    print()
    print("Review at: http://127.0.0.1:5001/admin/drafts")
    print()

    # Berry verification disabled - running from main session instead
    # if saved_ids:
    #     _kickoff_berry_verify(session, saved_ids)

    session.close()


def _kickoff_berry_verify(session, article_ids: list[int]):
    """Queue Berry verification in an isolated agent session.
    Writes an evidence file with article body + source text, then
    creates a one-shot cron job running gpt-4o-mini with
    require_citations=False.
    """
    import json
    import subprocess
    import time
    from sqlalchemy import text as sql_text

    evidence = {}
    for aid in article_ids:
        article = session.get(Article, aid)
        if not article:
            continue
        sources = []
        seen_metadata_keys = set()

        # For each unique meeting-body pair, build comprehensive context
        meeting_keys = set()
        for src in article.sources:
            meeting_keys.add((src.meeting_id, src.body))
        for (m_id, body_code) in meeting_keys:
            meeting_row = session.execute(
                sql_text("""
                    SELECT m.id AS db_id, m.meeting_date, m.meeting_type,
                           m.meeting_id, pb.name AS body_name,
                           j.name AS jurisdiction_name
                    FROM meetings m
                    JOIN public_bodies pb ON pb.id = m.public_body_id
                    JOIN jurisdictions j ON j.id = pb.jurisdiction_id
                    WHERE m.meeting_id = :mid AND m.body = :body
                    LIMIT 1
                """),
                {"mid": m_id, "body": body_code}
            ).fetchone()
            if not meeting_row:
                continue
            
            context_parts = [
                f"## Meeting: {meeting_row.body_name} ({meeting_row.meeting_date})",
                f"Jurisdiction: {meeting_row.jurisdiction_name}",
                f"Meeting type: {meeting_row.meeting_type}",
            ]
            item_texts = []
            for src in article.sources:
                if src.meeting_id == m_id and src.body == body_code and src.source_type == "agenda":
                    item_row = session.execute(
                        sql_text("""
                            SELECT agenda_item_text, agenda_item_title
                            FROM agenda_items
                            WHERE meeting_db_id = :db_id
                              AND agenda_item_number = :num
                            LIMIT 1
                        """),
                        {"db_id": meeting_row.db_id, "num": src.agenda_item_number or "0"}
                    ).fetchone()
                    if item_row and item_row.agenda_item_text:
                        item_texts.append(
                            f"Item {src.agenda_item_number}: {item_row.agenda_item_title}\n"
                            f"{item_row.agenda_item_text}"
                        )
            if item_texts:
                context_parts.append("")
                context_parts.extend(item_texts)
            context_text = "\n\n".join(context_parts)

            sources.append({
                "source_type": "context",
                "item_title": f"Drafting context for article #{aid}",
                "source_url": f"/meetings/{body_code}/{m_id}",
                "text": context_text,
            })
            meta_key = f"meta:{meeting_row.body_name}"
            if meta_key not in seen_metadata_keys:
                seen_metadata_keys.add(meta_key)
                sources.append({
                    "source_type": "meeting_metadata",
                    "item_title": f"{meeting_row.body_name} - {meeting_row.meeting_date}",
                    "source_url": "",
                    "text": f"Meeting of {meeting_row.body_name} on {meeting_row.meeting_date} ({meeting_row.meeting_type}).",
                })

        evidence[str(aid)] = {
            "title": article.title,
            "body": article.body,
            "summary": article.summary,
            "sources": sources,
        }

    stamp = int(time.time())
    path = f"/tmp/berry-evidence-{stamp}.json"
    with open(path, "w") as f:
        json.dump(evidence, f, indent=2)

    ids_str = " ".join(str(a) for a in article_ids)
    prompt = (
        f"Berry-verify pipeline draft articles {ids_str}. "
        f"Evidence file: {path}\n\n"
        "Read the file. For each article:\n"
        "1. berry__start_run(problem_statement='Verify article against sources')\n"
        "2. For each source, berry__add_span(text=source.text, meta={'source_type': source.source_type})\n"
        "3. berry__detect_hallucination_run("
        "answer=article.body, require_citations=False, context_mode='full', "
        "claim_split='sentences', max_evidence_chars=12000)\n"
        "4. Read the flagged claims. For each flagged sentence:\n"
        "   a. Identify the specific unsupported claim (e.g. 'voted 5-2', 'costs $2.1M').\n"
        "   b. Edit ONLY that sentence in the article body. Remove or rephrase the problematic claim.\n"
        "   c. Do NOT rewrite the paragraph or article — change only the flagged words.\n"
        "5. After fixing all flagged sentences, commit the revised body:\n"
        "   from db.newsroom import Article; from db.core import get_session\n"
        "   s = get_session(); a = s.get(Article, article_id); a.body = revised_body; s.merge(a); s.commit(); s.close()\n"
        "6. Re-verify: berry__detect_hallucination_run(answer=revised_body, ...). Loop until PASS or 3 attempts.\n"
        "7. Post a summary to #maricopa: title, how many claims were fixed, final pass/fail."
    )

    job_name = f"berry-verify-{stamp}"
    cmd = [
        "openclaw", "cron", "add",
        "--at", "+5s",
        "--name", job_name,
        "--description", f"Berry verify pipeline articles {ids_str}",
        "--agent", "maricopa",
        "--delete-after-run",
        "--announce",
        "--channel", "slack",
        "--to", "C0B7549FZE1",
        "--session", "isolated",
        "--model", "openai/gpt-5.4-mini",
        "--light-context",
        "--timeout-seconds", "600",
        "--message", prompt,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print(f"  Berry verification queued (isolated via gpt-5.4-mini)")
        else:
            print(f"  Berry verify launch error: {result.stderr[:300]}", file=sys.stderr)
    except FileNotFoundError:
        print("  openclaw CLI not found \u2014 Berry verification skipped", file=sys.stderr)
    except Exception as e:
        print(f"  Berry verify launch error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
