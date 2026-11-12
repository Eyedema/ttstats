"""Shared live scoring: the host's lobby and the opponent's join link.

KAN-151 (host side) and KAN-152 (join flow), epic KAN-148. The host starts a
live match without an opponent and shows a QR code of
``/pingpong/live/join/<token>/``; whoever opens it, is signed in and is in the
club becomes side 2, and both phones land on the scoreboard.

**The join token is a bearer credential** and follows the club-invite rules
(`.claude/rules/clubs.md`): looked up through ``LiveLobby.all_objects`` by
token only and re-compared with ``secrets.compare_digest``, rate limited like
the invite accept, parked in the session (never a signup or login URL) for an
anonymous visitor, never logged, scrubbed from Sentry (``TOKEN_PATH_PREFIXES``).
Registration is hooked (``consume_pending_live_join``), never forked.
"""
import secrets
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django_ratelimit.decorators import ratelimit

from . import flags
from .forms import LiveLobbyForm
from .managers import scope_to_active_club
from .models import LiveLobby, Player
from .services import cancel_lobby, open_lobby, start_shared_match

# Where a join token waits while an anonymous visitor logs in or signs up.
# Session, not a query parameter: the login `next` and the signup form must
# not carry the credential (clubs.md), and the signup form posts to itself.
PENDING_LIVE_JOIN_SESSION_KEY = "pending_live_join"

# How often live_lobby.js asks whether the opponent has joined.
POLL_INTERVAL_MS = 2000


def _player_or_none(user):
    try:
        return user.player
    except (AttributeError, Player.DoesNotExist):
        return None


def _host_lobby_or_404(request, pk):
    """The viewer's own lobby. Scoped manager (club + host), and pinned to the
    host even for staff, who bypass the user filter: the page shows the token."""
    player = _player_or_none(request.user)
    if player is None:
        raise Http404
    return get_object_or_404(LiveLobby.objects.filter(host=player), pk=pk)


def _lobby_state(lobby):
    """The status payload the lobby page polls for."""
    status = lobby.current_status
    payload = {"status": status}
    if status == LiveLobby.Status.JOINED and lobby.match_id:
        payload["match_url"] = reverse("pingpong:live_scoreboard", args=[lobby.match_id])
    elif status in (LiveLobby.Status.EXPIRED, LiveLobby.Status.CANCELLED):
        payload["reason"] = (
            "This code has expired." if status == LiveLobby.Status.EXPIRED
            else "This code was cancelled."
        )
    return payload


# ---------------------------------------------------------------------------
# Host side (KAN-151)
# ---------------------------------------------------------------------------


class LiveLobbyCreateView(LoginRequiredMixin, View):
    """POST from the match form's "Score live with a shared link" button."""

    def post(self, request):
        if not flags.is_enabled("shared_scoring", request.user):
            raise Http404
        host = _player_or_none(request.user)
        if host is None:
            messages.error(request, "You need a player profile to score live.")
            return redirect("pingpong:match_add")
        form = LiveLobbyForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Could not start that match. Check the match length.")
            return redirect("pingpong:match_add")
        if form.cleaned_data.get("is_double"):
            messages.error(request, "Shared scoring is for singles only, for now.")
            return redirect("pingpong:match_add")
        lobby = open_lobby(
            host,
            best_of=form.cleaned_data["best_of"],
            location=form.cleaned_data.get("location"),
            open_to_club=form.cleaned_data["open_to_club"],
        )
        return redirect("pingpong:live_lobby", pk=lobby.pk)


