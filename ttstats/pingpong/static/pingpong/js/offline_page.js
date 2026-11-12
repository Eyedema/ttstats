/* TTStats offline page: list what is saved on this phone (KAN-137 / S12.6).
 *
 * sw.js shows templates/pingpong/offline.html in place of a /pingpong/ page
 * that got no answer and has no saved copy. That HTML is the same for
 * everyone, so the list of saved pages is built here, on the phone: this
 * script asks the worker {type: 'list-snapshots'} over a MessageChannel and
 * gets back
 *
 *   {type: 'snapshots',
 *    snapshots:   [{url, title, savedAt, shown}]   the CURRENT partition only,
 *    scoreboards: [{url, title, savedAt, shown}]}  newest first; `shown` is the
 *                                                  time as the banner words it
 *
 * Scoreboards come first ("Live scoreboards you can keep scoring"), then the
 * snapshots grouped Today / Table / Matches / Players / Other by URL.
 *
 * Fails closed: with no JS, no worker or no answer the page keeps its static
 * text and its links to Today and the Table. "Try again" is a link to this
 * same URL; here it becomes location.reload().
 *
 * Plain JS, no inline code, no eval (production CSP). UMD: in Node
 * (tests/js/offline_page.test.mjs) it exports the pure grouping and touches
 * no DOM.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.TTStatsOfflinePage = api;
    api.boot(root);
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LIST_TIMEOUT_MS = 3000;
  var GROUPS = [
    { key: 'live', label: 'Live scoreboards you can keep scoring' },
    { key: 'today', label: 'Today' },
    { key: 'table', label: 'Table' },
    { key: 'matches', label: 'Matches' },
    { key: 'players', label: 'Players' },
    { key: 'other', label: 'Other' },
  ];
  var TITLE_SUFFIX = / - (TTStats|Table Tennis Tracker)$/;

  /* Which group a saved page belongs to, by its path. */
  function groupFor(pathname) {
    if (pathname === '/pingpong/') return 'today';
    if (/^\/pingpong\/(leaderboard|seasons)\//.test(pathname)) return 'table';
    if (/^\/pingpong\/matches\//.test(pathname)) return 'matches';
    if (/^\/pingpong\/(players|head-to-head)\//.test(pathname)) return 'players';
    return 'other';
  }

  function cleanTitle(title, path) {
    var text = String(title || '').replace(TITLE_SUFFIX, '').trim();
    return text || path;
  }

  /* Same-origin /pingpong/ URL -> path + query, else null. A worker reply is
   * trusted data, but the links it becomes must never leave the app. */
  function localPath(url, origin) {
    var parsed;
    try {
      parsed = new URL(String(url), origin);
    } catch (err) {
      return null;
    }
    if (parsed.origin !== origin || parsed.pathname.indexOf('/pingpong/') !== 0) return null;
    return parsed.pathname + parsed.search;
  }

  function item(entry, origin) {
    var path = entry && localPath(entry.url, origin);
    if (!path) return null;
    return {
      path: path,
      pathname: path.split('?')[0],
      title: cleanTitle(entry.title, path),
      shown: String((entry && entry.shown) || ''),
    };
  }

  /* The worker's reply -> [{key, label, items: [{path, title, shown}]}],
   * in display order, empty groups left out, each URL listed once. */
  function buildGroups(reply, origin) {
    var buckets = {};
    var seen = {};
    function add(key, entry) {
      if (!entry || seen[entry.path]) return;
      seen[entry.path] = true;
      (buckets[key] = buckets[key] || []).push(entry);
    }
    var data = reply || {};
    (Array.isArray(data.scoreboards) ? data.scoreboards : []).forEach(function (raw) {
      add('live', item(raw, origin));
    });
    (Array.isArray(data.snapshots) ? data.snapshots : []).forEach(function (raw) {
      var entry = item(raw, origin);
      if (entry) add(groupFor(entry.pathname), entry);
    });
    return GROUPS.filter(function (group) {
      return buckets[group.key];
    }).map(function (group) {
      return { key: group.key, label: group.label, items: buckets[group.key] };
    });
  }

  /* Ask the controlling worker; null if there is none or it doesn't answer. */
  function ask(navigatorObj) {
    var sw = navigatorObj && navigatorObj.serviceWorker;
    if (!sw || !sw.controller || typeof MessageChannel === 'undefined') return Promise.resolve(null);
    return new Promise(function (resolve) {
      var channel = new MessageChannel();
      var timer = setTimeout(function () {
        resolve(null);
      }, LIST_TIMEOUT_MS);
      channel.port1.onmessage = function (event) {
        clearTimeout(timer);
        resolve(event.data && event.data.type === 'snapshots' ? event.data : null);
      };
      sw.controller.postMessage({ type: 'list-snapshots' }, [channel.port2]);
    });
  }

  function render(doc, groups) {
    var saved = doc.querySelector('[data-offline-saved]');
    var groupTemplate = doc.querySelector('[data-offline-group-template]');
    var itemTemplate = doc.querySelector('[data-offline-item-template]');
    if (!saved || !groupTemplate || !itemTemplate) return;
    if (!groups.length) {
      var empty = doc.querySelector('[data-offline-empty]');
      if (empty) empty.hidden = false;
      return;
    }
    groups.forEach(function (group) {
      var section = groupTemplate.content.firstElementChild.cloneNode(true);
      section.setAttribute('data-offline-group', group.key);
      section.querySelector('[data-group-label]').textContent = group.label;
      var list = section.querySelector('[data-group-items]');
      group.items.forEach(function (entry) {
        var row = itemTemplate.content.firstElementChild.cloneNode(true);
        var link = row.querySelector('[data-item-link]');
        link.setAttribute('href', entry.path);
        row.querySelector('[data-item-title]').textContent = entry.title;
        row.querySelector('[data-item-time]').textContent = entry.shown ? 'Saved ' + entry.shown : '';
        list.appendChild(row);
      });
      saved.appendChild(section);
    });
    saved.hidden = false;
    // The real list replaces the two guesses.
    var fallback = doc.querySelector('[data-offline-static]');
    if (fallback) fallback.hidden = true;
  }

  function boot(win) {
    var doc = win.document;
    if (!doc || !doc.querySelector('[data-offline-page]')) return;
    var retry = doc.querySelector('[data-offline-retry]');
    if (retry) {
      retry.addEventListener('click', function (event) {
        event.preventDefault();
        win.location.reload();
      });
    }
    ask(win.navigator).then(function (reply) {
      if (reply) render(doc, buildGroups(reply, win.location.origin));
    });
  }

  return {
    GROUPS: GROUPS,
    groupFor: groupFor,
    cleanTitle: cleanTitle,
    buildGroups: buildGroups,
    boot: boot,
  };
});
