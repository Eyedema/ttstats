const { test } = require('@playwright/test');
const { defineOfflineCases } = require('./scoreboard-offline-cases');

/**
 * The same network-chaos cases in `desktop-chrome`: Chromium's network stack
 * and route handling differ from WebKit's, and the offline flag is emulated
 * differently by each engine.
 */
test.describe('scoreboard offline (desktop Chrome)', () => {
  defineOfflineCases();
});
