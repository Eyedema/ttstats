"""Divergence harness (KAN-66 / Q6.3): how does Glicko-2 differ from Elo on real history?

Read-only and in memory. It walks the confirmed history with the one shared
traversal (`elo_replay.iter_replay_matches`, driving a `Replay` with the Glicko
shadow switched on) and answers two questions per format:

* **Prediction:** before each match, how well did each system predict the
  winner? Brier score, log-loss and favourite hit rate are accumulated online
  (O(players) memory, never O(matches)), for all matches and for matches where
  both sides were already established (>= `elo.PROVISIONAL_MATCHES` prior
  matches in the format).
* **Ranking:** at the end, how differently do the two systems order the
  players? Per-player table, Kendall tau-b, biggest rank movers, and the
  distribution of the final RD.

Rateable set: exactly what `elo_replay.confirmed_history` yields (a winner, a
confirmed result, not live), in replay order `(date_played, created_at, pk)`.
The harness never walks history any other way, so it cannot drift from the
replay that `recalculate_elo` uses.

RD at the end is inflated to the history's LAST match date, never `now()`, so
two runs on the same data print the same bytes. Doubles have no Glicko shadow
yet (KAN-70): they report Elo prediction only.

Parameters are the PLACEHOLDER defaults unless overridden; this harness is the
instrument KAN-68 uses to fit them.
"""

import math
import statistics
from dataclasses import dataclass, field

from . import elo, glicko2, glicko_adapter
from .elo_replay import (
    DEFAULT_CHUNK_SIZE,
    Replay,
    confirmed_history,
    iter_replay_matches,
    load_context,
)
from .glicko_adapter import DEFAULT_PARAMS

LOG_EPS = 1e-15
RD_BIN_EDGES = (0, 50, 100, 150, 200, 250, 300, 350)
DEFAULT_TINY_RD = 300.0
DEFAULT_TOP = 10
MODELS = ("elo", "glicko")
SUBSETS = ("all", "established")


# ---------------------------------------------------------------------------
# Pure statistics (no Django, no DB)
# ---------------------------------------------------------------------------


@dataclass
class PredictionStats:
    """Online accumulator of pre-match predictions that side 1 wins."""

    n: int = 0
    non_tie: int = 0
    hits: int = 0
    brier_sum: float = 0.0
    log_loss_sum: float = 0.0

    def add(self, p_side1, side1_won):
        outcome = 1.0 if side1_won else 0.0
        self.n += 1
        self.brier_sum += (p_side1 - outcome) ** 2
        p_winner = p_side1 if side1_won else 1.0 - p_side1
        self.log_loss_sum += -math.log(min(max(p_winner, LOG_EPS), 1.0 - LOG_EPS))
        if p_side1 != 0.5:  # an exact tie has no favourite
            self.non_tie += 1
            if (p_side1 > 0.5) == bool(side1_won):
                self.hits += 1

    @property
    def brier(self):
        return self.brier_sum / self.n if self.n else None

    @property
    def log_loss(self):
        return self.log_loss_sum / self.n if self.n else None

    @property
    def favourite_rate(self):
        return self.hits / self.non_tie if self.non_tie else None

    def as_dict(self):
        return {
            "n": self.n,
            "non_tie": self.non_tie,
            "favourite_hits": self.hits,
            "favourite_rate": self.favourite_rate,
            "brier": self.brier,
            "log_loss": self.log_loss,
        }


