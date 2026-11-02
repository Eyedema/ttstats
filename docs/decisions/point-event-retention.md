# PointEvent retention: keep everything, and watch the number


**The policy is to keep every `PointEvent` row indefinitely** (option 1 of
T2.8). The rollups mean no page ever scans raw point rows, so nothing gets
slower as the table grows -- only the disk fills, and at ~60-100 rows per
match, ~50,000 rows per 500-match year, that takes years. Deleting data to
solve a problem the app does not have is the more expensive choice, and it is
irreversible.

- **The decision has a trigger point, not an expiry date**:
  `manage.py point_storage` reports rows, growth per year and a per-club
  breakdown, and prints `THRESHOLD CROSSED` in red at **1,000,000 rows or
  250,000 rows/year** (`REVISIT_ROWS` / `REVISIT_ROWS_PER_YEAR`). ~150 bytes
  a row with both indexes, so a million rows is 150-200MB -- material on a
  3.7GB VPS. Surfaced, never silently passed.
- **What pruning would cost, if it is ever adopted:** career and head-to-head
  figures are unaffected (they read rollups); only match detail's momentum
  chart reads the raw log, and it degrades to *absent*, which is what it
  already does for every non-live-scored match. `test_point_retention.py`
  asserts all four of those, so "rollups remain sufficient" cannot quietly
  stop being true.
- **Any automated pruning is blocked on Epic 3's T3.6 scheduled-job runner**,
  which does not exist. There is no cron here and `push.py` documents the
  absence of Celery as deliberate.

