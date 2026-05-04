#!/usr/bin/env node
import { chromium } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';

const SOURCE_PAGE = 'https://www.maricopa.gov/324/Board-of-Supervisors-Meeting-Information';
const SEARCH_URL = 'https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/Search?dropid=11&dropsv=01%2F01%2F2025%2000%3A00%3A00&dropev=01%2F31%2F2025%2023%3A59%3A59';
const START_DATE = '2025-01-01';
const END_DATE = '2025-01-31';
const REQUIRED_BODY = /Board of Supervisors/i;
const REQUIRED_TYPES = /(Formal|Informal)/i;

const ROOT = process.cwd();
const AGENDA_DIR = path.join(ROOT, 'data', 'agendas', '2025', '01');
const SUPPORT_DIR = path.join(ROOT, 'data', 'supporting-materials', '2025', '01');
const METADATA_CSV = path.join(AGENDA_DIR, 'metadata.csv');

function parseArgs(argv) {
  const args = new Set(argv.slice(2));
  return {
    download: args.has('--download'),
    headless: !args.has('--headed'),
    limit: numberArg(argv, '--limit') ?? Infinity,
  };
}

function numberArg(argv, flag) {
  const i = argv.indexOf(flag);
  if (i === -1) return null;
  const raw = argv[i + 1];
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function slugify(value) {
  return String(value || '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-zA-Z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-')
    .toLowerCase() || 'meeting';
}

function normalizeMeetingDate(raw) {
  const m = String(raw || '').match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) return '';
  const mm = String(Number(m[1])).padStart(2, '0');
  const dd = String(Number(m[2])).padStart(2, '0');
  return `${m[3]}-${mm}-${dd}`;
}

function csvEscape(value) {
  const s = value == null ? '' : String(value);
  if (/[",\n\r]/.test(s)) return '"' + s.replaceAll('"', '""') + '"';
  return s;
}

function csvLine(fields) {
  return fields.map(csvEscape).join(',') + '\n';
}

function parseCsv(text) {
  const lines = String(text || '').trim().split(/\r?\n/);
  if (!lines.length || !lines[0]) return [];
  const header = splitCsvLine(lines[0]);
  return lines.slice(1).filter(Boolean).map(line => {
    const values = splitCsvLine(line);
    const row = {};
    header.forEach((key, idx) => { row[key] = values[idx] ?? ''; });
    return row;
  });
}

function splitCsvLine(line) {
  const out = [];
  let cur = '';
  let i = 0;
  let quoted = false;
  while (i < line.length) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') {
        cur += '"';
        i += 2;
        continue;
      }
      if (ch === '"') {
        quoted = false;
        i++;
        continue;
      }
      cur += ch;
      i++;
      continue;
    }
    if (ch === ',') {
      out.push(cur);
      cur = '';
      i++;
      continue;
    }
    if (ch === '"') {
      quoted = true;
      i++;
      continue;
    }
    cur += ch;
    i++;
  }
  out.push(cur);
  return out;
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function chooseExtension(url, contentType, fallback = '.bin') {
  if ((contentType || '').includes('pdf')) return '.pdf';
  if ((contentType || '').includes('html')) return '.html';
  try {
    const ext = path.extname(new URL(url).pathname);
    if (ext && ext.length <= 8) return ext;
  } catch {}
  return fallback;
}

async function downloadUrl(url, destination) {
  const response = await fetch(url, {
    headers: {
      'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
      'accept': '*/*',
    },
  });
  if (!response.ok) {
    throw new Error(`Download failed ${response.status} ${response.statusText}: ${url}`);
  }
  const arrayBuffer = await response.arrayBuffer();
  await fs.writeFile(destination, Buffer.from(arrayBuffer));
  return {
    contentType: response.headers.get('content-type') || '',
  };
}

function normalizeUrl(href, base) {
  try {
    return new URL(href, base).toString();
  } catch {
    return href;
  }
}

async function loadExistingRows() {
  if (!(await exists(METADATA_CSV))) return new Map();
  const text = await fs.readFile(METADATA_CSV, 'utf8');
  const rows = parseCsv(text);
  return new Map(rows.map(row => [row.agenda_url, row]));
}

async function writeMetadataHeaderIfNeeded() {
  if (await exists(METADATA_CSV)) return;
  await ensureDir(path.dirname(METADATA_CSV));
  await fs.writeFile(
    METADATA_CSV,
    csvLine(['meeting_date', 'meeting_type', 'agenda_url', 'supporting_materials_url', 'local_file_paths', 'downloaded_at'])
  );
}

async function appendMetadataRow(row) {
  await writeMetadataHeaderIfNeeded();
  await fs.appendFile(METADATA_CSV, csvLine([
    row.meeting_date,
    row.meeting_type,
    row.agenda_url,
    row.supporting_materials_url,
    row.local_file_paths,
    row.downloaded_at,
  ]));
}

