"""
Email a report from the database to a recipient.
Converts markdown body to HTML with automatic hyperlinking of case numbers
and meeting references.
"""
import smtplib
import ssl
import sys
import os
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

EMAIL_FROM = "contact@poliscopic.com"
EMAIL_FROM_NAME = "Poliscopic"
EMAIL_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
if not EMAIL_PASSWORD:
    from dotenv import load_dotenv
    load_dotenv()
    EMAIL_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
if not EMAIL_PASSWORD:
    print("Error: EMAIL_APP_PASSWORD not set in .env")
    sys.exit(1)
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 587

# Base URL for links in emails (production by default)
# Override with POLISCOPIC_BASE_URL env var for local testing
BASE_URL = os.environ.get("POLISCOPIC_BASE_URL", "https://poliscopic.com").rstrip("/")
DEV_URL = "http://127.0.0.1:5001"


def build_case_url_map(session, body_text: str):
    """Scan the body text for case numbers and look up their document URLs.

    Returns a dict: case_number → first document URL from supporting_documents.
    """
    from db.models import SupportingDocument

    case_pattern = re.compile(r'\b([A-Z]{2,4}\d{5,8})\b')
    cases_found = set(case_pattern.findall(body_text))

    url_map = {}
    if cases_found:
        docs = session.query(SupportingDocument).filter(
            SupportingDocument.file_name != None
        ).all()
        for d in docs:
            fname = (d.file_name or '')
            m = re.search(r'([A-Z]{2,4}\d{5,8})', fname)
            if m and m.group(1) in cases_found and m.group(1) not in url_map:
                if d.document_url:
                    url_map[m.group(1)] = d.document_url
    return url_map


def build_meeting_descriptions(session, body_text: str):
    """Build a mapping of meeting URLs → descriptive text labels.

    Scans the body for meeting URL patterns and generates labels like
    'Planning & Zoning Commission Meeting, June 11, 2026'.
    """
    from db.models import Meeting, PublicBody

    # Find all meeting URLs in the body
    meeting_pattern = re.compile(r'http://127\.0\.0\.1:5001/meetings/([^/]+)/([^\s)\]]+)')
    meetings_found = set()
    for m in meeting_pattern.finditer(body_text):
        meetings_found.add((m.group(1), m.group(2)))

    label_map = {}
    for body_slug, meeting_id in meetings_found:
        meeting = session.query(Meeting).filter(
            Meeting.meeting_id == meeting_id
        ).first()
        if meeting:
            body = session.query(PublicBody).filter(
                PublicBody.id == meeting.public_body_id
            ).first()
            bname = body.name if body else body_slug
            date_str = meeting.meeting_date.strftime('%B %-d, %Y') if hasattr(meeting.meeting_date, 'strftime') else str(meeting.meeting_date)
            mtg_type = meeting.meeting_type
            if mtg_type and mtg_type.lower() not in ('regular', 'planning & zoning', 'board of adjustment', meeting.meeting_type):
                label = f"{bname} {mtg_type}, {date_str}"
            else:
                label = f"{bname} Meeting, {date_str}"
            label_map[f"{DEV_URL}/meetings/{body_slug}/{meeting_id}"] = label
    return label_map


def hyperlink_body(body_text: str, case_urls: dict, meeting_labels: dict) -> str:
    """Apply hyperlinks to the body text.

    NOTE: The report body already has hyperlinked source references created during
    drafting (e.g., [PZ Staff Report Z260015](url)). This function only:
    1. Replaces bare meeting URLs with descriptive link text
    2. Does NOT re-hyperlink already-linked content
    """
    result = body_text

    # Step 1: Replace meeting URL markdown links with descriptive labels
    # e.g., [View on poliscopic.com](url) → [Planning & Zoning Meeting, June 11](url)
    for url, label in meeting_labels.items():
        old_pattern = rf'\[.*?\]\({re.escape(url)}\)'
        new_link = f'[{label}]({url})'
        result = re.sub(old_pattern, new_link, result)

    return result


