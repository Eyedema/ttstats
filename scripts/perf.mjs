#!/usr/bin/env node
// `npm run perf` -- page-speed report for the key pages (KAN-121 / KAN-119).
//
// Boots its own prod-like server (scripts/e2e_server.sh on port 8126, with its
// own SQLite file and STATIC_ROOT so it can run beside the e2e suite), logs in
// once, and loads every page in KEY_PAGES under Chromium's CDP network
// emulation, cold (fresh context, empty HTTP cache) and warm (same context,
// page loaded once before). Reports the median of N runs as a markdown table
// on stdout and in perf-report.md (gitignored).
//
// Chromium, not WebKit: only Chromium exposes network throttling (CDP). The
// byte budgets that run in CI (tests/e2e/budgets.spec.js) use WebKit and
// count bytes only.
//
// Usage:
//   npm run perf                       # 5 runs, boots its own server
//   npm run perf -- --runs 3
//   npm run perf -- --pages dashboard,play
//   PERF_BASE_URL=http://localhost:8126 npm run perf   # reuse a running server
//   npm run perf -- --nav              # tab navigation: tap -> FCP, prefetch on/off
//
// --nav (KAN-124): page A is loaded (warm static cache, as when moving between
// tabs), throttling goes on, then a real touch tap on a tab: touchstart, TAP_MS
// held, touchend (-> click). Reported: tap -> FCP of page B, measured from the
// touchstart, in three modes (NAV_MODES below) on the same build.
import { spawn, execSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium, devices } = require('@playwright/test');
const { resolvePages, bucketFor } = require('../tests/e2e/perf-pages.js');
const { E2E_USERNAME, E2E_PASSWORD } = require('../tests/e2e/helpers.js');

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = Number(process.env.PERF_PORT || 8126);
const BASE_URL = process.env.PERF_BASE_URL || `http://localhost:${PORT}`;

// The profile from the KAN-121 ticket: a poor mobile connection.
// CDP throughput is bytes/second: 400 kbit/s = 50 000 B/s.
const PROFILE = {
  label: '400 kbps down / 400 kbps up / 400 ms RTT',
  conditions: {
    offline: false,
    latency: 400,
    downloadThroughput: (400 * 1000) / 8,
    uploadThroughput: (400 * 1000) / 8,
  },
};

function parseArgs(argv) {
  const args = { runs: 5, pages: null, nav: false, out: path.join(REPO_ROOT, 'perf-report.md') };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--nav') { args.nav = true; continue; }
    if (argv[i] === '--runs') args.runs = Number(argv[++i]);
    else if (argv[i] === '--pages') args.pages = argv[++i].split(',').map((s) => s.trim());
    else if (argv[i] === '--out') args.out = path.resolve(argv[++i]);
  }
  return args;
}

// ---------------------------------------------------------------- server ---

async function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`Server at ${url} did not come up in ${timeoutMs / 1000}s`);
}

function startServer() {
  const child = spawn('bash', ['scripts/e2e_server.sh', String(PORT)], {
    cwd: REPO_ROOT,
    detached: true, // own process group, so the whole tree can be stopped
    stdio: ['ignore', 'ignore', 'pipe'],
    env: {
      ...process.env,
      E2E_DB: path.join(REPO_ROOT, 'ttstats', 'perf.sqlite3'),
      E2E_STATIC_ROOT: path.join(REPO_ROOT, 'ttstats', 'staticfiles_perf'),
    },
  });
  let stderr = '';
  child.stderr.on('data', (d) => { stderr = (stderr + d).slice(-4000); });
  child.on('exit', (code) => {
    if (code && !child.stopping) {
      console.error(`perf server exited with ${code}:\n${stderr}`);
      process.exit(1);
    }
  });
  const stop = () => {
    child.stopping = true;
    try { process.kill(-child.pid, 'SIGTERM'); } catch { /* already gone */ }
  };
  process.on('exit', stop);
  process.on('SIGINT', () => { stop(); process.exit(130); });
  return stop;
}

