"""Template helpers for the rating UI (KAN-71).

Thresholds are read at call time from the modules that own them, so a template
never types a number and a cached page follows a retune.
"""

from django import template

from .. import glicko_adapter, seasons

register = template.Library()


@register.simple_tag
def provisional_rd():
    """The display RD above which a rating is provisional."""
    return round(glicko_adapter.PROVISIONAL_RD)


@register.simple_tag
def rating_mean():
    """The rating a season's carry-over is pulled toward."""
    return seasons.RATING_MEAN


@register.simple_tag
def season_rd_floor():
    """The least ± a season rating starts with."""
    return round(glicko_adapter.DEFAULT_PARAMS.season_rd_floor)