def markdown_to_html(md_text: str) -> str:
    """Convert markdown to HTML using Python's markdown library."""
    import markdown
    return markdown.markdown(
        md_text,
        extensions=[
            'extra',          # tables, fenced code, footnotes
            'sane_lists',     # sensible list behavior
            'toc',
        ]
    )


def send_article_email(article_id: int, recipient: str):
    """Send an article as a formatted HTML email."""
    sys.path.insert(0, 'scripts')
    from db.core import get_session
    from db.newsroom import Article

    s = get_session()
    article = s.query(Article).filter(Article.id == article_id).first()
    if not article:
        print(f"Error: Article #{article_id} not found.")
        sys.exit(1)

    body_md = article.body
    title = article.title

    # Build hyperlinks from database
    case_urls = build_case_url_map(s, body_md)
    meeting_labels = build_meeting_descriptions(s, body_md)
    body_md = hyperlink_body(body_md, case_urls, meeting_labels)

    # Replace dev URLs with production URLs for email
    # The database keeps local URLs for admin viewing; email gets public URLs
    body_md = body_md.replace(DEV_URL, BASE_URL)

    s.close()

    # Convert to HTML
    body_html = markdown_to_html(body_md)

    # Wrap tables in responsive scrollable container
    body_html = re.sub(
        r'(<table.*?</table>)',
        r'<div class="table-wrap">\1</div>',
        body_html,
        flags=re.DOTALL
    )

    # Build email
    full_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;line-height:1.6;color:#1a1a1a;max-width:720px;margin:0 auto;padding:20px;">
    <div style="border-bottom:2px solid #2563eb;padding-bottom:12px;margin-bottom:24px;">
        <h1 style="color:#2563eb;margin:0;font-size:22px;">{title}</h1>
        <p style="color:#666;margin:4px 0 0 0;font-size:13px;">Generated June 30, 2026 · Draft for review</p>
    </div>
    <div style="font-size:15px;">
        <style>
            /* Responsive tables for email */
            table {{border-collapse:collapse;margin:12px 0;font-size:13px;width:100%;max-width:100%;}}
            table th {{background:#2563eb;color:#fff;padding:6px 8px;text-align:left;font-weight:600;border:1px solid #ddd;}}
            table td {{padding:5px 8px;border:1px solid #ddd;vertical-align:top;}}
            table tr:nth-child(even) {{background:#f8f9fa;}}
            /* Mobile: wrap tables in scrollable container */
            .table-wrap {{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -8px;padding:0 8px;}}
            .table-wrap::before {{display:block;font-size:11px;color:#888;margin-bottom:4px;}}
            @media screen and (max-width:480px) {{
                table {{font-size:12px;}}
                table th, table td {{padding:4px 5px;}}
            }}
        </style>
        {body_html}
    </div>
    <div style="border-top:1px solid #ddd;margin-top:32px;padding-top:12px;font-size:12px;color:#888;">
        <p>Poliscopic — <a href="{BASE_URL}/admin/articles" style="color:#2563eb;">Review in admin →</a></p>
    </div>
</body>
</html>"""

    # Plain text fallback
    plain_text = body_md[:4000]
    if len(body_md) > 4000:
        plain_text += "\n\n... (truncated — view in admin for full report)"

    msg = MIMEMultipart('alternative')
    msg['Subject'] = title
    msg['From'] = formataddr((EMAIL_FROM_NAME, EMAIL_FROM))
    msg['To'] = recipient
    msg.attach(MIMEText(plain_text, 'plain'))
    msg.attach(MIMEText(full_html, 'html'))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, [recipient], msg.as_string())

    print(f"Email sent: '{title}' to {recipient}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Email a Poliscopic article')
    parser.add_argument('article_id', type=int, help='Article ID in the database')
    parser.add_argument('recipient', help='Email recipient')
    args = parser.parse_args()
    send_article_email(args.article_id, args.recipient)
