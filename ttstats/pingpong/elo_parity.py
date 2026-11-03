"""Elo parity harness (KAN-45 / Q4.8): prove the pipeline still matches today.

A regression in Elo does not raise or 500: the leaderboard just shows
different numbers, and nobody can tell which version was right. This harness
is the gate for every KAN-9 story. It replays the confirmed history (the one
traversal in `elo_replay`) through two pipelines at once:

(a) **the reference**: a frozen copy of the legacy arithmetic as it stood on
    2026-11-03, before KAN-9 touched `elo.py`. It is deliberately a *copy*:
    if it imported `elo` it would change whenever `elo` did and could never
    disagree with it.
(b) **the pipeline**: the live `elo.projected_elo_changes`, which is the only
    arithmetic the app uses for previews and writes.

It asserts identical final rating, peak and match count per player, and an
identical `rating_change` and `k_factor` for every player in every match.

Since KAN-47 the pipeline also writes the `PlayerRating` format scopes. For
players who have only ever played singles **in a component of the match graph
with no doubles match at all**, the all-time singles rating must equal the
legacy one. The component condition is necessary, not caution: a
singles-only player whose opponent also plays doubles meets that opponent's
*combined* legacy rating but their *singles* rating, so the two numbers
legitimately differ (docs/decisions/kan-9-seasons.md records this).
Optionally (`compare_stored_history`) it also compares the reference against
the stored `EloHistory` rows. Those can legitimately differ on real data
because live Elo is applied in *confirmation* order and replay uses *play*
order (see docs/decisions/kan-9-seasons.md).

If an intentional behaviour change is ever made, change the reference here in
the same commit and say so in the commit message. That is the explicit record
the ticket asks for.

Runs as `manage.py elo_parity [--inject-k-factor X]` and from pytest.
"""
from dataclasses import dataclass, field

from . import elo
from .elo_replay import DEFAULT_CHUNK_SIZE, Change, Replay, iter_replay_matches, load_context

# ---------------------------------------------------------------------------
# (a) The frozen reference. Do not "tidy" this into a call to elo.py.
# ---------------------------------------------------------------------------

_REF_BASE_K = 32
_REF_PROVISIONAL_MATCHES = 20
_REF_BEST_OF = {3: 0.9, 5: 1.0, 7: 1.1}


def _ref_k(match_type, best_of, matches_played):
    type_multiplier = 1.5 if match_type == "tournament" else 1.0
    best_of_multiplier = _REF_BEST_OF.get(best_of, 1.0)
    experience = 1.5 if matches_played < _REF_PROVISIONAL_MATCHES else 1.0
    return _REF_BASE_K * type_multiplier * best_of_multiplier * experience


def _ref_expected(rating_a, rating_b):
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def _ref_change(r1, r2, actual, k):
    return round(k * (actual - _ref_expected(r1, r2)))


class _ReferenceReplay:
    """Today's update_player_elo, on plain dicts: pk -> [rating, peak, matches]."""

    def __init__(self):
        self.state = {}

    def get(self, pk):
        return self.state.setdefault(pk, [1500, 1500, 0])

    def play(self, record):
        side1 = [self.get(pk) for pk in record.side1_ids]
        side2 = [self.get(pk) for pk in record.side2_ids]
        if not record.winner_side or not side1 or not side2:
            return []
        r1 = sum(s[0] for s in side1) / len(side1)
        r2 = sum(s[0] for s in side2) / len(side2)
        s1 = 1 if record.winner_side == 1 else 0
        k1 = [_ref_k(record.match_type, record.best_of, s[2]) for s in side1]
        k2 = [_ref_k(record.match_type, record.best_of, s[2]) for s in side2]
        k1, k2 = sum(k1) / len(k1), sum(k2) / len(k2)
        c1, c2 = _ref_change(r1, r2, s1, k1), _ref_change(r2, r1, 1 - s1, k2)

        changes = []
        for ids, delta, k in ((record.side1_ids, c1, k1), (record.side2_ids, c2, k2)):
            for pk in ids:
                s = self.get(pk)
                old = s[0]
                s[0] += delta
                s[1] = max(s[1], s[0])
                s[2] += 1
                changes.append(Change(pk, old, s[0], delta, k))
        return changes


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class PlayerDiff:
    player_id: int
    name: str
    scope: str
    field: str
    reference: object
    pipeline: object


