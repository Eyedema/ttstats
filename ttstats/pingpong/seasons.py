"""Seasons: pure decisions, no Django (KAN-9, docs/decisions/kan-9-seasons.md).

Same shape as `match_state.py` and `live_scoring.py`: the rules live here,
the models and the Elo pipeline call them, and the tests need no database.

* A season is `[starts_on, ends_on]`, both **inclusive**, in UTC dates. A
  match belongs to the season containing `date_played.date()`.
* A day in no season (before the first, or in a hand-made gap) resolves to
  `None`. That is defined behaviour: the match counts toward all-time ratings
  only, never silently toward nothing.
* A player's first rating in a season is a soft reset toward the mean:
  `1500 + SEASON_CARRY_FACTOR * (R - 1500)`, R being their all-time rating in
  that format just before the match.
"""
import calendar
import math
from datetime import date, datetime, timedelta

RATING_MEAN = 1500
SEASON_CARRY_FACTOR = 0.5
DEFAULT_FIRST_SEASON_MONTHS = 3


def _as_date(day):
    return day.date() if isinstance(day, datetime) else day


def contains(season, day):
    """True when `day` falls inside `season` (both ends inclusive)."""
    day = _as_date(day)
    return season.starts_on <= day <= season.ends_on


def resolve_season(day, seasons):
    """The season containing `day`, or None.

    `seasons` is any iterable of objects with `starts_on`/`ends_on`. The
    database forbids overlap within a club, so two matches mean the input was
    not one club's seasons: that raises rather than guessing.
    """
    found = [s for s in seasons if contains(s, day)]
    if len(found) > 1:
        raise ValueError(f"{len(found)} seasons contain {_as_date(day)}; seasons must not overlap")
    return found[0] if found else None


def overlaps(a_starts, a_ends, b_starts, b_ends):
    """Two inclusive date ranges share at least one day."""
    return a_starts <= b_ends and b_starts <= a_ends


def add_months(day, months):
    """`day` plus whole calendar months, clamping to the target month's end."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _whole_months(starts_on, ends_on):
    """How many calendar months the season spans exactly, or None."""
    if starts_on.day != 1:
        return None
    if ends_on.day != calendar.monthrange(ends_on.year, ends_on.month)[1]:
        return None
    return (ends_on.year - starts_on.year) * 12 + ends_on.month - starts_on.month + 1


def next_season_bounds(starts_on, ends_on):
    """(starts_on, ends_on) of the season after this one: same length, from the day after.

    "Same length" means the same number of calendar months when the season
    spans whole months (a quarter rolls to the next quarter, not to 90 days),
    and the same number of days otherwise.
    """
    next_start = ends_on + timedelta(days=1)
    months = _whole_months(starts_on, ends_on)
    if months:
        return next_start, add_months(next_start, months) - timedelta(days=1)
    return next_start, next_start + (ends_on - starts_on)


def first_season_bounds(starts_on, months=DEFAULT_FIRST_SEASON_MONTHS):
    """A club's first season: `months` calendar months from `starts_on`."""
    return starts_on, add_months(starts_on, months) - timedelta(days=1)


def carry_over_seed(all_time_rating):
    """The seed for a player's first season rating in a format.

    `None` (unrated in that format) starts at the mean. Rounds half up, so the
    result does not depend on Python's banker's rounding.
    """
    if all_time_rating is None:
        return RATING_MEAN
    raw = RATING_MEAN + SEASON_CARRY_FACTOR * (all_time_rating - RATING_MEAN)
    return math.floor(raw + 0.5)
