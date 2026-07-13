#!/usr/bin/env node
/**
 * HTTP helper for Tempe Council Subcommittees scraper.
 * Accessible via subprocess from the Python scraper.
 * 
 * Usage:
 *   node tempe_subcommittees_helper.mjs fetch <page-path>
 *   node tempe_subcommittees_helper.mjs download <url> [output-path]
 * 
 * The helper uses node's native fetch() which bypasses Akamai WAF
 * where Python requests / curl / Playwright are blocked.
 *
 * Output is JSON on stdout, errors on stderr.
 */

const BASE = "https://www.tempe.gov";

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

/**
 * Fetch a page and parse documents from its folder listing.
 */
async function cmdFetch(path) {
  const url = path.startsWith("http") ? path : BASE + path;
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

  // Extract all document entries from the HTML
  const documents = [];
  const docRe =
    /<li>(?:<img[^>]*\/>\s*)?<a[^>]*class='content_link'[^>]*aria-label='([^']*)'[^>]*href="([^"]+)"[^>]*>([^<]+)<\/a>/gs;
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

  // Extract all folder IDs from the page
  const allFolderIds = [...new Set(
    [...html.matchAll(/-folder-(\d+)/g)].map(m => m[1])
  )];

  console.log(JSON.stringify({
    url: response.url,
    status: response.status,
    byteLength: html.length,
    documents,
    breadcrumbs,
    allFolderIds,
  }));
}

/**
 * Download a file to disk (or to stdout).
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
    console.error("Usage: node helper.mjs <fetch|download> <path|url> [output-path]");
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
