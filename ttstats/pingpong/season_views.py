"""The season archive (KAN-51, S4.6): past seasons and their frozen final tables.

Read **only** from `SeasonStanding`, never recomputed from ratings or
matches: the archive is the record as it stood at rollover, and recomputing
it on view would drift the moment any rule or rating changed. For the same
reason nothing here is cached, and these pages are outside the leaderboard's
cache invalidation entirely -- a frozen table has nothing to invalidate.

Gated on the `seasons` flag (absent means 404, not a hidden link), and
tenancy-scoped through `Season.objects` / `SeasonStanding.objects`: another
club's season is a 404 like any row the viewer cannot see.
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from . import flags
from .models import RatingFormat, Season, SeasonStanding

FORMATS = [RatingFormat.SINGLES, RatingFormat.DOUBLES]


class SeasonsFlagRequiredMixin:
    """404 unless the `seasons` flag is on for this viewer."""

    def dispatch(self, request, *args, **kwargs):
        if not flags.is_enabled("seasons", request.user):
            raise Http404("Seasons are not enabled.")
        return super().dispatch(request, *args, **kwargs)


def _archived():
    return Season.objects.filter(archived_at__isnull=False)


class SeasonArchiveListView(LoginRequiredMixin, SeasonsFlagRequiredMixin, TemplateView):
    """Every archived season in the active club, newest first, with its champions."""

    template_name = "pingpong/seasons/archive_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        seasons = list(_archived().order_by("-starts_on"))

        champions = {}
        for row in (
            SeasonStanding.objects.filter(season__in=seasons, rank=1)
            .select_related("player").order_by("season_id", "format", "-wins", "player_name")
        ):
            champions.setdefault(row.season_id, {}).setdefault(row.format, []).append(row)

        context["seasons"] = [
            {
                "season": season,
                "champions": [
                    (fmt.label, champions[season.pk][fmt.value])
                    for fmt in FORMATS
                    if fmt.value in champions.get(season.pk, {})
                ],
            }
            for season in seasons
        ]
        return context


class SeasonArchiveDetailView(LoginRequiredMixin, SeasonsFlagRequiredMixin, TemplateView):
    """One archived season's frozen table, one format at a time (`?format=`)."""

    template_name = "pingpong/seasons/archive_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        season = get_object_or_404(_archived(), pk=self.kwargs["pk"])

        standings = SeasonStanding.objects.filter(season=season)
        present = set(standings.values_list("format", flat=True).distinct())
        requested = self.request.GET.get("format")
        available = [fmt for fmt in FORMATS if fmt.value in present]
        if requested in present:
            current = requested
        else:
            current = available[0].value if available else RatingFormat.SINGLES.value

        context.update({
            "season": season,
            "formats": [(fmt.value, fmt.label) for fmt in available],
            "current_format": current,
            "current_format_label": RatingFormat(current).label,
            "standings": list(
                standings.filter(format=current).select_related("player")
                .order_by("rank", "-wins", "player_name")
            ),
        })
        return context
