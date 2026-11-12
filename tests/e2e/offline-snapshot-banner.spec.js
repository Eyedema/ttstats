const { test } = require('@playwright/test');
const { defineBannerCases } = require('./offline-snapshot-banner-cases');

// KAN-145 in WebKit at iPhone size, light palette. Dark: .dark.spec.js; no JS: .nojs.spec.js.
defineBannerCases(test);
