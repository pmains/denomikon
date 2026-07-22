@echo off
setlocal enabledelayedexpansion

REM restore_from_backup.cmd
REM Restore production dump into dev database (Windows)
REM
REM Usage: restore_from_backup.cmd C:\path\to\prod-backup.dump
REM
REM Requirements:
REM   - PostgreSQL 18 pg_restore and psql on PATH (edit PG_BIN below if not)
REM   - PowerShell (built into Windows)
REM   - Dump file created with pg_dump --format=custom

set DUMP=%~1
if "%DUMP%"=="" (
    echo Usage: %~nx0 C:\path\to\prod-backup.dump
    exit /b 1
)
if not exist "%DUMP%" (
    echo Dump not found: %DUMP%
    exit /b 1
)

:: Adjust if PostgreSQL bin is not on PATH
set PG_BIN=
:: set PG_BIN=C:\Program Files\PostgreSQL\18\bin\

set DEV_DSN=host=localhost dbname=poliscopic_dev user=poliscopic password=CHANGEME

:: Verify connection
%PG_BIN%psql -d "%DEV_DSN:"=%" -c "SELECT 1" >nul 2>&1
if errorlevel 1 (
    echo Cannot connect to dev database.
    exit /b 1
)

echo ============================================================
echo Restoring from: %DUMP%
echo Target: poliscopic_dev
echo ============================================================

:: ── Phase 1: Truncate tables ──
echo.
echo -- Phase 1: Truncating tables --
for %%t in (meetings agenda_items agenda_item_votes member_votes supporting_documents meeting_members) do (
    %PG_BIN%psql -d "%DEV_DSN:"=%" -c "TRUNCATE TABLE public.%%t CASCADE;" 2>nul
    echo   Truncated %%t
)

:: ── Phase 2: Direct tables (1:1) ──
echo.
echo -- Phase 2: Restoring direct tables --
for %%t in (
    meetings agenda_items agenda_item_votes member_votes
    supporting_documents persons cases case_events body_memberships
    pz_item_details public_bodies public_body_members body_seats
    jurisdictions articles article_sources article_tags tags
    topics topic_weekly_reports entities entity_mentions entity_relationships
    executive_session_participants meeting_attendance
) do (
    echo   Restoring %%t...
    %PG_BIN%pg_restore --data-only --table=%%t -d "%DEV_DSN:"=%" "%DUMP%" 2>nul
)
echo   Direct tables done

:: ── Phase 3: meeting_supervisors → meeting_members ──
echo.
echo -- Phase 3: Migrating meeting_supervisors to meeting_members --

:: Extract meeting_supervisors data from dump to SQL file
%PG_BIN%pg_restore --data-only --table=meeting_supervisors --file=%TEMP%\ms_restore.sql "%DUMP%" 2>nul

if not exist %TEMP%\ms_restore.sql (
    echo   WARNING: Could not extract meeting_supervisors
    goto :skip_ms
)

for %%z in (%TEMP%\ms_restore.sql) do set size=%%~zz
if !size! equ 0 (
    echo   meeting_supervisors data empty
    del %TEMP%\ms_restore.sql 2>nul
    goto :skip_ms
)

:: Rewrite COPY to use a real (non-temp) staging table named _ms_staging
powershell -Command "(gc '%TEMP%\ms_restore.sql') -replace 'public\.meeting_supervisors', '_ms_staging' | Out-File -Encoding ascii '%TEMP%\ms_fixed.sql'"

:: Create the staging table with the OLD column names, load data, migrate, drop
%PG_BIN%psql -d "%DEV_DSN:"=%" -c "
    DROP TABLE IF EXISTS _ms_staging;
    CREATE TABLE _ms_staging (
        id SERIAL,
        body VARCHAR(16) NOT NULL DEFAULT '',
        meeting_id VARCHAR(32) NOT NULL,
        meeting_db_id INTEGER NOT NULL DEFAULT 0,
        supervisor_id INTEGER NOT NULL,
        role VARCHAR(64),
        present BOOLEAN,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    );
" 2>nul

:: Load data into staging table
%PG_BIN%psql -d "%DEV_DSN:"=%" -f %TEMP%\ms_fixed.sql -q 2>nul

:: Migrate to meeting_members with column rename
%PG_BIN%psql -d "%DEV_DSN:"=%" -c "
    INSERT INTO public.meeting_members
        (body, meeting_id, meeting_db_id, member_id, role, present, created_at, updated_at)
    SELECT body, meeting_id, meeting_db_id, supervisor_id, role, present, created_at, updated_at
    FROM _ms_staging
    ON CONFLICT (body, meeting_id, member_id) DO NOTHING;
    DROP TABLE _ms_staging;
" 2>nul

del %TEMP%\ms_restore.sql %TEMP%\ms_fixed.sql 2>nul
echo   meeting_supervisors to meeting_members: done

:skip_ms

:: ── Phase 4: Update sequences ──
echo.
echo -- Phase 4: Updating sequences --
%PG_BIN%psql -d "%DEV_DSN:"=%" -t -A -c "
    SELECT 'SELECT SETVAL(' ||
        quote_literal(quote_ident(s.schemaname) || '.' || quote_ident(s.relname)) ||
        ', COALESCE(MAX(' || quote_ident(c.attname) || '), 1)) FROM ' ||
        quote_ident(s.schemaname) || '.' || quote_ident(t.relname) || ';'
    FROM pg_class s, pg_depend d, pg_class t, pg_attribute c, pg_tables pt
    WHERE s.relkind = 'S'
      AND s.oid = d.objid
      AND d.refobjid = t.oid
      AND d.refobjid = c.attrelid
      AND d.refobjsubid = c.attnum
      AND t.relname = pt.tablename
      AND pt.schemaname = 'public'
" > %TEMP%\seq_fix.sql 2>nul
%PG_BIN%psql -d "%DEV_DSN:"=%" -f %TEMP%\seq_fix.sql -q 2>nul
del %TEMP%\seq_fix.sql 2>nul
echo   Sequences updated

:: ── Phase 5: Verification ──
echo.
echo -- Phase 5: Row counts --
for %%t in (meetings agenda_items agenda_item_votes member_votes supporting_documents meeting_members persons) do (
    for /f "delims=" %%c in ('%PG_BIN%psql -t -A -d "%DEV_DSN:"=%" -c "SELECT COUNT(*) FROM public.%%t"') do (
        echo   %%t: %%c rows
    )
)

echo.
echo ============================================================
echo Restore complete!
echo ============================================================
echo.
echo Next steps:
echo   1. Run '.\scrape buckeye --sync --force --month=2026-07' to
echo      recover Buckeye data synced today but not in the backup
echo   2. Verify upcoming meetings show on the front page
echo.
