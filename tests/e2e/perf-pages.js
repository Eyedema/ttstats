// The key pages KAN-119 measures, shared by `npm run perf` (scripts/perf.mjs)
// and the byte budgets (budgets.spec.js) so both always look at the same set.
//
// Seeded ids differ between runs, so the paths that need one are resolved
// against the live server with a logged-in page. Resolution navigates; call
// it on a page you will throw away (or before you start recording).
const { openMomentumMatch, openPlayerByName } = require('./helpers');

const STATIC_PAGES = [
  { name: 'dashboard', path: '/pingpong/' },
  { name: 'play', path: '/pingpong/play/' },
  { name: 'leaderboard', path: '/pingpong/leaderboard/' },
  { name: 'match list', path: '/pingpong/matches/' },
];

async function resolvePages(page) {
  const matchPath = await openMomentumMatch(page);
  const playerPath = await openPlayerByName(page, 'E2E Player');

  await page.goto('/pingpong/head-to-head/');
  const ids = await page.evaluate(() => {
    const opts = Array.from(document.querySelectorAll('select option'));
    const find = (name) => opts.find((o) => o.textContent.trim().includes(name));
    return {
      one: find('E2E Player') && find('E2E Player').value,
      two: find('E2E Momentum') && find('E2E Momentum').value,
    };
  });
  if (!ids.one || !ids.two) throw new Error('Seeded players missing from head-to-head');

  return [
    ...STATIC_PAGES,
    // The momentum match: the one page that loads Chart.js today.
    { name: 'match detail', path: matchPath },
    { name: 'player detail', path: playerPath },
    {
      name: 'head-to-head',
      path: `/pingpong/head-to-head/?player1=${ids.one}&player2=${ids.two}`,
    },
  ];
}

/** Bucket a Playwright resourceType (or CDP type, lower-cased) for reporting. */
function bucketFor(resourceType) {
  switch (resourceType) {
    case 'document': return 'html';
    case 'script': return 'js';
    case 'stylesheet': return 'css';
    case 'font': return 'font';
    case 'image': return 'img';
    default: return 'other';
  }
}

module.exports = { resolvePages, bucketFor, STATIC_PAGES };
