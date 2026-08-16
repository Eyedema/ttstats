"""Pure round-robin scheduling.

The circle method is list manipulation with no database in it, so it lives
here and is unit-tested directly rather than through generated ScheduledMatch
rows -- same discipline as ``live_scoring`` and ``match_state``.
"""
from __future__ import annotations

__all__ = ["round_robin_rounds", "round_robin_double_rounds"]


def round_robin_rounds(competitors):
    """One full single round-robin, as ``[(round_number, [(a, b), ...]), ...]``.

    Uses the circle method: fix the first competitor, rotate the rest. With an
    odd count a bye is added, so one competitor sits out each round.
    Fewer than two competitors yields no rounds.
    """
    competitors = list(competitors)
    if len(competitors) < 2:
        return []

    slots = list(competitors)
    if len(slots) % 2 == 1:
        slots.append(None)  # bye

    count = len(slots)
    rounds = []

    for round_index in range(count - 1):
        pairings = [
            (slots[i], slots[count - 1 - i])
            for i in range(count // 2)
            if slots[i] is not None and slots[count - 1 - i] is not None
        ]
        rounds.append((round_index + 1, pairings))

        # Rotate everything except the first slot.
        slots = [slots[0]] + [slots[-1]] + slots[1:-1]

    return rounds


def round_robin_double_rounds(competitors):
    """Home and away legs (andata e ritorno).

    The second leg repeats the first with home/away swapped, and its round
    numbers continue from the first leg.
    """
    first_leg = round_robin_rounds(competitors)
    if not first_leg:
        return []

    leg_length = len(first_leg)
    second_leg = [
        (number + leg_length, [(b, a) for a, b in pairings])
        for number, pairings in first_leg
    ]
    return first_leg + second_leg
