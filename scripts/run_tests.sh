#!/bin/bash
# run from ttstats/ttstats (where manage.py is located), like so:
# ../scripts/run_tests.sh
set -e

echo "🧪 Running pytest with coverage..."
rm -f .coverage
rm -rf htmlcov/

# Run pytest with coverage
pytest --cov=pingpong --cov-config=../.coveragerc --cov-report=term --cov-report=html -v

echo ""
echo "✅ HTML coverage report generated in htmlcov/index.html"

# Extract and display total coverage
COVERAGE=$(coverage report | grep TOTAL | awk '{print $4}' | sed 's/%//')
echo ""
echo "📈 Total Coverage: ${COVERAGE}%"

exit 0
