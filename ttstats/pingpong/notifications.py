"""What gets pushed, and to whom.

`push.py` is the transport; this module is the vocabulary. Everything here
takes domain objects and calls `push.send_to_user`, so the wording of a
notification lives in one place instead of being inlined at each signal.

Every function returns the set of Player ids that were reached by push. That
return value is the whole point of the layering: the callers use it to decide
who still needs an email, which is how "push preferred, email as fallback"
stays a single rule rather than a condition repeated at each call site.
"""

import logging

from django.urls import reverse
from django.utils import timezone

from . import flags, push, ratings
from .models import EloHistory, Match, NotificationKind, Player, Side

logger = logging.getLogger(__name__)


def _users_for(players):
    """(player, user) pairs for players who actually have an account."""
    return [(p, p.user) for p in players if p.user_id]


def notify_match_confirmation_needed(match, players):
    """"Confirm the match you just played." Returns Player ids reached.

    This is the notification the feature exists for: match confirmation is
    what gates Elo, and gating it on email is what made results sit unconfirmed
    for days.
    """
    url = reverse('pingpong:match_detail', args=[match.pk])
    side1_ids = {p.pk for p in match.side1_players}
    reached = set()
    for player, user in _users_for(players):
        opponent_side = Side.TWO if player.pk in side1_ids else Side.ONE
        delivered = push.send_to_user(
            user,
            kind=NotificationKind.MATCH_CONFIRMATION,
            title='Confirm your match',
            body=f"{match.side_label(opponent_side)} logged a result against you. Tap to confirm.",
            url=url,
            # Per match, so a re-send replaces the old one instead of stacking.
            tag=f'match-confirm-{match.pk}',
        )
        if delivered:
            reached.add(player.pk)
    return reached


def notify_match_result(match):
    """"Match confirmed -- you're now 1512 (+11)." Returns Player ids reached.

    Reads the Elo deltas from EloHistory rather than taking them as arguments,
    so it can be called from anywhere after `update_player_elo` has run without
    the caller having to thread the numbers through.
    """
    history = {
        eh.player_id: eh
        for eh in EloHistory.objects.filter(match=match).select_related('player')
    }
    if not history:
        return set()

    # Glicko recipients read the RatingChange Glicko columns of the match's
    # own season and format, resolved lazily (once) for the first of them.
    glicko_readings = None
    glicko_possible = flags.glicko_may_be_shown()
    if glicko_possible:
        flags.prefetch_overrides([user for _, user in _users_for(match.all_players)])

    url = reverse('pingpong:match_detail', args=[match.pk])
    winner_ids = {p.pk for p in match.players_on(match.winner_side)}
    reached = set()
    for player, user in _users_for(match.all_players):
        entry = history.get(player.pk)
        if entry is None:
            continue
        won = player.pk in winner_ids
        outcome = f"{'You won' if won else 'You lost'} {match.side1_score}-{match.side2_score}."
        if glicko_possible and ratings.system(user) == ratings.GLICKO:
            if glicko_readings is None:
                glicko_readings = ratings.applied_glicko_readings(
                    match, ratings.display_scope(match)
                )
            reading = glicko_readings.get(player.pk)
            if reading is None:
                rating_text = ''
            else:
                new, change = reading
                rating_text = f" Rating {round(new)} ({change or 0:+d})."
        else:
            sign = '+' if entry.rating_change >= 0 else ''
            rating_text = f" Elo {entry.new_rating} ({sign}{entry.rating_change})."
        delivered = push.send_to_user(
            user,
            kind=NotificationKind.MATCH_RESULT,
            title='Match confirmed',
            body=f"{outcome}{rating_text}",
            url=url,
            tag=f'match-result-{match.pk}',
        )
        if delivered:
            reached.add(player.pk)
    return reached


def notify_scheduled_match(scheduled_match, players):
    """"You have a match on Friday." Returns Player ids reached."""
    url = reverse('pingpong:scheduled_match_detail', args=[scheduled_match.pk])
    when = scheduled_match.scheduled_datetime
    side1_ids = {p.pk for p in scheduled_match.side1_players}
    reached = set()
    for player, user in _users_for(players):
        opponent_side = Side.TWO if player.pk in side1_ids else Side.ONE
        delivered = push.send_to_user(
            user,
            kind=NotificationKind.SCHEDULED_MATCH,
            title='Match scheduled',
            body=(
                f"vs {scheduled_match.side_label(opponent_side)} "
                f"on {when:%a %d %b at %H:%M}."
            ),
            url=url,
            tag=f'scheduled-{scheduled_match.pk}',
        )
        if delivered:
            reached.add(player.pk)
    return reached