@dataclass
class MatchDiff:
    match_id: int
    player_id: int
    field: str
    reference: object
    pipeline: object


@dataclass
class ParityReport:
    matches_replayed: int = 0
    players_compared: int = 0
    player_diffs: list = field(default_factory=list)
    match_diffs: list = field(default_factory=list)
    stored_history_diffs: list = field(default_factory=list)
    compared_stored_history: bool = False
    singles_only_checked: int = 0
    injected_k: object = None
    names: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not (self.player_diffs or self.match_diffs or self.stored_history_diffs)

    def render(self, max_match_rows=50):
        lines = []
        if self.injected_k is not None:
            lines.append(f"Injected divergence: pipeline BASE_K = {self.injected_k}")
        if self.player_diffs:
            lines.append("")
            lines.append("Per-player differences (reference = frozen legacy arithmetic):")
            lines.append(
                f"  {'player':<24} {'scope':<16} {'field':<8} "
                f"{'reference':>10} {'pipeline':>10} {'delta':>7}"
            )
            for d in self.player_diffs:
                lines.append(
                    f"  {d.name[:24]:<24} {d.scope:<16} {d.field:<8} "
                    f"{d.reference!s:>10} {d.pipeline!s:>10} "
                    f"{_delta(d.reference, d.pipeline):>7}"
                )
        for title, diffs in (
            ("Per-match differences", self.match_diffs),
            ("Stored EloHistory vs reference", self.stored_history_diffs),
        ):
            if not diffs:
                continue
            lines.append("")
            lines.append(f"{title} ({len(diffs)}):")
            lines.append(
                f"  {'match':>7} {'player':<24} {'field':<14} {'reference':>10} {'other':>10}"
            )
            for d in diffs[:max_match_rows]:
                name = self.names.get(d.player_id, f"#{d.player_id}")
                lines.append(
                    f"  {d.match_id:>7} {name[:24]:<24} {d.field:<14} "
                    f"{_fmt(d.reference):>10} {_fmt(d.pipeline):>10}"
                )
            if len(diffs) > max_match_rows:
                lines.append(f"  ... and {len(diffs) - max_match_rows} more")
        lines.append("")
        stored = (
            f", {len(self.stored_history_diffs)} stored-history diffs"
            if self.compared_stored_history else ""
        )
        lines.append(
            f"Elo parity {'PASS' if self.ok else 'FAIL'}: "
            f"{self.matches_replayed} matches replayed, "
            f"{self.players_compared} players compared, "
            f"{len(self.player_diffs)} player diffs, "
            f"{len(self.match_diffs)} per-match diffs, "
            f"{self.singles_only_checked} singles-only players checked{stored}"
        )
        return "\n".join(lines)


