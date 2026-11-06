// Pull to refresh for the installed app (KAN-126).
//
// Installed to the home screen, iOS gives the app no reload button and no
// native pull-to-refresh, so after a flaky moment the only way to refresh was
// to kill the app. Plain JS, not Alpine: it must work under a CSP without
// 'unsafe-eval' (see .claude/rules/frontend.md).
//
// Standalone only (display-mode standalone/fullscreen, or iOS's
// navigator.standalone): a browser tab keeps the browser's own gesture. Where
// this is live, html.ptr-on sets overscroll-behavior-y: contain so a native
// pull-to-refresh (Chrome Android, Firefox Android if enabled) cannot fire
// alongside it.
//
// Off for the whole page under [data-no-pull-refresh] (the live scoreboard).
// Per gesture it also stands down while an overlay is open, while a POST form
// has unsaved input, or when the touch starts inside something scrolled away
// from its top.
//
// Fails closed: the indicator only exists once this script has run.
(function () {
  'use strict';

  var standalone = navigator.standalone === true || (!!window.matchMedia &&
    matchMedia('(display-mode: standalone), (display-mode: fullscreen)').matches);
  if (!standalone || document.querySelector('[data-no-pull-refresh]')) return;
  document.documentElement.classList.add('ptr-on');

  var THRESHOLD = 70; // finger travel (px) that arms a refresh
  var SLOP = 10;      // travel before deciding whether the drag is ours
  var IDLE = 0, DECIDING = 1, PULLING = 2, DONE = 3;

  var state = IDLE, x0 = 0, y0 = 0, dy = 0, timer;
  var dirty = [];
  var el = document.createElement('div');
  el.className = 'ptr';
  el.id = 'pull-refresh';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.innerHTML = '<svg aria-hidden="true"><use href="#i-refresh-cw"></use></svg><span></span>';
  document.body.appendChild(el);
  var use = el.querySelector('use'), label = el.querySelector('span');

  function reduced() {
    return matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // Unsaved work means a POST form (or an htmx one) the user has typed into.
  // GET forms are filters: reloading them loses nothing. A form htmx swapped
  // out is no longer connected, so it stops counting by itself.
  function track(e) {
    var f = e.target && e.target.form;
    if (!f || dirty.indexOf(f) >= 0) return;
    if (String(f.method).toLowerCase() === 'post' || f.hasAttribute('hx-post')) dirty.push(f);
  }
  document.addEventListener('input', track, true);
  document.addEventListener('change', track, true);

  function blocked(target) {
    if (document.querySelector('.overlay.is-open')) return true;
    for (var i = 0; i < dirty.length; i++) if (dirty[i].isConnected) return true;
    // A drag inside a scrolled panel scrolls that panel, not the page.
    for (var n = target; n && n !== document.body; n = n.parentElement) {
      if (n.scrollTop > 0) return true;
    }
    return false;
  }

  // Apple's rubber band: the further you pull, the less it follows.
  function rubber(d) {
    var dim = window.innerHeight, c = 0.55;
    return (d * dim * c) / (dim + c * d);
  }

  function place(y) {
    if (reduced()) return;
    el.style.setProperty('--ptr-y', y + 'px');
    el.style.setProperty('--ptr-r', (y * 4) + 'deg');
  }

  function show(text, icon) {
    clearTimeout(timer);
    label.textContent = text;
    use.setAttribute('href', '#i-' + (icon || 'refresh-cw'));
    el.classList.toggle('is-static', reduced());
    el.classList.add('is-visible');
  }

  function hide() {
    state = IDLE;
    el.classList.add('is-settling');
    el.classList.remove('is-visible', 'is-refreshing');
    place(0);
    timer = setTimeout(function () { label.textContent = ''; }, 300);
  }

  document.addEventListener('touchstart', function (e) {
    if (state === DONE) return;
    state = IDLE;
    if (e.touches.length !== 1 || window.scrollY > 0 || blocked(e.target)) return;
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
    dy = 0;
    state = DECIDING;
    el.classList.remove('is-settling');
  }, { passive: true });

  // The one non-passive listener, and it has to be: only preventDefault()
  // stops iOS rubber-banding the page down along with the indicator. It is
  // registered only on pages where pull-to-refresh is live (never on the
  // scoreboard), returns at once unless a pull is underway, and cancels
  // nothing until a drag has proved itself vertical and downward -- so normal
  // scrolling and horizontal swipes are left alone.
  document.addEventListener('touchmove', function (e) {
    if (state !== DECIDING && state !== PULLING) return;
    var t = e.touches[0], x = t.clientX - x0, y = t.clientY - y0;
    if (state === DECIDING) {
      if (Math.abs(x) < SLOP && Math.abs(y) < SLOP) return;
      if (e.touches.length !== 1 || y <= 0 || Math.abs(x) > y) { state = IDLE; return; }
      state = PULLING;
    }
    e.preventDefault();
    dy = Math.max(0, y);
    place(rubber(dy));
    show(dy >= THRESHOLD ? 'Release to refresh' : 'Pull to refresh');
  }, { passive: false });

  function end(e) {
    if (state !== PULLING) { if (state !== DONE) state = IDLE; return; }
    if (e.type !== 'touchend' || dy < THRESHOLD) return hide();
    state = DONE;
    if (navigator.onLine === false) {
      show('No connection', 'wifi-off');
      setTimeout(hide, 2000);
      return;
    }
    show('Refreshing…');
    if (!reduced()) el.classList.add('is-refreshing');
    location.reload();
  }
  document.addEventListener('touchend', end, { passive: true });
  document.addEventListener('touchcancel', end, { passive: true });

  // Back via bfcache restores this document mid-refresh; start clean.
  window.addEventListener('pageshow', function (e) { if (e.persisted) hide(); });
})();
