const { test, expect } = require('@playwright/test');
const config = require('../../playwright.config.js');

/**
 * KAN-96: service workers are blocked in every project except `iphone-pwa`.
 *
 * A worker controlling the page makes page.route() -- and so applyProdCSP()
 * -- silently skip its requests (WebKit: always; Chromium and Gecko: once a
 * route answers with respondWith()). A spec in a project that allowed workers
 * would then pass or fail on a race. This pins the config so a new project,
 * or an edited one, cannot quietly start allowing them.
 * See docs/decisions/kan-76-sw-e2e-audit.md.
 */
const SW_PROJECTS = ['iphone-pwa'];

test('only the PWA project lets a service worker register', () => {
  const effective = Object.fromEntries(
    config.projects.map((project) => [
      project.name,
      (project.use && project.use.serviceWorkers) || config.use.serviceWorkers || 'allow',
    ]),
  );

  for (const [name, mode] of Object.entries(effective)) {
    expect(mode, `project ${name}`).toBe(SW_PROJECTS.includes(name) ? 'allow' : 'block');
  }
});