// ----------------------------------------------------------- measurement ---

// Installed before any page script: records the latest LCP candidate.
function lcpObserver() {
  window.__perfLcp = null;
  try {
    new PerformanceObserver((list) => {
      const entries = list.getEntries();
      const last = entries[entries.length - 1];
      if (last) window.__perfLcp = last.startTime;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch { /* unsupported */ }
}

function contextOptions(storageState) {
  // iPhone 13 viewport and touch, as in the e2e suite, but in Chromium.
  const { defaultBrowserType, ...iphone } = devices['iPhone 13'];
  return {
    ...iphone,
    baseURL: BASE_URL,
    storageState,
    // sw.js has no fetch handler (it only receives push); blocking it keeps
    // its registration request out of the page's numbers.
    serviceWorkers: 'block',
  };
}

async function measure(browser, storageState, pagePath, mode) {
  const context = await browser.newContext(contextOptions(storageState));
  await context.addInitScript(lcpObserver);
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');

  if (mode === 'warm') {
    // Prime the HTTP cache, then leave so the measured load is a fresh
    // navigation (not a reload, which would revalidate everything).
    await page.goto(pagePath, { waitUntil: 'load' });
    await page.waitForLoadState('networkidle');
    await page.goto('about:blank');
  }

  await cdp.send('Network.emulateNetworkConditions', PROFILE.conditions);

  let documentTtfb = null;
  const requests = new Map();
  const req = (id) => {
    if (!requests.has(id)) requests.set(id, { type: 'other', cached: false, bytes: 0, done: false });
    return requests.get(id);
  };
  cdp.on('Network.requestWillBeSent', (e) => {
    if (e.request.url.startsWith('data:')) return;
    req(e.requestId).type = (e.type || 'other').toLowerCase();
  });
  cdp.on('Network.requestServedFromCache', (e) => { req(e.requestId).cached = true; });
  cdp.on('Network.responseReceived', (e) => {
    if (!requests.has(e.requestId)) return;
    const r = req(e.requestId);
    r.type = (e.type || r.type).toLowerCase();
    if (e.response.fromDiskCache || e.response.fromPrefetchCache) r.cached = true;
    // Chromium's emulated latency lands between the first and the last
    // header byte, so the Navigation Timing responseStart does not include
    // it. receiveHeadersEnd does: that is the TTFB a phone would see.
    if (e.type === 'Document' && documentTtfb === null && e.response.timing) {
      documentTtfb = e.response.timing.receiveHeadersEnd;
    }
  });
  cdp.on('Network.loadingFinished', (e) => {
    if (!requests.has(e.requestId)) return;
    const r = req(e.requestId);
    r.bytes = e.encodedDataLength; // compressed body + headers
    r.done = true;
  });

  const response = await page.goto(pagePath, { waitUntil: 'load' });
  if (!response || response.status() !== 200) {
    throw new Error(`${pagePath} answered ${response && response.status()}`);
  }
  await page.waitForLoadState('networkidle');

  const timing = await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0];
    const fcp = performance.getEntriesByName('first-contentful-paint')[0];
    return {
      // Request sent -> first header byte, before the emulated latency is
      // applied: effectively the server's time on the page (plus loopback).
      server: nav.responseStart - nav.requestStart,
      fcp: fcp ? fcp.startTime : null,
      lcp: window.__perfLcp,
      dcl: nav.domContentLoadedEventEnd - nav.startTime,
      load: nav.loadEventEnd - nav.startTime,
      serverTiming: (nav.serverTiming || []).map((s) => ({
        name: s.name, duration: s.duration, description: s.description,
      })),
    };
  });
  await context.close();

  const bytes = { html: 0, js: 0, css: 0, font: 0, img: 0, other: 0, total: 0 };
  let network = 0;
  let cached = 0;
  for (const r of requests.values()) {
    if (r.cached) { cached += 1; continue; }
    network += 1;
    bytes[bucketFor(r.type)] += r.bytes;
    bytes.total += r.bytes;
  }
  return { ...timing, ttfb: documentTtfb, bytes, network, cached };
}

