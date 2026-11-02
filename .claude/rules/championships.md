---
paths:
  - "ttstats/pingpong/championship_scheduling.py"
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/templates/pingpong/championship*.html"
  - "ttstats/pingpong/tests/test_championship*.py"
---

# Championships

- Championship views use `Match.all_objects` / `ScheduledMatch.all_objects` --
  participants see every championship match. Transitively club-safe because the
  championship came through the scoped manager.
- `ScheduledMatchConvertView.form_valid()` sets `match.championship` when
  converting a championship scheduled match.
- `check_completion()` moves to `completed` when all scheduled matches are
  converted and confirmed.
- Round-robin pairing is pure (`round_robin_rounds`, `round_robin_double_rounds`
  in `championship_scheduling.py`): circle method, home + away.
- Entrants are `ChampionshipEntry` + `ChampionshipEntryMember`. The denormalized
  `championship` FK on the member row enforces one entry per player. Register
  via `championship.register_entry(players)`.
- **`generate_schedule()` uses `bulk_create`, which bypasses `post_save`**, so it
  builds `ScheduledMatchParticipant` rows explicitly. Any new bulk path must too.
