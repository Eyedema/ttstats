#!/usr/bin/env bash
# Run the Playwright suite with almost no terminal output.
#
# The `list` reporter prints a line per test per project plus a full stack,
# code frame and page snapshot per failure. When an agent runs the suite all
# of that lands in its context, and a clean run of the whole matrix costs
# hundreds of lines that say nothing. This wrapper sends the full output to a
# log and prints only the counts and, per failure, one line of where and one
# line of why. Read the log only when a failure needs more than that.
#
# Usage: scripts/e2e_quiet.sh [any playwright test args]
#   npm run test:e2e:quiet -- mobile-drawer --project=iphone-safari
#   npm run test:e2e:quiet -- --only-changed=master
set -uo pipefail

cd "$(dirname "$0")/.."
OUT=tests/e2e/.last-run
mkdir -p "$OUT"
# A run that dies before reporting must not summarise the previous run.
rm -f "$OUT/results.json"

PLAYWRIGHT_JSON_OUTPUT_NAME="$OUT/results.json" \
  npx playwright test --reporter=list,json,html "$@" >"$OUT/output.log" 2>&1
status=$?

if [ -s "$OUT/results.json" ]; then
  node scripts/e2e_summary.mjs "$OUT/results.json"
else
  # Playwright died before reporting (server failed to boot, bad args...).
  echo "No results written. Last lines of $OUT/output.log:"
  tail -n 20 "$OUT/output.log"
fi
[ "$status" -ne 0 ] && echo "Full output: $OUT/output.log  (HTML: npx playwright show-report)"
exit "$status"
