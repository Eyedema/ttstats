// Shared fixtures for the sw_*.test.mjs files that drive the snapshot route
// and its messages (not a test file itself: no .test. in its name).
import { ORIGIN, fetchEvent, loadWorker, messageEvent } from './sw_harness.mjs';

export const ME = '0123456789abcdef';
export const OTHER = 'fedcba9876543210';
export const SNAP = (identity) => `ttstats-snap-ttstats-v5-test-${identity}`;
export const DAY = 24 * 60 * 60 * 1000;

export const PAGE = (text) =>
  '<!DOCTYPE html><html lang="en" data-freshness="network" data-rendered-at="x">' +
  `<head><title>Today &amp; more · TTStats</title></head><body>` +
  '<div class="snapshot-banner"><span class="snapshot-recent">No connection — saved copy from </span>' +
  `<time data-snapshot-time></time></div><main>${text}</main></body></html>`;

export function answer(body, { status = 200, snapshot = true, identity = ME, headers = {} } = {}) {
  const all = { 'Content-Type': 'text/html; charset=utf-8', ...headers };
  if (snapshot) all['X-TTStats-Snapshot'] = '1';
  if (identity) all['X-TTStats-Identity'] = identity;
  return new Response(body, { status, headers: all });
}

/** A network the test answers by hand: `net.next` resolves/rejects the
 *  current request; until then it hangs. */
export function controllableNetwork({ offlinePage = null } = {}) {
  const net = { calls: 0, offlineFetches: 0 };
  net.fetch = (request, init) => {
    // The worker's own fetch of the offline page (install, and every
    // identity message until it has it) answers at once: 404 unless the
    // test gives it a page (sw_offline_page.test.mjs does).
    if (typeof request === 'string' && request === '/offline/') {
      net.offlineFetches += 1;
      net.offlineInit = init;
      return Promise.resolve(
        offlinePage
          ? new Response(offlinePage, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } })
          : new Response('nope', { status: 404 })
      );
    }
    net.calls += 1;
    return new Promise((resolve, reject) => {
      net.resolve = resolve;
      net.reject = reject;
    });
  };
  return net;
}

export async function announce(worker, data) {
  const event = messageEvent(data);
  worker.listeners.message(event);
  await event.settled();
}

export async function navigate(worker, path, options) {
  const event = fetchEvent(path, options);
  worker.listeners.fetch(event);
  return event;
}

/** Visit `path` online once so a copy of `text` is stored. */
export async function visitOnline(worker, net, path, text, options = {}) {
  const event = await navigate(worker, path);
  net.resolve(answer(PAGE(text), options));
  const response = await event.response;
  await event.settled();
  return response;
}

export async function stored(worker, identity, path) {
  const cache = worker.storage.caches.get(SNAP(identity));
  if (!cache) return null;
  const hit = await cache.match(ORIGIN + path);
  return hit ? hit.text() : null;
}

export async function setup({ offlinePage, ...options } = {}) {
  const net = controllableNetwork({ offlinePage });
  const worker = loadWorker({ fetch: net.fetch, timers: 'manual', ...options });
  await announce(worker, { type: 'identity', identity: ME, snapshots: 'on' });
  return { net, worker };
}