// ------------------------------------------------------------ navigation ---

// Tab bar hops: from -> to. Every tab is a destination once.
const NAV_HOPS = [
  ['/pingpong/', '/pingpong/leaderboard/'],
  ['/pingpong/leaderboard/', '/pingpong/championships/'],
  ['/pingpong/championships/', '/pingpong/play/'],
  ['/pingpong/play/', '/pingpong/'],
];
// A finger on glass: touchstart -> touchend. ~100 ms is a typical quick tap.
const TAP_MS = 100;

// off:  Save-Data on, prefetch.js stands down (the baseline).
// join: Chromium as it is: the navigation joins the in-flight prefetch.
// hold: navigator.userAgentData hidden, so prefetch.js takes the WebKit/Gecko
//       path (click held until the prefetch is cached). Models the iPhone
//       and Firefox gain on the one engine that can throttle.
const NAV_MODES = ['off', 'join', 'hold'];

async function measureNav(browser, storageState, from, to, mode) {
  const context = await browser.newContext(contextOptions(storageState));
  if (mode === 'off') {
    await context.addInitScript(() => {
      Object.defineProperty(navigator, 'connection', { value: { saveData: true }, configurable: true });
    });
  }
  if (mode === 'hold') {
    await context.addInitScript(() => {
      Object.defineProperty(Navigator.prototype, 'userAgentData', { get: () => undefined, configurable: true });
    });
  }
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  // Warm the static cache on B too, so only B's document crosses the wire.
  await page.goto(to, { waitUntil: 'load' });
  await page.goto(from, { waitUntil: 'load' });
  await page.waitForLoadState('networkidle');
  await cdp.send('Network.emulateNetworkConditions', PROFILE.conditions);

  let docRequests = 0;
  const target = new URL(to, BASE_URL).href;
  // Did B's navigation itself go to the network? A navigation answered by
  // the prefetch (from the HTTP cache, or joined in flight) does not.
  cdp.on('Network.responseReceived', (e) => {
    if (e.type !== 'Document' || e.response.url !== target) return;
    if (e.response.fromDiskCache || e.response.fromPrefetchCache || e.response.fromServiceWorker) return;
    docRequests += 1;
  });

  const box = await page.locator(`nav[aria-label="Primary"] a[href="${to}"]`).boundingBox();
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const tapAt = Date.now();
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
  await new Promise((r) => setTimeout(r, TAP_MS));
  await Promise.all([
    page.waitForURL(target, { waitUntil: 'load' }),
    cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }),
  ]);
  // FCP can be reported a frame after load; wait for it (bounded).
  await page.waitForFunction(
    () => performance.getEntriesByName('first-contentful-paint').length > 0,
    null, { timeout: 5000 },
  ).catch(() => {});
  const t = await page.evaluate(() => {
    const fcp = performance.getEntriesByName('first-contentful-paint')[0];
    return { origin: performance.timeOrigin, fcp: fcp ? fcp.startTime : null };
  });
  await context.close();
  return {
    fcp: t.fcp === null ? null : t.origin + t.fcp - tapAt,
    commitWait: t.origin - tapAt,
    requests: docRequests,
  };
}

async function runNav(browser, storageState, runs) {
  const results = {};
  for (let run = 1; run <= runs; run++) {
    for (const [from, to] of NAV_HOPS) {
      for (const mode of NAV_MODES) {
        const r = await measureNav(browser, storageState, from, to, mode);
        ((results[`${from} → ${to}`] ||= { off: [], join: [], hold: [] })[mode]).push(r);
      }
    }
    console.error(`nav run ${run}/${runs} done`);
  }
  const lines = [
    '### Tab navigation: tap → FCP of the next tab (warm static cache)',
    '',
    '| Hop | FCP off ms | FCP join ms (Δ) | FCP hold ms (Δ) | Navigation hit the network off/join/hold |',
    '|---|---:|---:|---:|---:|',
  ];
  for (const [hop, r] of Object.entries(results)) {
    const f = (m) => median(r[m].map((x) => x.fcp));
    const n = (m) => median(r[m].map((x) => x.requests));
    const off = f('off');
    lines.push(`| ${hop} | ${ms(off)} | ${ms(f('join'))} (${ms(f('join') - off)}) | ${ms(f('hold'))} (${ms(f('hold') - off)}) | ${n('off')}/${n('join')}/${n('hold')} |`);
  }
  return lines.join('\n');
}

