/* TTStats push notification client.
 *
 * Loaded on every page (base.html). Two jobs:
 *
 *   1. Always: register the service worker, and if this browser already has a
 *      push subscription, re-POST it to the server. Browsers rotate and drop
 *      subscriptions on their own schedule, and the only way to notice is to
 *      keep re-asserting the current one. Without this, notifications quietly
 *      stop for a device and nobody finds out until a match goes unconfirmed.
 *
 *   2. On the notification settings page only: drive the enable/disable/test
 *      controls and report which of the many ways this can fail applied.
 *
 * Plain JS, no Alpine, on purpose -- see CLAUDE.md. Production's CSP has no
 * 'unsafe-eval', so Alpine expressions do not run there, and this is exactly
 * the kind of UI that must not depend on a framework booting.
 *
 * Everything degrades to "push is off": no service worker support, no
 * PushManager, a denied permission and a failed registration all land in a
 * visible state rather than a broken button.
 */
(function () {
  'use strict';

  const configEl = document.getElementById('push-config');
  if (!configEl) {
    return;
  }
  const config = JSON.parse(configEl.textContent);

  // Two different capabilities, and conflating them breaks iOS. Registering
  // the worker is what makes the app installable; iOS Safari does NOT expose
  // PushManager until the app has been installed, so gating registration on
  // push support means the app can never reach the state where push becomes
  // available. Register whenever we can; ask about push separately.
  const swSupported = 'serviceWorker' in navigator;
  const pushSupported =
    swSupported && 'PushManager' in window && 'Notification' in window;

  // iOS only exposes PushManager to a page launched from the Home Screen. In
  // Safari proper the APIs are simply absent, so "unsupported" and "you need
  // to install it first" are indistinguishable without this check -- and
  // telling an iPhone user their browser cannot do push, when it can once
  // installed, is the single most confusing outcome available here.
  const isIOS =
    /iP(hone|ad|od)/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isStandalone =
    window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;

  // ---------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------

  /* The VAPID public key travels as base64url text but applicationServerKey
   * wants raw bytes. */
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = window.atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) {
      output[i] = raw.charCodeAt(i);
    }
    return output;
  }

  /* The CSRF cookie is HttpOnly (settings/base.py), so the token cannot be
   * read from document.cookie. It comes through the config blob instead --
   * the same trick base.html's hx-headers uses. */
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': config.csrfToken,
      },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
  }

  function register() {
    return navigator.serviceWorker.register(config.urls.serviceWorker, {
      scope: '/',
    });
  }

  function currentSubscription() {
    return navigator.serviceWorker.ready.then((reg) =>
      reg.pushManager.getSubscription()
    );
  }

  function syncSubscription(subscription) {
    return postJSON(config.urls.subscribe, subscription.toJSON());
  }

  // ---------------------------------------------------------------------
  // 1. Every page: register and repair
  // ---------------------------------------------------------------------

  function bootstrap() {
    if (!swSupported) {
      return Promise.resolve(null);
    }
    return register()
      .then(() => (pushSupported ? currentSubscription() : null))
      .then((subscription) => {
        // Only re-assert an existing subscription. Never subscribe here --
        // that would trigger a permission prompt on an arbitrary page, which
        // browsers rightly punish and users rightly refuse.
        if (subscription) {
          return syncSubscription(subscription).then(() => subscription);
        }
        return null;
      })
      .catch((err) => {
        console.warn('[ttstats] service worker registration failed', err);
        return null;
      });
  }

  // ---------------------------------------------------------------------
  // 2. Settings page controls
  // ---------------------------------------------------------------------

  const panel = document.querySelector('[data-push-panel]');

  function showState(name) {
    if (!panel) return;
    panel.querySelectorAll('[data-push-state]').forEach((el) => {
      el.classList.toggle('hidden', el.dataset.pushState !== name);
    });
  }

  function setStatus(text, tone) {
    const el = panel && panel.querySelector('[data-push-status]');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('hidden', !text);
    el.classList.toggle('text-destructive', tone === 'error');
    el.classList.toggle('text-muted-foreground', tone !== 'error');
  }

  /* Decide which of the mutually exclusive panel states applies. Order
   * matters: the most specific, most actionable reason wins. */
  function resolveState(subscription) {
    if (!config.enabled) return 'unconfigured';
    if (isIOS && !isStandalone && !pushSupported) return 'ios-install';
    if (!pushSupported) return 'unsupported';
    if (Notification.permission === 'denied') return 'denied';
    return subscription ? 'on' : 'off';
  }

  function refresh() {
    if (!panel) return Promise.resolve();
    if (!pushSupported || !config.enabled) {
      showState(resolveState(null));
      return Promise.resolve();
    }
    return currentSubscription()
      .then((subscription) => showState(resolveState(subscription)))
      .catch(() => showState('unsupported'));
  }

  function enable() {
    setStatus('');
    return Notification.requestPermission()
      .then((permission) => {
        if (permission !== 'granted') {
          showState(permission === 'denied' ? 'denied' : 'off');
          setStatus('Permission was not granted.', 'error');
          return null;
        }
        return navigator.serviceWorker.ready.then((reg) =>
          reg.pushManager.subscribe({
            // Required by every browser now; a push you cannot read is not a
            // push anyone wants to receive.
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(config.vapidPublicKey),
          })
        );
      })
      .then((subscription) => {
        if (!subscription) return;
        return syncSubscription(subscription).then((res) => {
          if (!res.ok) throw new Error('Server rejected the subscription');
          showState('on');
          setStatus('This device will now receive notifications.');
        });
      })
      .catch((err) => {
        console.warn('[ttstats] enabling push failed', err);
        setStatus('Could not enable notifications on this device.', 'error');
        return refresh();
      });
  }

  function disable() {
    setStatus('');
    return currentSubscription()
      .then((subscription) => {
        if (!subscription) return null;
        const endpoint = subscription.endpoint;
        // Tell the server first. If unsubscribe() succeeds and the POST then
        // fails, the server keeps pushing to an endpoint the browser has
        // discarded -- silent failures until the push service expires it.
        return postJSON(config.urls.unsubscribe, { endpoint }).then(() =>
          subscription.unsubscribe()
        );
      })
      .then(() => {
        showState('off');
        setStatus('This device will no longer receive notifications.');
      })
      .catch((err) => {
        console.warn('[ttstats] disabling push failed', err);
        setStatus('Could not turn notifications off.', 'error');
        return refresh();
      });
  }

  function sendTest() {
    setStatus('Sending...');
    return postJSON(config.urls.test)
      .then((res) => res.json())
      .then((data) => {
        setStatus(
          data.ok
            ? 'Test notification sent.'
            : 'Nothing was delivered -- this device may not be subscribed.',
          data.ok ? null : 'error'
        );
      })
      .catch(() => setStatus('Could not send the test notification.', 'error'));
  }

  function wire() {
    if (!panel) return;
    const on = (selector, handler) => {
      panel.querySelectorAll(selector).forEach((el) =>
        el.addEventListener('click', (event) => {
          event.preventDefault();
          handler();
        })
      );
    };
    on('[data-push-enable]', enable);
    on('[data-push-disable]', disable);
    on('[data-push-test]', sendTest);
  }

  bootstrap().then(() => {
    wire();
    return refresh();
  });
})();
