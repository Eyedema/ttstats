"""Template helpers for the rating UI (KAN-71).

Thresholds are read at call time from the modules that own them, so a template
never types a number and a cached page follows a retune.
"""

import re

from django import template

from .. import elo, glicko_adapter, seasons

register = template.Library()


@register.simple_tag
def provisional_rd():
    """The display RD above which a Glicko rating is provisional."""
    return round(glicko_adapter.PROVISIONAL_RD)


@register.simple_tag
def provisional_matches():
    """The match count under which an Elo rating is provisional."""
    return elo.PROVISIONAL_MATCHES


@register.simple_tag
def rating_mean():
    """The rating a season's carry-over is pulled toward."""
    return seasons.RATING_MEAN


@register.simple_tag
def season_rd_floor():
    """The least ± a season rating starts with."""
    return round(glicko_adapter.DEFAULT_PARAMS.season_rd_floor)


_RATED_ABOVE = re.compile(r"\d+\+ Elo above you")


@register.filter
def neutral_rating_copy(text, rating_system=""):
    """Achievement copy without the system's name, under Glicko only.

    The achievement logic stays on Elo until KAN-73; only the words change,
    and only for a Glicko viewer, so Elo pages render exactly as before.
    """
    if rating_system != "glicko2" or not text:
        return text
    text = _RATED_ABOVE.sub("well above you", str(text))
    return text.replace("personal Elo peak", "personal rating peak")