def kendall_tau_b(xs, ys):
    """Kendall tau-b of two equal-length value lists (ties handled); None if undefined.

    O(n^2) pairs, fine for a club's worth of players. Pure Python: no scipy.
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError("xs and ys must have the same length")
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx, dy = xs[i] - xs[j], ys[i] - ys[j]
            if dx == 0 and dy == 0:
                continue  # tied in both: in neither tie count of tau-b
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    denom = math.sqrt(
        (concordant + discordant + ties_x) * (concordant + discordant + ties_y)
    )
    if denom == 0:
        return None
    return (concordant - discordant) / denom


def ordinal_ranks(values, ids):
    """Rank 1 = highest value; ties broken by id so a rank is always unique."""
    order = sorted(range(len(values)), key=lambda i: (-values[i], ids[i]))
    ranks = [0] * len(values)
    for position, index in enumerate(order, 1):
        ranks[index] = position
    return ranks


def rd_histogram(rds, edges=RD_BIN_EDGES):
    """Counts per [edge, next_edge) bin; the last bin includes its upper edge."""
    bins = [0] * (len(edges) - 1)
    for rd in rds:
        for i in range(len(bins)):
            last = i == len(bins) - 1
            if edges[i] <= rd < edges[i + 1] or (last and rd == edges[i + 1]):
                bins[i] += 1
                break
        else:
            if rd > edges[-1]:
                bins[-1] += 1
    return [
        {"low": edges[i], "high": edges[i + 1], "count": count}
        for i, count in enumerate(bins)
    ]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class PlayerRow:
    player_id: int
    name: str
    matches: int
    last_match: object
    elo: int
    glicko: float = None
    rd: float = None
    diff: float = None
    elo_rank: int = None
    glicko_rank: int = None
    rank_change: int = None

    def as_dict(self):
        return {
            "player_id": self.player_id,
            "name": self.name,
            "matches": self.matches,
            "last_match": self.last_match.isoformat() if self.last_match else None,
            "elo": self.elo,
            "glicko": self.glicko,
            "rd": self.rd,
            "diff": self.diff,
            "elo_rank": self.elo_rank,
            "glicko_rank": self.glicko_rank,
            "rank_change": self.rank_change,
        }


@dataclass
class FormatReport:
    format: str
    glicko_rated: bool
    matches: int = 0
    prediction: dict = field(default_factory=dict)  # model -> subset -> PredictionStats
    players: list = field(default_factory=list)  # comparable PlayerRows, by Glicko rank
    not_comparable: list = field(default_factory=list)
    tau: float = None
    movers: list = field(default_factory=list)
    rd_summary: dict = None

    def as_dict(self):
        return {
            "format": self.format,
            "glicko_rated": self.glicko_rated,
            "matches": self.matches,
            "prediction": {
                model: {subset: stats.as_dict() for subset, stats in subsets.items()}
                for model, subsets in self.prediction.items()
            },
            "kendall_tau_b": self.tau,
            "players": [p.as_dict() for p in self.players],
            "movers": [p.as_dict() for p in self.movers],
            "not_comparable": [p.as_dict() for p in self.not_comparable],
            "rd": self.rd_summary,
        }


def _fmt(value, spec=".4f"):
    return "n/a" if value is None else format(value, spec)


@dataclass
class DivergenceReport:
    params: object
    tiny_rd: float
    top: int
    history_end: object
    rated_matches: int
    formats: list = field(default_factory=list)

    def as_dict(self):
        return {
            "params": dict(self.params._asdict()),
            "tiny_rd": self.tiny_rd,
            "history_end": self.history_end.isoformat() if self.history_end else None,
            "rated_matches": self.rated_matches,
            "formats": [f.as_dict() for f in self.formats],
        }

    def render(self):
        p = self.params
        lines = [
            "Elo vs Glicko-2 divergence (KAN-66)",
            (
                "Rateable set: confirmed matches with a winner, not live "
                "(elo_replay.confirmed_history), replayed in (date_played, created_at, pk) order."
            ),
            (
                f"Params: tau={p.tau} period_days={p.period_days} "
                f"season_rd_floor={p.season_rd_floor} (placeholders until KAN-68)"
            ),
            (
                f"Rated matches: {self.rated_matches}; history ends {self.history_end or 'n/a'} "
                "(RD is inflated to that date, never to now)."
            ),
        ]
        for fr in self.formats:
            lines += ["", "=" * 72, f"{fr.format.upper()}: {fr.matches} matches"]
            lines += self._render_prediction(fr)
            if not fr.glicko_rated:
                lines.append(
                    "Ranking: not rated under Glicko-2 yet (KAN-70); no comparison."
                )
                continue
            lines += self._render_ranking(fr)
        return "\n".join(lines)

    def _render_prediction(self, fr):
        lines = [
            "Pre-match prediction (side 1 wins; favourite rate excludes exact 0.5 ties):",
            f"  {'model':<8}{'subset':<13}{'n':>6}{'fav rate':>10}{'Brier':>9}{'log-loss':>10}",
        ]
        for model in MODELS:
            if model == "glicko" and not fr.glicko_rated:
                lines.append(f"  {model:<8}not rated under Glicko-2 yet")
                continue
            for subset in SUBSETS:
                s = fr.prediction[model][subset]
                lines.append(
                    f"  {model:<8}{subset:<13}{s.n:>6}{_fmt(s.favourite_rate, '.3f'):>10}"
                    f"{_fmt(s.brier):>9}{_fmt(s.log_loss):>10}"
                )
        return lines

    def _render_ranking(self, fr):
        lines = [
            "",
            (
                f"Players compared: {len(fr.players)}; Kendall tau-b (Elo vs Glicko-2): "
                f"{_fmt(fr.tau, '.4f')}"
            ),
            "(d# = Elo rank - Glicko rank: positive means Glicko-2 ranks the player higher.)",
        ]
        header = (
            f"  {'name':<22}{'n':>5} {'last':<11}{'Elo':>6}{'Glicko':>8}{'RD':>6}"
            f"{'diff':>7}{'E#':>4}{'G#':>4}{'d#':>4}"
        )
        lines.append(header)
        lines += [self._row_line(r) for r in fr.players]
        lines += [
            "",
            f"Biggest movers (|rank change|, then |diff|), top {len(fr.movers)}:",
        ]
        lines += [self._row_line(r) for r in fr.movers] or ["  (none)"]
        rd = fr.rd_summary
        if rd:
            lines += [
                "",
                (
                    f"End RD (all rated players): min {rd['min']:.1f}  "
                    f"median {rd['median']:.1f}  max {rd['max']:.1f}"
                ),
            ]
            lines += [
                f"  {b['low']:>3}-{b['high']:<3} {b['count']:>4} " + "#" * b["count"]
                for b in rd["histogram"]
            ]
        lines += [
            "",
            f"Not comparable (end RD >= {self.tiny_rd:g}): {len(fr.not_comparable)}",
        ]
        lines += [self._row_line(r) for r in fr.not_comparable] or ["  (none)"]
        return lines

    @staticmethod
    def _row_line(r):
        return (
            f"  {r.name[:21]:<22}{r.matches:>5} {r.last_match or '-'!s:<11}"
            f"{r.elo:>6}{_fmt(r.glicko, '.0f'):>8}{_fmt(r.rd, '.0f'):>6}"
            f"{_fmt(r.diff, '+.0f'):>7}{_fmt(r.elo_rank, 'd'):>4}"
            f"{_fmt(r.glicko_rank, 'd'):>4}{_fmt(r.rank_change, '+d'):>4}"
        )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _blank_stats():
    return {m: {s: PredictionStats() for s in SUBSETS} for m in MODELS}


def _match_day(record):
    day = record.date_played
    return day.date() if hasattr(day, "hour") else day


def _predict(replay, record, fmt, params, match_day):
    """((elo p, glicko p or None), established) for side 1, before the match is played."""
    scope = elo.RatingScope(None, fmt)
    side1, side2 = record.side1_ids, record.side2_ids
    readings1 = [elo.scope_reading(replay.book, pk, scope) for pk in side1]
    readings2 = [elo.scope_reading(replay.book, pk, scope) for pk in side2]
    r1 = sum(r for r, _ in readings1) / len(readings1)
    r2 = sum(r for r, _ in readings2) / len(readings2)
    p_elo = elo.calculate_expected_score(r1, r2)
    established = all(
        exp >= elo.PROVISIONAL_MATCHES for _, exp in readings1 + readings2
    )
    p_glicko = None
    if fmt == elo.SINGLES:
        g1 = glicko_adapter.glicko_reading(
            replay.book, side1[0], scope, match_day, params
        )
        g2 = glicko_adapter.glicko_reading(
            replay.book, side2[0], scope, match_day, params
        )
        p_glicko = glicko2.win_probability(g1.rating, g1.rd, g2.rating, g2.rd)
    return (p_elo, p_glicko), established


def run_divergence(
    *,
    params=DEFAULT_PARAMS,
    chunk_size=DEFAULT_CHUNK_SIZE,
    tiny_rd=DEFAULT_TINY_RD,
    top=DEFAULT_TOP,
):
    """Replay the confirmed history once and return a `DivergenceReport`."""
    from .models import Match, Player

    replay = Replay(*load_context(), glicko=params)
    stats = {elo.SINGLES: _blank_stats(), elo.DOUBLES: _blank_stats()}
    counts = {elo.SINGLES: 0, elo.DOUBLES: 0}
    last_played = {}  # (format, player_id) -> date
    history_end = None
    seen = 0

    for record in iter_replay_matches(chunk_size=chunk_size):
        # confirmed_history guarantees a winner; assert it so a future change
        # to the traversal cannot silently widen the rateable set.
        assert record.winner_side, "iter_replay_matches yielded an unrated match"
        seen += 1
        day = _match_day(record)
        if history_end is None or day > history_end:
            history_end = day
        fmt = elo.DOUBLES if record.is_double else elo.SINGLES
        rated = bool(record.side1_ids and record.side2_ids)
        if fmt == elo.SINGLES and not (
            len(record.side1_ids) == 1 == len(record.side2_ids)
        ):
            rated = False

        if rated:
            (p_elo, p_glicko), established = _predict(replay, record, fmt, params, day)
            side1_won = record.winner_side == 1
            for model, p in (("elo", p_elo), ("glicko", p_glicko)):
                if p is None:
                    continue
                stats[fmt][model]["all"].add(p, side1_won)
                if established:
                    stats[fmt][model]["established"].add(p, side1_won)
            counts[fmt] += 1
            for pk in record.side1_ids + record.side2_ids:
                last_played[(fmt, pk)] = max(day, last_played.get((fmt, pk), day))
        replay.play(record)

    assert seen == confirmed_history(Match._base_manager).count(), (
        "the harness saw a different match set than elo_replay.confirmed_history"
    )

    names = dict(Player._base_manager.values_list("pk", "name"))
    report = DivergenceReport(
        params=params,
        tiny_rd=tiny_rd,
        top=top,
        history_end=history_end,
        rated_matches=seen,
    )
    for fmt in (elo.SINGLES, elo.DOUBLES):
        fr = FormatReport(
            format=fmt,
            glicko_rated=fmt == elo.SINGLES,
            matches=counts[fmt],
            prediction=stats[fmt],
        )
        _fill_ranking(
            fr, replay.book, fmt, names, last_played, params, history_end, tiny_rd, top
        )
        report.formats.append(fr)
    return report


def _end_rating(state, history_end, params):
    """The stored Glicko state inflated to the history's last match day (not now)."""
    stored = glicko2.Rating(state.glicko_rating, state.rd, state.volatility)
    periods = glicko_adapter.days_to_periods(state.last_rated_on, history_end, params)
    return glicko2.inflate(stored, periods, rd_max=params.rd_max)