@method_decorator(never_cache, name="dispatch")
class LiveLobbyView(LoginRequiredMixin, View):
    """The host's lobby page: QR code, link, and waiting for the opponent.

    ``never_cache`` (no-store): the page carries a bearer token. With
    scripting off, "Check again" reloads it, and a joined lobby redirects to
    the scoreboard from here.
    """

    template_name = "pingpong/live_lobby.html"

    def get(self, request, pk):
        lobby = _host_lobby_or_404(request, pk)
        state = _lobby_state(lobby)
        if "match_url" in state:
            return redirect(state["match_url"])
        # A dead lobby's token is useless, but it is still not shown.
        join_url = (
            request.build_absolute_uri(reverse("pingpong:live_join", args=[lobby.token]))
            if state["status"] == LiveLobby.Status.OPEN
            else ""
        )
        return render(
            request,
            self.template_name,
            {
                "lobby": lobby,
                "status": state["status"],
                "reason": state.get("reason", ""),
                "join_url": join_url,
                "lobby_config": {
                    "status_url": reverse("pingpong:live_lobby_status", args=[lobby.pk]),
                    "join_url": join_url,
                    "interval_ms": POLL_INTERVAL_MS,
                    "share_title": "TTStats live match",
                    "share_text": "Score our match on TTStats",
                },
            },
        )


@method_decorator(never_cache, name="dispatch")
class LiveLobbyStatusView(LoginRequiredMixin, View):
    """``{status, match_url?, reason?}`` for live_lobby.js's poll."""

    def get(self, request, pk):
        lobby = _host_lobby_or_404(request, pk)
        return JsonResponse(_lobby_state(lobby))


class LiveLobbyCancelView(LoginRequiredMixin, View):
    """Cancel the lobby; with ``pick_opponent``, go on to the match form
    with the lobby's match length and location filled in."""

    def post(self, request, pk):
        lobby = _host_lobby_or_404(request, pk)
        cancel_lobby(lobby)
        if request.POST.get("pick_opponent"):
            params = {"best_of": lobby.best_of}
            if lobby.location_id:
                params["location"] = lobby.location_id
            return redirect(f"{reverse('pingpong:match_add')}?{urlencode(params)}")
        lobby.refresh_from_db(fields=["status", "match"])
        if lobby.status == LiveLobby.Status.JOINED and lobby.match_id:
            # The opponent got in first: the match exists, go and score it.
            return redirect("pingpong:live_scoreboard", pk=lobby.match_id)
        messages.info(request, "Cancelled. Nobody can join with that code now.")
        return redirect("pingpong:play")


# ---------------------------------------------------------------------------
# Join side (KAN-152)
# ---------------------------------------------------------------------------


def find_lobby(token):
    """Look a lobby up by its join token. None rather than raising.

    ``all_objects``: the joiner is not the host, so the scoped manager would
    hide it; the caller enforces the club rule (KAN-24 audit). Re-compared in
    constant time, as ``_find_invite`` does. The token is never logged.
    """
    if not token or len(token) > 64:
        return None
    lobby = (
        LiveLobby.all_objects.filter(token=token)
        .select_related("host", "club", "location")
        .first()
    )
    if lobby is None or not secrets.compare_digest(str(lobby.token), str(token)):
        return None
    return lobby


def _join_page(request, lobby, state, *, status=200, join_path=None, **extra):
    """``join_path`` is where the confirm form posts and "Not you?" returns:
    the token URL by default, the token-free club route from Today (KAN-157),
    which must never put the token on the page."""
    context = {"lobby": lobby, "state": state, **extra}
    if lobby is not None:
        context["join_path"] = join_path or reverse("pingpong:live_join", args=[lobby.token])
    return render(request, "pingpong/live_join.html", context, status=status)


