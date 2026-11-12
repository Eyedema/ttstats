// Runs the real service worker (a Django template) in a vm, with fakes for
// the parts of the worker global a unit test needs to drive: Cache Storage,
// fetch, timers, clients and the clock.
//
// The template expressions are replaced with inert values. Top-level
// `function` declarations become properties of the returned context, so the
// worker's pure helpers can be called directly.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

export const ORIGIN = 'https://ttstats.test';
export const SOURCE = readFileSync(
  new URL('../../ttstats/pingpong/templates/pingpong/sw.js', import.meta.url),
  'utf8'
)
  .replace('{% load static %}', '')
  .replace('{{ sw_build }}', 'test')
  .replace('{{ sw_precache_json|safe }}', '[]')
  .replace(/\{% static "([^"]+)" %\}/, '/static/$1');

const keyOf = (request) =>
  typeof request === 'string' ? new URL(request, ORIGIN).href : request.url;

/** An in-memory Cache: insertion-ordered, like the real keys(). */
export class FakeCache {
  constructor() {
    this.entries = new Map();
  }
  async match(request) {
    const hit = this.entries.get(keyOf(request));
    return hit ? hit.clone() : undefined;
  }
  async put(request, response) {
    // Like the real one: the body is read now.
    const body = await response.arrayBuffer();
    this.entries.delete(keyOf(request));
    this.entries.set(
      keyOf(request),
      new Response(body, { status: response.status, headers: response.headers })
    );
  }
  async delete(request) {
    return this.entries.delete(keyOf(request));
  }
  async keys() {
    return [...this.entries.keys()].map((url) => ({ url }));
  }
}

export class FakeCacheStorage {
  constructor() {
    this.caches = new Map();
  }
  async open(name) {
    if (!this.caches.has(name)) this.caches.set(name, new FakeCache());
    return this.caches.get(name);
  }
  async has(name) {
    return this.caches.has(name);
  }
  async delete(name) {
    return this.caches.delete(name);
  }
  async keys() {
    return [...this.caches.keys()];
  }
}

/**
 * Load the worker. Options:
 *   fetch(request)  what the network does (default: never answers)
 *   timers          'never' (default) or 'manual' (run with fireTimers())
 *   onLine          navigator.onLine inside the worker (default true)
 *   storage         a FakeCacheStorage to share (a restarted worker)
 */
export function loadWorker({ fetch, timers = 'never', onLine = true, storage: shared } = {}) {
  const listeners = {};
  const pendingTimers = [];
  const messages = [];
  const clock = { now: Date.UTC(2026, 10, 4, 13, 32) };
  class FakeDate extends Date {
    constructor(...args) {
      if (args.length) super(...args);
      else super(clock.now);
    }
    static now() {
      return clock.now;
    }
  }
  // Pass another worker's `storage` to model a restart over the same caches.
  const storage = shared || new FakeCacheStorage();
  const self = {
    location: new URL(ORIGIN + '/sw.js'),
    navigator: { onLine },
    addEventListener: (type, fn) => {
      listeners[type] = fn;
    },
    skipWaiting: () => Promise.resolve(),
    clients: {
      claim: () => Promise.resolve(),
      matchAll: () => Promise.resolve([]),
      get: async (id) => ({ id, postMessage: (message) => messages.push({ id, message }) }),
    },
    registration: { showNotification: () => Promise.resolve() },
  };
  const context = {
    self,
    URL,
    Headers,
    Response,
    Promise,
    Date: FakeDate,
    caches: storage,
    fetch: fetch || (() => new Promise(() => {})),
    setTimeout: (fn, ms, ...args) => {
      if (timers === 'manual') pendingTimers.push(() => fn(...args));
      return 0;
    },
    console,
  };
  vm.runInNewContext(SOURCE, context);
  return {
    context,
    listeners,
    storage,
    clock,
    messages,
    fireTimers() {
      while (pendingTimers.length) pendingTimers.shift()();
    },
  };
}

/** A fetch event; `settled()` waits for respondWith and every waitUntil. */
export function fetchEvent(path, { method = 'GET', mode = 'navigate', headers = {} } = {}) {
  const lifetime = [];
  const event = {
    request: { url: ORIGIN + path, method, mode, headers: new Headers(headers) },
    resultingClientId: 'client-1',
    clientId: '',
    responded: false,
    response: null,
    respondWith(promise) {
      this.responded = true;
      this.response = Promise.resolve(promise);
      lifetime.push(this.response.catch(() => {}));
    },
    waitUntil(promise) {
      lifetime.push(Promise.resolve(promise).catch(() => {}));
    },
    async settled() {
      // waitUntil can be called from inside an earlier waitUntil promise.
      let seen = -1;
      while (seen !== lifetime.length) {
        seen = lifetime.length;
        await Promise.all(lifetime);
        await new Promise((resolve) => setImmediate(resolve));
      }
    },
  };
  return event;
}

/** A message event, with the same `settled()`. */
export function messageEvent(data) {
  const lifetime = [];
  return {
    data,
    waitUntil(promise) {
      lifetime.push(Promise.resolve(promise));
    },
    async settled() {
      await Promise.all(lifetime);
    },
  };
}