def players_passed_on_leaderboard(player, old_rating, new_rating):
    """Players this one overtook by moving from old_rating to new_rating.

    Strictly between the two ratings: a player sitting exactly on the old
    rating was not passed (they were already level, and the leaderboard's tie
    order is arbitrary), and one sitting exactly on the new rating still is
    not below. Both bounds being strict is what keeps this from firing a
    "you've been passed" buzz at someone who is still tied.

    Returns an empty queryset when the rating did not rise.
    """
    if new_rating <= old_rating:
        return Player.objects.none()
    # Player.objects is unfiltered (PlayerManager applies no row-level rule),
    # so there is no all_objects to reach for here.
    return Player.objects.filter(
        elo_rating__gt=old_rating,
        elo_rating__lt=new_rating,
        user__isnull=False,
    ).exclude(pk=player.pk)


def notify_leaderboard_overtakes(match):
    """Tell anyone the match's winners climbed past. Returns Player ids reached.

    Players who were in the match are excluded: they are already getting a
    match-result push, and two buzzes for one game is how people mute an app.
    The ranking compared is each recipient's own system: Elo recipients are
    passed on `Player.elo_rating`, Glicko recipients in the match's table.
    """
    participant_ids = {p.pk for p in match.all_players}
    reached = set()
    glicko_possible = flags.glicko_may_be_shown()

    def push_to(player, climber):
        delivered = push.send_to_user(
            player.user,
            kind=NotificationKind.LEADERBOARD_OVERTAKE,
            title='You just got passed',
            body=f"{climber} moved ahead of you on the leaderboard.",
            url=reverse('pingpong:leaderboard'),
            # Per rival, so repeated overtakes by the same person collapse.
            tag=f'overtake-{climber.pk}',
        )
        if delivered:
            reached.add(player.pk)

    for entry in EloHistory.objects.filter(match=match).select_related('player'):
        climber = entry.player
        passed = players_passed_on_leaderboard(
            climber, entry.old_rating, entry.new_rating
        ).exclude(pk__in=participant_ids).select_related('user')
        for player in passed:
            if not glicko_possible or ratings.system(player.user) == ratings.ELO:
                push_to(player, climber)

    if not glicko_possible:
        return reached
    glicko_recipients = None
    side1, side2 = match.sides()
    scope = ratings.display_scope(match)
    for climber in (side1 if match.winner_side == Side.ONE else side2):
        movement = ratings.glicko_movement(match, scope, climber)
        if movement is None:
            continue
        passed = list(
            ratings.players_between(scope, climber, *movement)
            .exclude(pk__in=participant_ids)
            .select_related('user')
        )
        # One query for every recipient's overrides, not one each.
        flags.prefetch_overrides([p.user for p in passed])
        for player in passed:
            if glicko_recipients is None:
                glicko_recipients = {}
            if glicko_recipients.setdefault(
                player.pk, ratings.system(player.user) == ratings.GLICKO
            ):
                push_to(player, climber)

    return reached


def notify_season_ended(player, *, season_name, standings, url):
    """"Autumn 2026 has ended: #2 in Singles." Returns Player ids reached.

    Takes plain values rather than a Season so the rollover job owns the
    domain and this module owns only the wording. ``standings`` is an iterable
    of ``(format_label, rank)``; one push covers every format, because the
    kind's daily limit is one per player. A push suppressed by that limit, a
    mute or a missing device leaves the player out of the returned set, and
    the caller emails them.
    """
    if not player.user_id:
        return set()
    ranks = ', '.join(f"#{rank} in {label}" for label, rank in standings)
    delivered = push.send_to_user(
        player.user,
        kind=NotificationKind.SEASON_ENDED,
        title=f"{season_name} has ended",
        body=f"Your final rank: {ranks}." if ranks else "See the final standings.",
        url=url,
        tag=f'season-ended-{season_name}',
    )
    return {player.pk} if delivered else set()


def notify_match_confirmed(match):
    """Fire the post-confirmation notifications exactly once per match.

    Three signal handlers can reach a confirmed match with Elo applied, and
    two of them run concurrently when both players confirm at the same moment.
    Guarding on an in-memory flag would still double-send across those two
    requests, so the claim is a conditional UPDATE: whichever transaction sets
    `result_notified_at` from NULL first wins, and the loser sees 0 rows
    updated and does nothing.

    Uses all_objects because MatchManager filters by the logged-in user, and
    the player confirming is not necessarily one of the participants (staff
    can confirm on someone's behalf).

    Returns the set of Player ids reached by any push.
    """
    if match.result_notified_at is not None:
        return set()
    if not match.winner_side or not match.is_confirmed:
        return set()

    claimed = Match.all_objects.filter(
        pk=match.pk, result_notified_at__isnull=True
    ).update(result_notified_at=timezone.now())
    if not claimed:
        return set()

    reached = notify_match_result(match)
    reached |= notify_leaderboard_overtakes(match)
    return reached
