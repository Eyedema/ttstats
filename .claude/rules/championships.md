---
paths:
  - "ttstats/pingpong/championship_scheduling.py"
  - "ttstats/pingpong/models.py"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/templates/pingpong/championship*.html"
  - "ttstats/pingpong/tests/test_championship*.py"
---

# Championships

### Championship System
- Championship matches may have winners but `is_confirmed=False` — always filter by `winner_side__isnull=False` for championship data, not `is_confirmed=True`
- Use `Match.all_objects` and `ScheduledMatch.all_objects` in championship views to bypass row-level security
- `ScheduledMatchConvertView.form_valid()` auto-sets `match.championship` FK when converting championship scheduled matches
- `check_completion()` auto-transitions to `completed` when all scheduled matches are converted and confirmed
- Round-robin pairing is a pure function in `championship_scheduling.py` (`round_robin_rounds`, `round_robin_double_rounds`) -- circle method, home + away (andata e ritorno). Test it without a DB.
- Entrants are `ChampionshipEntry` + `ChampionshipEntryMember`, not teams. The denormalized `championship` FK on the member row lets the DB enforce one entry per player per championship. Register via `championship.register_entry(players)`.
- **`generate_schedule()` uses `bulk_create`, which bypasses `post_save`** — it therefore builds `ScheduledMatchParticipant` rows explicitly. Any new bulk path must do the same or the schedule comes out with no participants.
