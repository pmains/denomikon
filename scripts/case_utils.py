"""Case number utilities for article deduplication."""
import re

CASE_PATTERN = re.compile(r'(?:Case\s*[#:]?\s*)([A-Z]{2,4}\d{5,8})')
CASE_PATTERN_NO_LABEL = re.compile(r'\b([A-Z]{2,4}\d{5,8})\b')


def extract_case_number(text: str) -> str | None:
    """Extract a Maricopa County case number from agenda item text.
    
    Matches patterns like: Case: Z260015, Case #: SU250007, MCP250001, etc.
    Returns the first case number found, or None.
    """
    if not text:
        return None
    # Prefer the labeled pattern (Case: X, Case #: X)
    m = CASE_PATTERN.search(text)
    if m:
        return m.group(1)
    # Fall back to bare pattern
    m = CASE_PATTERN_NO_LABEL.search(text)
    return m.group(1) if m else None


def extract_all_case_numbers(text: str) -> list[str]:
    """Extract all case numbers from text."""
    if not text:
        return []
    results = []
    for m in CASE_PATTERN.finditer(text):
        results.append(m.group(1))
    return results


def get_article_for_case(session, case_number: str):
    """Check if an article has already been written about a case.
    
    Searches ArticleSource records and article body text for the case number.
    Returns the Article if found, None otherwise.
    """
    from db.newsroom import Article, ArticleSource
    
    # Check ArticleSource records
    src = session.query(ArticleSource).filter(
        ArticleSource.item_title.ilike(f'%{case_number}%')
    ).first()
    if src:
        return session.query(Article).filter(Article.id == src.article_id).first()
    
    # Check article body
    article = session.query(Article).filter(
        Article.body.ilike(f'%{case_number}%')
    ).first()
    return article


def find_pipeline_for_case(session, case_number: str):
    """Find all appearances of a case across bodies.
    
    Returns list of (meeting_id, body_name, meeting_date, meeting_type) tuples
    sorted by date.
    """
    from db.models import AgendaItem, Meeting, PublicBody
    
    items = session.query(AgendaItem).filter(
        (AgendaItem.agenda_item_text.ilike(f'%{case_number}%')) |
        (AgendaItem.agenda_item_title.ilike(f'%{case_number}%'))
    ).all()
    
    results = []
    for item in items:
        meeting = session.query(Meeting).filter(Meeting.id == item.meeting_db_id).first()
        if meeting:
            body = session.query(PublicBody).filter(PublicBody.id == meeting.public_body_id).first()
            body_name = body.name if body else "Unknown"
            results.append({
                'meeting_id': meeting.id,
                'body': body_name,
                'date': meeting.meeting_date,
                'type': meeting.meeting_type,
                'item_number': item.agenda_item_number,
                'item_title': item.agenda_item_title,
            })
    
    results.sort(key=lambda r: r['date'])
    return results


def stage_in_pipeline(body_name: str, meeting_type: str) -> int:
    """Determine how far along a case is in the pipeline.
    
    Higher number = later stage. Used to pick which appearance to write about.
    """
    pipeline_order = [
        'planning & zoning commission',     # Stage 1: P&Z recommendation
        'planning & zoning',                 # (alias)
        'board of adjustment',               # Stage 2: BOA variance
        'board of supervisors',              # Stage 3: BOS decision
        'city council',                      # (same level for cities)
    ]
    key = (body_name or '').lower().strip()
    for i, stage in enumerate(pipeline_order):
        if stage in key:
            return i
    return -1


def should_skip(session, case_number: str, body_name: str, mtg_type: str) -> tuple[bool, str]:
    """Check if a case should be skipped due to dedup.
    
    Returns (skip: bool, reason: str).
    
    Skip rules:
    1. An article has already been written about this case → skip
    2. This case appears at an earlier body (e.g., P&Z) and also at a later
       body (e.g., BOS) — prefer the later stage unless the earlier one has
       the actual substantive decision
    """
    from db.newsroom import Article
    
    # Rule 1: Already covered?
    existing = get_article_for_case(session, case_number)
    if existing:
        return True, f"Already covered in article #{existing.id} '{existing.title}'"
    
    # Rule 2: Earlier stage in pipeline?
    appearances = find_pipeline_for_case(session, case_number)
    if len(appearances) > 1:
        current_stage = stage_in_pipeline(body_name, mtg_type)
        for a in appearances:
            other_stage = stage_in_pipeline(a['body'], a['type'])
            if other_stage > current_stage:
                return True, (
                    f"This case also appears at a later stage "
                    f"({a['body']} on {a['date']}). "
                    f"Prefer the later hearing unless it's an action-less continuation."
                )
    
    return False, ""