def _resolve_join(request, lobby, join_path=None):
    """What this visitor sees for this lobby: a response, or None to mean
    "may join" (the confirm page on GET, the join itself on POST)."""
    if lobby is None:
        return _join_page(request, None, "invalid", status=404)

    user = request.user
    player = _player_or_none(user) if user.is_authenticated else None
    status = lobby.current_status

    if status == LiveLobby.Status.JOINED:
        # The rejoin path: whoever is on the match goes back to it.
        if player is not None and lobby.match_id and player.pk in _match_scorer_ids(lobby):
            return redirect("pingpong:live_scoreboard", pk=lobby.match_id)
        return _join_page(
            request, lobby, "ended", status=410, reason=lobby.ended_reason, join_path=join_path
        )
    if status != LiveLobby.Status.OPEN:
        return _join_page(
            request, lobby, "ended", status=410, reason=lobby.ended_reason, join_path=join_path
        )

    if not user.is_authenticated:
        request.session[PENDING_LIVE_JOIN_SESSION_KEY] = lobby.token
        return _join_page(
            request,
            lobby,
            "anonymous",
            login_url="{}?{}".format(
                resolve_url("login"),
                urlencode({"next": reverse("pingpong:live_join_resume")}),
            ),
        )

    if player is not None and player.pk == lobby.host_id:
        return _join_page(request, lobby, "own", join_path=join_path)
    if player is None or (
        lobby.club_id is not None and not lobby.club.user_is_member(user)
    ):
        return _join_page(request, lobby, "not_member", join_path=join_path)
    return None


def _match_scorer_ids(lobby):
    from .models import LiveScorer

    return set(
        LiveScorer.objects.filter(match_id=lobby.match_id, revoked_at__isnull=True)
        .values_list("player_id", flat=True)
    )


@method_decorator(never_cache, name="dispatch")
@method_decorator(
    # Like the club-invite accept: the token is guessable only by brute
    # force, and the GET is the oracle for "is it real". Per user when signed
    # in (a club night shares one address), per IP otherwise. GET only: an
    # anonymous POST is bounced to the GET without a lookup, and a signed-in
    # POST is a verified member's, already limited by their GETs.
    ratelimit(key="user_or_ip", rate="20/h", method="GET", block=True),
    name="dispatch",
)
class LiveJoinView(View):
    """Open a lobby's join link: who are you, then join."""

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST" and not request.user.is_authenticated:
            # Uncounted, so it must not answer: no lookup, no parked token.
            return redirect(request.path)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, token):
        lobby = find_lobby(token)
        response = _resolve_join(request, lobby)
        if response is not None:
            return response
        return _join_page(request, lobby, "confirm", player=request.user.player)

    def post(self, request, token):
        lobby = find_lobby(token)
        response = _resolve_join(request, lobby)
        if response is not None:
            return response
        match = start_shared_match(lobby, request.user.player, request)
        if match is None:
            # Lost the claim (another joiner, a cancel, the clock): show why.
            lobby.refresh_from_db()
            return _resolve_join(request, lobby)
        request.session.pop(PENDING_LIVE_JOIN_SESSION_KEY, None)
        messages.success(request, "You're in. Score your side.")
        return redirect("pingpong:live_scoreboard", pk=match.pk)


# ---------------------------------------------------------------------------
# Join from Today, without the token (KAN-157)
# ---------------------------------------------------------------------------

# How many waiting lobbies Today lists. Past this it is a list, not a card.
TODAY_LOBBY_LIMIT = 3


def _club_lobbies():
    """Open, unexpired, open-to-club lobbies of the viewer's active club.

    ``all_objects`` because the scoped manager shows a player only lobbies
    they host; ``scope_to_active_club`` puts the club filter back (KAN-24
    audit): staff or not, nobody sees another club's lobby.
    """
    from django.utils import timezone

    return scope_to_active_club(
        LiveLobby.all_objects.filter(
            status=LiveLobby.Status.OPEN,
            expires_at__gt=timezone.now(),
            open_to_club=True,
        )
    )


def lobbies_waiting_for(player):
    """The Today card: lobbies this player could join, newest first.

    Never the viewer's own. Read on every load, never cached: a lobby lives
    30 minutes and a cached list would show a dead one or hide a live one.
    """
    if player is None:
        return []
    return list(
        _club_lobbies()
        .exclude(host=player)
        .select_related("host", "location")
        .order_by("-created_at")[:TODAY_LOBBY_LIMIT]
    )


def _club_lobby_or_404(pk):
    """A lobby by pk for the Today join: in the viewer's active club and
    opened to it by the host. Any status, so a joined lobby can still send
    its players back to the scoreboard and a dead one can say why."""
    lobby = (
        scope_to_active_club(LiveLobby.all_objects.filter(pk=pk, open_to_club=True))
        .select_related("host", "club", "location")
        .first()
    )
    if lobby is None:
        raise Http404
    return lobby