def _fill_ranking(fr, book, fmt, names, last_played, params, history_end, tiny_rd, top):
    rows = sorted(
        (
            r
            for (pid, season_id, f), r in book.rows.items()
            if season_id is None and f == fmt
        ),
        key=lambda r: r.player_id,
    )
    everyone = []
    for state in rows:
        row = PlayerRow(
            player_id=state.player_id,
            name=names.get(state.player_id, f"#{state.player_id}"),
            matches=state.matches,
            last_match=last_played.get((fmt, state.player_id)),
            elo=state.rating,
        )
        if fr.glicko_rated and state.glicko_rating is not None:
            end = _end_rating(state, history_end, params)
            row.glicko, row.rd = end.rating, end.rd
            row.diff = end.rating - state.rating
        everyone.append(row)

    if not fr.glicko_rated:
        fr.players = sorted(everyone, key=lambda r: (-r.elo, r.player_id))
        return

    rated = [r for r in everyone if r.glicko is not None]
    rds = [r.rd for r in rated]
    if rds:
        fr.rd_summary = {
            "min": min(rds),
            "median": statistics.median(rds),
            "max": max(rds),
            "histogram": rd_histogram(rds),
        }
    fr.not_comparable = [r for r in rated if r.rd >= tiny_rd]
    comparable = [r for r in rated if r.rd < tiny_rd]
    ids = [r.player_id for r in comparable]
    elo_ranks = ordinal_ranks([r.elo for r in comparable], ids)
    glicko_ranks = ordinal_ranks([r.glicko for r in comparable], ids)
    for r, er, gr in zip(comparable, elo_ranks, glicko_ranks):
        r.elo_rank, r.glicko_rank, r.rank_change = er, gr, er - gr
    fr.tau = kendall_tau_b([r.elo for r in comparable], [r.glicko for r in comparable])
    fr.players = sorted(comparable, key=lambda r: r.glicko_rank)
    fr.movers = sorted(
        comparable, key=lambda r: (-abs(r.rank_change), -abs(r.diff), r.player_id)
    )[:top]
