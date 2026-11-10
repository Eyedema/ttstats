"""Write-side helpers shared by the match/scheduled-match views."""
from django.db import IntegrityError, transaction
from django.db.models import Case, Value, When
from django.utils import timezone

from . import live_scoring
from .models import (
    ChampionshipEntry,
    LiveLobby,
    LiveScorer,
    Match,
    MatchParticipant,
    ScheduledMatchParticipant,
    Side,
)


def set_match_sides(match, side1_players, side2_players):
    """Replace a match's participants with the given players."""
    _set_sides(match, MatchParticipant, "match", side1_players, side2_players)


def create_live_match(
    match, *, scorekeeper, side1_players, side2_players, can_score_both=True
):
    """Save an unsaved ``Match`` as a live match, with its host scorer row.

    The one way a live match comes into existence (KAN-149): the "Score live"
    branch of ``MatchCreateView`` and the join flow (KAN-152) both call it.
    The caller builds ``match`` with its own fields (best of, location, an
    explicit ``club`` when the viewer's active club may differ); this sets the
    live fields, writes the sides, recomputes, and creates the host
    ``LiveScorer`` on the scorekeeper's side (side 1 for a scorekeeper who is
    not playing). ``can_score_both`` is True for a solo scorer; a shared match
    passes False and adds the guest row itself.
    """
    with transaction.atomic():
        match.is_live = True
        match.scorekeeper = scorekeeper
        match.live_state = live_scoring.initial_state(match.best_of)
        match.save()
        set_match_sides(match, side1_players, side2_players)
        match.save()  # recompute now that the sides exist
        side = (
            Side.TWO
            if any(p.pk == scorekeeper.pk for p in side2_players)
            else Side.ONE
        )
        LiveScorer.objects.create(
            match=match,
            player=scorekeeper,
            side=side,
            role=LiveScorer.Role.HOST,
            can_score_both=can_score_both,
            last_seen_at=timezone.now(),
        )
    return match


def open_lobby(host, *, best_of, location=None):
    """Open a shared-scoring lobby for ``host`` (KAN-151), closing any other.

    At most one open lobby per host: an earlier open one is cancelled (or
    marked expired, if it already was) in the same transaction, and a partial
    unique constraint backs that up. Two simultaneous creates for one host
    can still collide on the constraint; the loser retries once and wins.
    The club is stamped from the viewer by ``ClubStampedMixin``.
    """
    for attempt in (1, 2):
        try:
            with transaction.atomic():
                now = timezone.now()
                # all_objects, keyed on the host: a lobby the host left open
                # while viewing another club must close too (KAN-24 audit).
                LiveLobby.all_objects.filter(
                    host=host, status=LiveLobby.Status.OPEN
                ).update(
                    status=Case(
                        When(expires_at__lte=now, then=Value(LiveLobby.Status.EXPIRED)),
                        default=Value(LiveLobby.Status.CANCELLED),
                    )
                )
                lobby = LiveLobby(
                    host=host, best_of=best_of, location=location, created_at=now
                )
                lobby.save()
                return lobby
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def cancel_lobby(lobby):
    """Cancel ``lobby`` if it is still open. True if this call cancelled it.

    Conditional, like the join's claim, so a cancel racing a join cannot
    un-join a lobby that already has a match.
    """
    cancelled = LiveLobby.all_objects.filter(
        pk=lobby.pk, status=LiveLobby.Status.OPEN
    ).update(status=LiveLobby.Status.CANCELLED)
    if cancelled:
        lobby.status = LiveLobby.Status.CANCELLED
    return bool(cancelled)


def start_shared_match(lobby, joiner_player, request):
    """Claim ``lobby`` for ``joiner_player`` and create the shared live match.

    Returns the match, or None when the lobby was no longer open (another
    joiner won, the host cancelled, it expired). The caller has already
    checked that the joiner may join (a member of the lobby's club, not the
    host).

    The claim is a conditional ``.update()``: two simultaneous joins cannot
    both win, because only one UPDATE matches ``status=open``. The match is
    created through ``create_live_match`` like any live match, with
    ``club=lobby.club`` **explicitly** (the joiner's active club may be
    another of theirs): host on side 1 (role host), joiner on side 2 (role
    guest), both ``can_score_both=False``. The joiner's active club becomes
    the lobby's, so the scoreboard's scoped lookup finds the match.
    """
    from ttstats.middleware import set_active_club

    host = lobby.host
    with transaction.atomic():
        claimed = LiveLobby.all_objects.filter(
            pk=lobby.pk,
            status=LiveLobby.Status.OPEN,
            expires_at__gt=timezone.now(),
        ).update(status=LiveLobby.Status.JOINED)
        if not claimed:
            return None
        match = create_live_match(
            Match(best_of=lobby.best_of, location=lobby.location, club=lobby.club),
            scorekeeper=host,
            side1_players=[host],
            side2_players=[joiner_player],
            can_score_both=False,
        )
        LiveScorer.objects.create(
            match=match,
            player=joiner_player,
            side=Side.TWO,
            role=LiveScorer.Role.GUEST,
            can_score_both=False,
            last_seen_at=timezone.now(),
        )
        LiveLobby.all_objects.filter(pk=lobby.pk).update(match=match)
    lobby.status = LiveLobby.Status.JOINED
    lobby.match = match
    if lobby.club is not None:
        set_active_club(request, lobby.club)
    return match


def set_scheduled_match_sides(scheduled_match, side1_players, side2_players):
    _set_sides(
        scheduled_match,
        ScheduledMatchParticipant,
        "scheduled_match",
        side1_players,
        side2_players,
    )


def _set_sides(parent, participant_model, parent_field, side1_players, side2_players):
    participant_model.objects.filter(**{parent_field: parent}).delete()
    participant_model.objects.bulk_create([
        participant_model(**{parent_field: parent}, player=player, side=side)
        for side, players in (
            (Side.ONE, side1_players),
            (Side.TWO, side2_players),
        )
        for player in players
    ])
    # `Match.sides()` memoizes these rows. Rewriting them without saying so
    # leaves the caller holding the previous line-up -- and this is the one
    # path in the app that rewrites them.
    if hasattr(parent, "invalidate_participant_cache"):
        parent.invalidate_participant_cache()


def link_championship_entries(obj):
    """Point side1_entry / side2_entry at the entries matching each side.

    Runs on save for anything in a championship whose entry links are not set
    yet, so converting a scheduled match, using the admin or building one in a
    factory all end up linked without each remembering to. Already-set links
    are left alone.
    """
    if not obj.championship_id:
        return
    if obj.side1_entry_id and obj.side2_entry_id:
        return

    entries = ChampionshipEntry.objects.filter(
        championship_id=obj.championship_id
    ).prefetch_related("members")
    by_members = {
        frozenset(m.player_id for m in entry.members.all()): entry
        for entry in entries
    }

    updates = {}
    for attr, side in (("side1_entry", Side.ONE), ("side2_entry", Side.TWO)):
        if getattr(obj, f"{attr}_id"):
            continue
        key = frozenset(
            p.player_id for p in obj.participants.all() if p.side == side
        )
        entry = by_members.get(key)
        if entry is not None:
            updates[attr] = entry

    if not updates:
        return

    type(obj).all_objects.filter(pk=obj.pk).update(
        **{f"{attr}_id": entry.pk for attr, entry in updates.items()}
    )
    for attr, entry in updates.items():
        setattr(obj, attr, entry)
