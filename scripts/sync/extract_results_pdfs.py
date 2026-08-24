#!/usr/bin/env python3
"""
extract_results_pdfs.py — Download and extract text from Phoenix AEM result PDFs.

Processes all meeting records with meeting_type='Result' that don't yet have
text extracted.  Downloads the PDF, runs pdftotext, and stores the result
as a SupportingDocument record with text_content.

Usage:
    python3 scripts/sync/extract_results_pdfs.py [--batch-size 50] [--max 100]
"""

import sys, os, time, logging, subprocess, tempfile, urllib.request, re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "scraper"))

from db import get_engine
from sqlalchemy import text
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("extract-results-pdfs")

BATCH_SIZE = 50
MAX_PDFS = 0
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": USER_AGENT}

CHUNK_SIZE = 8192

def extract_pdf_text(url: str) -> str | None:
    """Download a PDF and extract text using pdftotext."""
    tmp = None
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                tmp.write(chunk)
            tmp.close()

        result = subprocess.run(
            ["pdftotext", "-layout", tmp.name, "-"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("pdftotext failed for %s: %s", url, result.stderr[:100])
            return None
        text_output = result.stdout
        if not text_output.strip():
            log.warning("Empty text from %s", url)
            return None
        return text_output

    except Exception as e:
        log.warning("Failed to process %s: %s", url, e)
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def main():
    engine = get_engine()
    total_processed = 0
    total_bytes = 0
    errors = 0
    start_ts = time.time()

    while True:
        with engine.connect() as conn:
            # Find results that don't have supporting doc text extracted
            rows = conn.execute(text("""
                SELECT m.id, m.meeting_id, m.body, m.source_url, m.meeting_title
                FROM meetings m
                WHERE m.meeting_type = 'Result'
                  AND m.source_url != ''
                  AND NOT EXISTS (
                    SELECT 1 FROM supporting_documents sd
                    WHERE sd.meeting_id = m.meeting_id
                      AND sd.body = m.body
                      AND sd.document_type = 'Meeting Result'
                  )
                ORDER BY m.id
                LIMIT :limit
            """), {"limit": BATCH_SIZE}).fetchall()

        if not rows:
            log.info("No more results to process")
            break

        for row in rows:
            meeting_id = row[1]
            body = row[2]
            pdf_url = row[3]
            title = row[4] or ""

            if not pdf_url:
                continue

            # Skip non-Phoenix URLs
            if "phoenix.gov" not in pdf_url:
                continue

            log.info("[%d/%d] %s", total_processed + 1, total_processed + len(rows), pdf_url[-60:])

            text_output = extract_pdf_text(pdf_url)

            with engine.begin() as conn:
                # Look up the actual meeting_db_id
                meeting_db_id = conn.execute(text(
                    "SELECT id FROM meetings WHERE meeting_id = :mid AND body = :body LIMIT 1"
                ), {"mid": meeting_id, "body": body}).scalar()
                if not meeting_db_id:
                    log.warning("  Meeting not found in DB for %s/%s", body, meeting_id)
                    total_processed += 1
                    continue

                if text_output and text_output.strip():
                    total_bytes += len(text_output)

                    # Insert or update supporting_document
                    # Use doc_id from meeting_id
                    doc_id = f"result-{body}-{meeting_id}"

                    # Check if doc already exists
                    existing = conn.execute(text(
                        "SELECT id FROM supporting_documents WHERE document_url = :url LIMIT 1"
                    ), {"url": pdf_url}).fetchone()

                    if not existing:
                        # Create supporting_document
                        file_name = pdf_url.rsplit("/", 1)[-1] if "/" in pdf_url else ""
                        _, ext = (file_name.rsplit(".", 1) + [""])[:2]
                        now = datetime.now(timezone.utc)

                        conn.execute(text("""
                            INSERT INTO supporting_documents (
                                body, meeting_id, meeting_db_id, agenda_item_id,
                                agenda_item_number, document_title, document_url,
                                document_type, file_name, file_extension,
                                text_content, text_extracted_at, text_extraction_method,
                                extraction_duration_ms,
                                created_at, updated_at
                            ) VALUES (
                                :body, :mid, :db_id, :doc_id,
                                '0', :title, :url,
                                'Meeting Result', :fname, :ext,
                                :text, :now, 'pdftotext',
                                :duration,
                                :now, :now
                            )
                        """), {
                            "body": body,
                            "mid": meeting_id,
                            "db_id": meeting_db_id,
                            "doc_id": doc_id,
                            "title": (title or "Meeting Result")[:512],
                            "url": pdf_url,
                            "fname": file_name,
                            "ext": ext,
                            "text": text_output,
                            "now": now,
                            "duration": 0,
                        })
                        log.info("  Created supporting_doc, %d chars", len(text_output))
                    else:
                        log.info("  Already exists, skipping")
                else:
                    log.warning("  No text extracted")
                    # Insert stub so we don't reprocess this PDF on the next batch
                    existing = conn.execute(text(
                        "SELECT id FROM supporting_documents WHERE document_url = :url LIMIT 1"
                    ), {"url": pdf_url}).fetchone()

                    if not existing and meeting_db_id:
                        file_name = pdf_url.rsplit("/", 1)[-1] if "/" in pdf_url else ""
                        _, ext = (file_name.rsplit(".", 1) + [""])[:2]
                        now = datetime.now(timezone.utc)
                        conn.execute(text("""
                            INSERT INTO supporting_documents (
                                body, meeting_id, meeting_db_id, agenda_item_id,
                                agenda_item_number, document_title, document_url,
                                document_type, file_name, file_extension,
                                text_content, text_extracted_at, text_extraction_method,
                                extraction_duration_ms,
                                created_at, updated_at
                            ) VALUES (
                                :body, :mid, :db_id, :doc_id,
                                '0', :title, :url,
                                'Meeting Result', :fname, :ext,
                                '', :now, 'pdftotext-failed',
                                0,
                                :now, :now
                            )
                        """), {
                            "body": body,
                            "mid": meeting_id,
                            "db_id": meeting_db_id,
                            "doc_id": f"result-{body}-{meeting_id}-failed",
                            "title": (title or "Meeting Result")[:512],
                            "url": pdf_url,
                            "fname": file_name,
                            "ext": ext,
                            "now": now,
                        })
                        log.info("  Recorded extraction failure (stub)")

            total_processed += 1
            if MAX_PDFS and total_processed >= MAX_PDFS:
                log.info("Reached max PDFs (%d)", MAX_PDFS)
                break

            time.sleep(1)  # Rate limit

        if MAX_PDFS and total_processed >= MAX_PDFS:
            break

    elapsed = time.time() - start_ts
    rate = total_processed / elapsed if elapsed > 0 else 0
    log.info(
        "DONE: %d PDFs processed, %d MB text, %d errors, %.0fs, %.2f/s",
        total_processed, total_bytes / (1024 * 1024), errors, elapsed, rate,
    )


if __name__ == "__main__":
    main()
