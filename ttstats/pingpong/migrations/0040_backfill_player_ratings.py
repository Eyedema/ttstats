"""Backfill PlayerRating/RatingChange by replaying confirmed history (KAN-47).

Deterministic: the replay order is `date_played, created_at, pk` and the
arithmetic is `elo.projected_elo_changes`, through the same traversal
`recalculate_elo` and the parity harness use (`elo_replay`). Batched, so it
never materialises the whole history.

Idempotent: forward deletes every PlayerRating/RatingChange row and rebuilds,
so running it twice gives the same rows. Reverse deletes them, leaving the
legacy `Player.elo_*` fields and `EloHistory` untouched -- they are never
written here.

This imports app code (`elo_replay`, `elo`) on purpose: the backfill's job is
to produce exactly what the live pipeline would, and a frozen copy of the
arithmetic here would be a second source of truth. Re-running it after the
arithmetic changes produces what `recalculate_elo` would.
"""
from django.db import migrations


def _models(apps):
    return dict(
        match=apps.get_model("pingpong", "Match"),
        participant=apps.get_model("pingpong", "MatchParticipant"),
        season=apps.get_model("pingpong", "Season"),
        player=apps.get_model("pingpong", "Player"),
        rating=apps.get_model("pingpong", "PlayerRating"),
        change=apps.get_model("pingpong", "RatingChange"),
    )


def backfill(apps, schema_editor):
    from pingpong.elo_replay import rebuild_player_ratings

    rebuild_player_ratings(models=_models(apps))


def unbackfill(apps, schema_editor):
    models = _models(apps)
    models["change"].objects.all().delete()
    models["rating"].objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pingpong", "0039_player_rating"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
