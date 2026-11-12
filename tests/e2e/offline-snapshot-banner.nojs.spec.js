const { test } = require('@playwright/test');
const { defineBannerCases } = require('./offline-snapshot-banner-cases');

// KAN-145 with JavaScript off (KAN-138 case 13): the banner is CSS alone, so
// it is hidden on a live page and shown on a saved copy without any script.
defineBannerCases(test, { geometry: false });
