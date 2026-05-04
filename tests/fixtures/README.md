# Agenda Online HTML fixtures

This directory is for offline test fixtures captured from Maricopa County Agenda Online HTML pages.

## Purpose

The fixture set should protect parser behavior without requiring live Agenda Online access during normal tests. Fixtures should represent real meeting-page structures from both 2025 and 2026.

## Capture script

Script:

```bash
python scripts/capture_fixtures.py --dry-run
python scripts/capture_fixtures.py
```

The script has an internal target list and dynamic discovery rules. It captures agenda HTML pages only:

- source URLs must be `Meetings/ViewMeeting?...&doctype=1`
- PDF `DownloadFile` URLs are refused
- fixture HTML is saved under `tests/fixtures/agendas/`
- manifest rows are written to `tests/fixtures/fixtures_manifest.csv`

Manifest columns:

```text
meeting_id,meeting_date,meeting_type,source_url,local_fixture_path,reason_included,validation_status,html_sha256,captured_at
```

## Initial target coverage

The initial script target set includes:

- `4470` — 2025-01-29 Special
- `4449` — 2025-01-29 Formal
- `4471` — 2025-01-27 Executive
- `4448` — 2025-01-27 Informal
- one later 2025 Formal from local metadata, if available
- one 2026 Formal from Agenda Online search, if available
- one 2026 Special or Informal from Agenda Online search, if available

## Safety rules

- Always run `--dry-run` first.
- Existing fixture files are skipped unless `--overwrite` is passed.
- The script prefers direct HTTP and falls back to Playwright only if needed.
- Use `--meeting-id <id>` to capture or debug one fixture at a time.
- The script never calls OpenAI APIs.
- Do not commit large unrelated captures; add fixtures intentionally with a parser-regression reason.
