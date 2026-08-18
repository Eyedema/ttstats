"""Player identity hues.

Every player owns one hue, permanently. It is not decoration: the design uses
it as the primary "who" signal -- a 4px bar on a confirmation row, the fill of
a scoreboard half, the rank marker on the leaderboard -- so that a rivalry is
legible before any name is read. A player whose colour drifted between screens
would be worse than no colour at all.

Pure module, no Django imports, so the assignment is testable without a DB.

The palette lives here rather than in tailwind.config.js because a hue is per
record, not per class name: Tailwind cannot generate `bg-player-<pk>`. The eight
entries are emitted as `.hue-1` .. `.hue-8` in app.css, each setting `--hue` and
`--hue-ink`; templates then use the presentational `.hue-bar` / `.hue-fill` /
`.hue-text` on top. That keeps hue out of inline style attributes entirely.
"""

# (name, hue, ink) -- ink is the text colour that sits on a solid fill of that
# hue. Chosen per entry rather than computed: the yellow and the sky blue need
# near-black, the red and the purple need white, and a luminance threshold gets
# orange wrong.
HUES = (
    ("sky", "#38bdf8", "#04121c"),
    ("red", "#ef4444", "#ffffff"),
    ("green", "#22c55e", "#04140a"),
    ("orange", "#f97316", "#1a0a00"),
    ("pink", "#ec4899", "#ffffff"),
    ("teal", "#14b8a6", "#04140f"),
    ("yellow", "#eab308", "#16130a"),
    ("purple", "#a855f7", "#ffffff"),
)

HUE_COUNT = len(HUES)


def hue_index(pk):
    """1-based palette slot for a player id.

    Keyed on the primary key so a player's hue survives a rename, and so two
    people never swap colours when someone else is deleted. `None` (a player
    who no longer exists -- MatchParticipant.player cascades) gets slot 1
    rather than raising; an absent player still has to render.
    """
    if not pk:
        return 1
    return (int(pk) - 1) % HUE_COUNT + 1


def hue_class(pk):
    """CSS class carrying this player's `--hue` / `--hue-ink` pair."""
    return f"hue-{hue_index(pk)}"


def hue_hex(pk):
    """The hue itself, for the few places that need a raw value (Chart.js)."""
    return HUES[hue_index(pk) - 1][1]


def hue_ink(pk):
    """Text colour that sits legibly on a solid fill of this player's hue."""
    return HUES[hue_index(pk) - 1][2]