@method_decorator(never_cache, name="dispatch")
class LiveLobbyClubJoinView(LoginRequiredMixin, View):
    """``live/lobby/<pk>/join/``: the Today card's Join (KAN-157).

    The same confirm page and the same ``start_shared_match`` as the token
    link; only the lookup differs. Membership of the lobby's club is the
    authorization (the active club is only ever one the viewer belongs to,
    and ``_resolve_join`` checks membership again), so it needs no token and
    no rate limit, and a lobby the host did not open to the club is a 404.
    """

    def get(self, request, pk):
        lobby = _club_lobby_or_404(pk)
        join_path = reverse("pingpong:live_lobby_join", args=[lobby.pk])
        response = _resolve_join(request, lobby, join_path)
        if response is not None:
            return response
        return _join_page(
            request, lobby, "confirm", player=request.user.player, join_path=join_path
        )

    def post(self, request, pk):
        lobby = _club_lobby_or_404(pk)
        join_path = reverse("pingpong:live_lobby_join", args=[lobby.pk])
        response = _resolve_join(request, lobby, join_path)
        if response is not None:
            return response
        match = start_shared_match(lobby, request.user.player, request)
        if match is None:
            lobby.refresh_from_db()
            return _resolve_join(request, lobby, join_path)
        messages.success(request, "You're in. Score your side.")
        return redirect("pingpong:live_scoreboard", pk=match.pk)


class LiveJoinResumeView(View):
    """Where login sends a joiner back to: the parked token's join page.

    The login ``next`` points here rather than at the join URL, so the token
    never appears in a login URL (or its access log line).
    """

    def get(self, request):
        token = request.session.get(PENDING_LIVE_JOIN_SESSION_KEY)
        if not token:
            return redirect("pingpong:dashboard")
        return redirect("pingpong:live_join", token=token)


def pending_live_join_url(request):
    """The resume URL if the session holds a parked token whose lobby is
    still open, else None (a dead token is dropped on the way).

    Where verification and login send a joiner when they carry no ``next`` of
    their own: the token-free ``live/join/`` route, never the join URL, so the
    token stays out of every URL but its own.
    """
    token = request.session.get(PENDING_LIVE_JOIN_SESSION_KEY)
    if not token:
        return None
    lobby = find_lobby(token)
    if lobby is None or not lobby.is_open:
        request.session.pop(PENDING_LIVE_JOIN_SESSION_KEY, None)
        return None
    return reverse("pingpong:live_join_resume")


def consume_pending_live_join(request, user):
    """The registration hook for a signup that came from a join link.

    Called from ``PlayerRegistrationView.form_valid`` after
    ``consume_pending_invite`` (so a club invite in the same session has
    already made them a member). Returns a response, or None to let
    registration carry on as usual (the "verify your email" page).

    **Never logs the new account in.** An unverified session could create
    matches against verified clubmates that auto-confirm, and register a
    passkey, bypassing verification for good. When the lobby is still open
    and the account is in its club, the token stays parked: verifying (in
    this browser) or the next login resumes the join via
    ``pending_live_join_url``. A dead lobby or the host's own drops it; a
    non-member gets the dead end at once.
    """
    token = request.session.get(PENDING_LIVE_JOIN_SESSION_KEY)
    if not token:
        return None
    lobby = find_lobby(token)
    player = _player_or_none(user)
    if lobby is None or not lobby.is_open or player is None or player.pk == lobby.host_id:
        request.session.pop(PENDING_LIVE_JOIN_SESSION_KEY, None)
        return None
    if lobby.club_id is not None and not lobby.club.user_is_member(user):
        request.session.pop(PENDING_LIVE_JOIN_SESSION_KEY, None)
        return _join_page(request, lobby, "not_member", email_sent_to=user.email)
    return None