// ------------------------------------------------------------- reporting ---

const median = (xs) => {
  const v = xs.filter((x) => x !== null && x !== undefined).sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
};
const kb = (b) => (b === null ? '–' : (b / 1024).toFixed(1));
const ms = (t) => (t === null ? '–' : String(Math.round(t)));

function summarise(runs) {
  const pick = (f) => median(runs.map(f));
  const lcps = runs.map((r) => r.lcp).filter((x) => x !== null);
  const spread = lcps.length
    ? ((Math.max(...lcps) - Math.min(...lcps)) / median(lcps)) * 100
    : null;
  const serverTiming = {};
  for (const r of runs) {
    for (const s of r.serverTiming) (serverTiming[s.name] ||= []).push(s.duration);
  }
  return {
    network: pick((r) => r.network),
    cached: pick((r) => r.cached),
    bytes: Object.fromEntries(
      Object.keys(runs[0].bytes).map((k) => [k, pick((r) => r.bytes[k])]),
    ),
    ttfb: pick((r) => r.ttfb),
    server: pick((r) => r.server),
    fcp: pick((r) => r.fcp),
    lcp: pick((r) => r.lcp),
    dcl: pick((r) => r.dcl),
    load: pick((r) => r.load),
    lcpSpread: spread,
    serverTiming: Object.entries(serverTiming)
      .map(([name, ds]) => `${name}=${ms(median(ds))}`)
      .join(' '),
  };
}

function table(mode, rows) {
  const anyServerTiming = rows.some(([, s]) => s.serverTiming);
  const head = ['Page', 'Reqs (net/cache)', 'HTML KB', 'JS KB', 'CSS KB', 'Font KB',
    'Other KB', 'Total KB', 'Server ms', 'TTFB ms', 'FCP ms', 'LCP ms', 'DCL ms', 'Load ms', 'LCP spread'];
  if (anyServerTiming) head.push('Server-Timing (ms)');
  const lines = [
    `### ${mode}`,
    '',
    `| ${head.join(' | ')} |`,
    `|${head.map((_, i) => (i === 0 ? '---' : '---:')).join('|')}|`,
  ];
  for (const [name, s] of rows) {
    const b = s.bytes;
    const cells = [
      name, `${s.network}/${s.cached}`, kb(b.html), kb(b.js), kb(b.css), kb(b.font),
      kb(b.other + b.img), kb(b.total), ms(s.server), ms(s.ttfb), ms(s.fcp), ms(s.lcp), ms(s.dcl),
      ms(s.load), s.lcpSpread === null ? '–' : `${s.lcpSpread.toFixed(1)}%`,
    ];
    if (anyServerTiming) cells.push(s.serverTiming || '–');
    lines.push(`| ${cells.join(' | ')} |`);
  }
  if (!anyServerTiming) {
    lines.push('', '_No `Server-Timing` header on any page._');
  }
  return lines.join('\n');
}

function gitRevision() {
  try {
    const sha = execSync('git rev-parse --short HEAD', { cwd: REPO_ROOT }).toString().trim();
    const dirty = execSync('git status --porcelain', { cwd: REPO_ROOT }).toString().trim();
    return dirty ? `${sha} (+ uncommitted changes)` : sha;
  } catch {
    return 'unknown';
  }
}

// ------------------------------------------------------------------ main ---

