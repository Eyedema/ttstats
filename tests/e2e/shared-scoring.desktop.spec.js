const { test } = require('@playwright/test');
const { defineTwoPhoneCases } = require('./shared-scoring-two-phones');

/**
 * The two-phone cases (KAN-155) in `desktop-chrome`: Chromium's network
 * stack, route handling and click (not tap) input, against the same server.
 */
test.describe('two phones, one match (desktop Chrome)', () => {
  defineTwoPhoneCases();
});
