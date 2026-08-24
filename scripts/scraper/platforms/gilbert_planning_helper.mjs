#!/usr/bin/env node
/**
 * HTTP helper for Gilbert Planning Commission scraper.
 * Uses Node.js native fetch() to bypass Akamai WAF (same approach
 * as tempe_subcommittees_helper.mjs).
 *
 * Usage:
 *   node gilbert_planning_helper.mjs fetch <url> [--page N]
 *   node gilbert_planning_helper.mjs download <pdf-url> [output-path]
 *
 * For the folder page, use the full gilbertaz.gov URL including
 * optional pagination suffix like /-npage-2.
 */

const BASE = "https://www.gilbertaz.gov";

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

/**
 * Fetch a folder page and extract all document entries and pagination info.
 */
async function cmdFetch(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  const response = await fetch(url, {
    headers: { "User-Agent": UA, Accept: "text/html" },
    redirect: "follow",
    signal: controller.signal,
  });
  clearTimeout(timer);

  if (!response.ok) {
    throw new Error(`HTTP ${response.status} from ${url}`);
  }

  const html = await response.text();

  // Extract document entries — CivicPlus Document Folder widget
  // <a  class='content_link' aria-label='...' href="/home/showpublisheddocument/...">Title</a>
  const documents = [];
  const docRe =
    /<a\s+class='content_link'[^>]*aria-label='([^']*)'[^>]*href="(\/home\/showpublisheddocument\/\d+\/\d+)"[^>]*>([^<]+)<\/a>/gs;
  let m;
  while ((m = docRe.exec(html)) !== null) {
    documents.push({
      description: m[1],
      url: m[2].startsWith("http") ? m[2] : BASE + m[2],
      title: m[3].trim(),
    });
  }

  // Extract breadcrumb folder info
  const breadcrumbs = [];
  const bcRe =
    /<div class='document_breadcrumb'>([\s\S]*?)<\/div>/g;
  let bcMatch;
  while ((bcMatch = bcRe.exec(html)) !== null) {
    const links = [...bcMatch[1].matchAll(
      /href="[^"]*-folder-(\d+)"[^>]*>([^<]+)</g
    )];
    breadcrumbs.push(
      links.map((l) => ({ id: l[1], label: l[2].trim() }))
    );
  }

  // Extract pagination info
  const pagination = {
    totalItems: null,
    totalPages: null,
    currentPage: 1,
    nextUrl: null,
    lastUrl: null,
  };

  // Total items count
  const totalMatch = html.match(/of\s+(\d+)\s+items/i);
  if (totalMatch) pagination.totalItems = parseInt(totalMatch[1], 10);

  // Current page from the active pagination link
  const curMatch = html.match(
    /<span[^>]*>\s*<strong>(\d+)<\/strong>\s*<\/span>/i
  );
  if (curMatch) pagination.currentPage = parseInt(curMatch[1], 10);

  // Next page link
  const nextMatch = html.match(
    /<a[^>]*href="([^"]*-npage-(\d+))"[^>]*>Next\s*&raquo;<\/a>/i
  );
  if (nextMatch) {
    pagination.nextUrl = nextMatch[1].startsWith("http")
      ? nextMatch[1]
      : BASE + nextMatch[1];
  }

  // Last page link
  const lastMatch = html.match(
    /<a[^>]*href="([^"]*-npage-(\d+))"[^>]*>Last\s*&raquo;<\/a>/i
  );
  if (lastMatch) {
    pagination.totalPages = parseInt(lastMatch[2], 10);
    pagination.lastUrl = lastMatch[1].startsWith("http")
      ? lastMatch[1]
      : BASE + lastMatch[1];
  }

  // Fallback: count page links to determine total pages
  if (!pagination.totalPages) {
    const pageNums = [...html.matchAll(
      /href="[^"]*-npage-(\d+)"[^>]*>(\d+)<\/a>/g
    )].map((mm) => parseInt(mm[1], 10));
    if (pageNums.length > 0) {
      pagination.totalPages = Math.max(...pageNums);
    }
  }

  console.log(JSON.stringify({
    url: response.url,
    status: response.status,
    byteLength: html.length,
    documents,
    breadcrumbs,
    pagination,
  }));
}

/**
 * Download a PDF to stdout (for piping) or to disk.
 */
async function cmdDownload(url, outputPath) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 55000);
  const response = await fetch(url, {
    headers: { "User-Agent": UA },
    redirect: "follow",
    signal: controller.signal,
  });
  clearTimeout(timer);
  if (!response.ok) {
    throw new Error(`Download HTTP ${response.status} from ${url}`);
  }
  const buf = Buffer.from(await response.arrayBuffer());
  if (outputPath) {
    const fs = await import("fs");
    fs.writeFileSync(outputPath, buf);
    console.log(JSON.stringify({ downloaded: true, path: outputPath, bytes: buf.length }));
  } else {
    process.stdout.write(buf);
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length < 2) {
    console.error("Usage: node helper.mjs <fetch|download> <url> [output-path]");
    process.exit(1);
  }

  const command = args[0];
  const target = args[1];

  try {
    if (command === "fetch") {
      await cmdFetch(target);
    } else if (command === "download") {
      await cmdDownload(target, args[2]);
    } else {
      throw new Error(`Unknown command: ${command}`);
    }
  } catch (err) {
    console.error(err.message);
    process.exit(1);
  }
}

main();
