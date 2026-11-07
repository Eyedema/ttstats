const { test } = require('@playwright/test');
const { defineOfflineCases } = require('./scoreboard-offline-cases');

/**
 * Network chaos for the live scoreboard in `iphone-safari` (WebKit, touch,
 * iPhone viewport). The cases live in scoreboard-offline-cases.js, shared
 * with the desktop-chrome run in scoreboard-offline.desktop.spec.js.
 */
test.describe('scoreboard offline (iPhone)', () => {
  defineOfflineCases();
});