async function extractMeetings(page) {
  await page.goto(SEARCH_URL, { waitUntil: 'networkidle' });

  const meetings = await page.evaluate(() => {
    function text(el) { return (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim(); }
    function absUrl(href) { try { return new URL(href, location.href).toString(); } catch { return href; } }
    function rowDate(rowText) {
      const m = rowText.match(/\b(\d{1,2}\/\d{1,2}\/\d{4})\b/);
      return m ? m[1] : '';
    }
    function rowTime(rowText) {
      const m = rowText.match(/\b(\d{1,2}:\d{2}\s?[AP]M)\b/i);
      return m ? m[1].replace(/\s+/g, ' ').toUpperCase() : '';
    }

    const rows = Array.from(document.querySelectorAll('tr'));
    const found = [];
    for (const row of rows) {
      const rowText = text(row);
      if (!rowText || !/\d{1,2}\/\d{1,2}\/\d{4}/.test(rowText)) continue;
      const detail = row.querySelector('a[href*="MeetingDetail.aspx"]');
      const agenda = row.querySelector('a[href*="View.ashx?M=A"]:not([href*="AADA"])');
      if (!detail || !agenda) continue;

      const anchors = Array.from(row.querySelectorAll('a[href]')).map(a => ({
        text: text(a),
        href: absUrl(a.getAttribute('href') || ''),
      }));
      const bodyText = rowText;
      const date = rowDate(rowText);
      const time = rowTime(rowText);
      const bodyAnchor = Array.from(row.querySelectorAll('a[href]')).find(a => {
        const href = a.getAttribute('href') || '';
        return !/MeetingDetail\.aspx/i.test(href) && !/View\.ashx/i.test(href);
      });
      const meetingType = text(bodyAnchor) || text(row.querySelector('strong, b')) || bodyText.split(' ').slice(0, 12).join(' ');

      found.push({
        meeting_date: date,
        meeting_time: time,
        meeting_type: meetingType,
        row_text: bodyText,
        detail_url: absUrl(detail.getAttribute('href') || ''),
        agenda_url: absUrl(agenda.getAttribute('href') || ''),
        anchors,
      });
    }
    return found;
  });

  return meetings
    .map(meeting => ({
      ...meeting,
      meeting_date: normalizeMeetingDate(meeting.meeting_date),
    }))
    .filter(meeting => {
      const body = `${meeting.meeting_type} ${meeting.row_text}`;
      return REQUIRED_BODY.test(body) && REQUIRED_TYPES.test(body) && meeting.meeting_date.startsWith('2025-01-');
    });
}

async function scrapeSupportingUrls(page, url) {
  const ctx = page.context();
  const tmp = await ctx.newPage();
  try {
    await tmp.goto(url, { waitUntil: 'networkidle' });
    const urls = await tmp.evaluate(() => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim();
      const anchors = Array.from(document.querySelectorAll('a[href]')).map(a => ({
        text: clean(a.textContent),
        href: new URL(a.getAttribute('href'), location.href).toString(),
      }));
      return anchors
        .filter(a => /View\.ashx\?M=(?!A(?:ADA)?)/i.test(a.href) || /support/i.test(a.text))
        .map(a => a.href);
    });
    return Array.from(new Set(urls));
  } finally {
    await tmp.close();
  }
}

async function main() {
  const { download, headless, limit } = parseArgs(process.argv);
  const existing = await loadExistingRows();
  await ensureDir(AGENDA_DIR);
  await ensureDir(SUPPORT_DIR);

  const browser = await chromium.launch({ headless });
  const page = await browser.newPage();

  try {
    await page.goto(SOURCE_PAGE, { waitUntil: 'networkidle' });
    const searchLink = await page.locator('a').filter({ hasText: /Meetings/i }).first();
    let searchHref = SEARCH_URL;
    if (await searchLink.count()) {
      const href = await searchLink.getAttribute('href');
      if (href) searchHref = normalizeUrl(href, SOURCE_PAGE);
    }

    await page.goto(searchHref, { waitUntil: 'networkidle' });
    const meetings = await extractMeetings(page);

    let processed = 0;
    for (const meeting of meetings) {
      if (processed >= limit) break;
      processed++;

      const existingRow = existing.get(meeting.agenda_url);
      if (existingRow) {
        const localPaths = (existingRow.local_file_paths || '').split(';').filter(Boolean);
        const allPresent = localPaths.length > 0 && (await Promise.all(localPaths.map(p => exists(path.join(ROOT, p))))).every(Boolean);
        if (allPresent) continue;
      }

      const meetingDate = meeting.meeting_date || START_DATE;
      const dateFolder = meetingDate.replace(/\//g, '-');
      const timePart = meeting.meeting_time ? `_${slugify(meeting.meeting_time)}` : '';
      const prefix = `${dateFolder}${timePart}_${slugify(meeting.meeting_type)}`;

      const agendaPath = path.join(AGENDA_DIR, `${prefix}_agenda.pdf`);
      const localPaths = [];

      if (download) {
        if (!(await exists(agendaPath))) {
          const result = await downloadUrl(meeting.agenda_url, agendaPath);
          if (!result.contentType.includes('pdf') && !agendaPath.endsWith('.pdf')) {
            // kept intentionally minimal; filename already ends in .pdf
          }
        }
      }
      localPaths.push(path.relative(ROOT, agendaPath));

      const supportingUrls = await scrapeSupportingUrls(page, meeting.detail_url);
      const supportingPaths = [];
      for (let i = 0; i < supportingUrls.length; i++) {
        const supportUrl = supportingUrls[i];
        const ext = chooseExtension(supportUrl, '', '.bin');
        const supportPath = path.join(SUPPORT_DIR, `${prefix}_supporting_${String(i + 1).padStart(2, '0')}${ext}`);
        if (download && !(await exists(supportPath))) {
          await downloadUrl(supportUrl, supportPath);
        }
        supportingPaths.push(path.relative(ROOT, supportPath));
      }

      const row = {
        meeting_date: meetingDate,
        meeting_type: meeting.meeting_type,
        agenda_url: meeting.agenda_url,
        supporting_materials_url: supportingUrls.join(';'),
        local_file_paths: [...localPaths, ...supportingPaths].join(';'),
        downloaded_at: new Date().toISOString(),
      };

      if (download && !existing.has(meeting.agenda_url)) {
        await appendMetadataRow(row);
        existing.set(meeting.agenda_url, row);
      }
    }
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