async function main() {
  const args = parseArgs(process.argv.slice(2));
  let stopServer = () => {};
  if (!process.env.PERF_BASE_URL) {
    console.error(`Starting prod-like server on :${PORT} ...`);
    stopServer = startServer();
  }
  await waitForServer(`${BASE_URL}/accounts/login/`, 180000);

  const browser = await chromium.launch();
  try {
    // Log in once (the login view allows 5 POSTs / IP / 15 min).
    const loginContext = await browser.newContext({ baseURL: BASE_URL });
    const loginPage = await loginContext.newPage();
    await loginPage.goto('/accounts/login/');
    await loginPage.fill('input[name="username"]', E2E_USERNAME);
    await loginPage.fill('input[name="password"]', E2E_PASSWORD);
    await Promise.all([
      loginPage.waitForURL(/\/pingpong\//),
      loginPage.click('button[type="submit"]'),
    ]);
    const storageState = await loginContext.storageState();

    if (args.nav) {
      await loginContext.close();
      const report = [
        `## Tab navigation — median of ${args.runs} runs`,
        '',
        `- Revision: \`${gitRevision()}\`, ${new Date().toISOString().slice(0, 10)}`,
        `- Chromium ${browser.version()}, iPhone 13 viewport, CDP network emulation: ${PROFILE.label}`,
        `- Tap = touchstart, ${TAP_MS} ms, touchend. FCP is measured from the touchstart. Off = Save-Data (prefetch.js stands down); join = Chromium as is; hold = userAgentData hidden, so prefetch.js holds the click as on WebKit/Gecko.`,
        '- "Navigation hit the network": 1 = the click\'s navigation fetched B itself; 0 = it was answered by the prefetch.',
        '',
        await runNav(browser, storageState, args.runs),
        '',
      ].join('\n');
      console.log(report);
      writeFileSync(args.out, report);
      return;
    }

    let pages = await resolvePages(loginPage);
    if (args.pages) pages = pages.filter((p) => args.pages.includes(p.name));
    // One unrecorded, unthrottled load each, so the first measured run does
    // not pay for the server's cold LocMemCache.
    for (const p of pages) await loginPage.goto(p.path);
    await loginContext.close();

    const results = { cold: {}, warm: {} };
    // Interleave pages and modes within each run, so slow drift on the
    // machine spreads across every page instead of landing on one.
    for (let run = 1; run <= args.runs; run++) {
      for (const p of pages) {
        for (const mode of ['cold', 'warm']) {
          const r = await measure(browser, storageState, p.path, mode);
          (results[mode][p.name] ||= []).push(r);
        }
      }
      console.error(`run ${run}/${args.runs} done`);
    }

    const browserVersion = browser.version();
    const report = [
      `## Page speed — median of ${args.runs} runs`,
      '',
      `- Revision: \`${gitRevision()}\`, ${new Date().toISOString().slice(0, 10)}`,
      `- Chromium ${browserVersion}, iPhone 13 viewport, CDP network emulation: ${PROFILE.label}`,
      '- Server: `scripts/e2e_server.sh` (dev settings + prod static pipeline: hashed, Brotli + gzip, WhiteNoise), `runserver` over HTTP/1.1, seeded SQLite, server cache warmed',
      '- Bytes are transferred (compressed body + headers); KB = 1024 B. Cold = empty HTTP cache; warm = same page loaded once before.',
      '- Server ms = request sent → first header byte, without the emulated latency (the server\'s own time). TTFB includes the emulated RTT.',
      '- LCP spread = (max − min) / median across the runs.',
      '',
      table('Cold cache', pages.map((p) => [p.name, summarise(results.cold[p.name])])),
      '',
      table('Warm cache', pages.map((p) => [p.name, summarise(results.warm[p.name])])),
      '',
    ].join('\n');

    console.log(report);
    writeFileSync(args.out, report);
    console.error(`Wrote ${path.relative(REPO_ROOT, args.out)}`);
  } finally {
    await browser.close();
    stopServer();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
