#!/bin/bash
# run from ttstats/ttstats (where manage.py is located), like so:
# ../scripts/run_tests.sh
set -e

echo "🧪 Running Django tests with coverage..."
rm -f .coverage
rm -rf htmlcov/


coverage run --rcfile=../.coveragerc manage.py test pingpong --verbosity=1

echo ""
echo "📊 Coverage Report:"
echo "===================="
coverage report

coverage html
echo ""
echo "✅ HTML coverage report generated in htmlcov/index.html"

COVERAGE=$(coverage report | grep TOTAL | awk '{print $4}' | sed 's/%//')
echo ""
echo "📈 Total Coverage: ${COVERAGE}%"

exit 0
