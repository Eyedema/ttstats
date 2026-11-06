const { definePullToRefreshCases } = require('./pull-to-refresh-cases');

/**
 * Pull to refresh in the app installed to an iPhone home screen (KAN-126).
 * Runs in `iphone-pwa`, the project for standalone behaviour; the cases are
 * shared with the Android engines in pull-to-refresh-cases.js.
 */
definePullToRefreshCases();