def _fmt(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def _delta(a, b):
    try:
        return f"{b - a:+g}"
    except TypeError:
        return ""


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

_CHANGE_FIELDS = ("old", "new", "change", "k")
_FIELD_NAMES = {"old": "old_rating", "new": "new_rating", "change": "rating_change", "k": "k_factor"}


def _compare_changes(match_id, reference, other, out):
    ref_by_player = {c.player_id: c for c in reference}
    other_by_player = {c.player_id: c for c in other}
    for pk in sorted(set(ref_by_player) | set(other_by_player)):
        ref, oth = ref_by_player.get(pk), other_by_player.get(pk)
        if ref is None or oth is None:
            out.append(MatchDiff(match_id, pk, "row", "present" if ref else "missing",
                                 "present" if oth else "missing"))
            continue
        for attr in _CHANGE_FIELDS:
            if getattr(ref, attr) != getattr(oth, attr):
                out.append(MatchDiff(match_id, pk, _FIELD_NAMES[attr],
                                     getattr(ref, attr), getattr(oth, attr)))


def _stored_history():
    from .models import EloHistory

    stored = {}
    for match_id, pk, old, new, change, k in EloHistory._base_manager.values_list(
        "match_id", "player_id", "old_rating", "new_rating", "rating_change", "k_factor"
    ).iterator(chunk_size=DEFAULT_CHUNK_SIZE):
        stored.setdefault(match_id, []).append(Change(pk, old, new, change, k))
    return stored


def run_parity(*, inject_k_factor=None, compare_stored_history=False,
               chunk_size=DEFAULT_CHUNK_SIZE):
    """Replay history through both pipelines and return a `ParityReport`.

    Reads only. `inject_k_factor` is passed to the *pipeline's* replay as its
    base K (an explicit parameter; nothing global is patched), which must make
    the harness fail; that is how it proves it can.
    """
    from .models import Player

    report = ParityReport(
        injected_k=inject_k_factor, compared_stored_history=compare_stored_history
    )
    reference = _ReferenceReplay()
    pipeline = Replay(*load_context(), base_k=inject_k_factor)
    components = _Components()
    stored = _stored_history() if compare_stored_history else {}

    for record in iter_replay_matches(chunk_size=chunk_size):
        ref_changes = reference.play(record)
        new_changes, _ = pipeline.play(record)
        components.add(record)
        report.matches_replayed += 1
        _compare_changes(record.pk, ref_changes, new_changes, report.match_diffs)
        if compare_stored_history:
            _compare_changes(record.pk, ref_changes, stored.pop(record.pk, []),
                             report.stored_history_diffs)

    # EloHistory for a match outside the replay set (unconfirmed, no winner)
    # is itself a divergence: live Elo was applied to something replay skips.
    for match_id, rows in stored.items():
        _compare_changes(match_id, [], rows, report.stored_history_diffs)

    report.names = dict(Player._base_manager.values_list("pk", "name"))
    report.players_compared = len(report.names)
    for pk, name in sorted(report.names.items(), key=lambda kv: (kv[1], kv[0])):
        ref = reference.state.get(pk, [1500, 1500, 0])
        p = pipeline.legacy.players.get(pk)
        new = [p.elo_rating, p.elo_peak, p.matches_for_elo] if p else [1500, 1500, 0]
        for label, a, b in zip(("rating", "peak", "matches"), ref, new):
            if a != b:
                report.player_diffs.append(PlayerDiff(pk, name, "legacy", label, a, b))

        # The singles-only invariant (KAN-47): all-time singles == legacy.
        if p is None or not components.singles_only(pk):
            continue
        report.singles_only_checked += 1
        row = pipeline.book.row(pk, None, elo.SINGLES)
        singles = [row.rating, row.peak, row.matches] if row else [1500, 1500, 0]
        for label, a, b in zip(("rating", "peak", "matches"), new, singles):
            if a != b:
                report.player_diffs.append(
                    PlayerDiff(pk, name, "singles all-time", label, a, b)
                )
    return report


class _Components:
    """Union-find over players who met in a match, remembering doubles."""

    def __init__(self):
        self.parent = {}
        self.has_doubles = set()

    def find(self, pk):
        self.parent.setdefault(pk, pk)
        while self.parent[pk] != pk:
            self.parent[pk] = self.parent[self.parent[pk]]
            pk = self.parent[pk]
        return pk

    def add(self, record):
        ids = list(record.side1_ids) + list(record.side2_ids)
        if not ids:
            return
        roots = {self.find(pk) for pk in ids}
        root = roots.pop()
        doubles = record.is_double or root in self.has_doubles
        for other in roots:
            self.parent[other] = root
            doubles = doubles or other in self.has_doubles
            self.has_doubles.discard(other)
        if doubles:
            self.has_doubles.add(root)

    def singles_only(self, pk):
        return pk in self.parent and self.find(pk) not in self.has_doubles

