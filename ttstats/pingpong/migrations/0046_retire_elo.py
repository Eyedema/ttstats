"""KAN-73: retire Elo.

* `Player.elo_rating` / `elo_peak` / `matches_for_elo` and `PlayerRating.rating`
  / `peak` are dropped. They were derived values (current Elo), recomputable
  from the frozen history below, and a stale "current Elo" would keep teaching
  the wrong model.
* `RatingChange`'s Elo columns are the per-format Elo audit trail. They are
  **renamed, not dropped** (`elo_*`) and made nullable: no historical row is
  rewritten, and new rows leave them NULL.
* `EloHistory` is untouched (frozen; docs/decisions/kan-63-cutover-plan.md, OQ6).
* The leaderboard index now orders by `glicko_rating`.
* Giant Slayer / Peak Performer descriptions lose the word "Elo".
* Overrides of the retired `seasons` / `glicko2` flags are deleted.

Irreversible in data terms: reversing re-adds the dropped columns at their
defaults (1500 / 0), not their last values, and fills post-KAN-73 rows' Elo
columns with a neutral placeholder. Restore from the pre-KAN-73 dump instead.
"""
from django.db import migrations, models

# Achievement copy without the retired system's name (it used to be swapped at
# render time by the `neutral_rating_copy` filter).
NEW_COPY = {
    "giant_slayer_bronze": ("Beat someone rated 100+ Elo above you", "Beat someone rated 100+ points above you"),
    "giant_slayer_silver": ("Beat someone rated 200+ Elo above you", "Beat someone rated 200+ points above you"),
    "giant_slayer_gold": ("Beat someone rated 300+ Elo above you", "Beat someone rated 300+ points above you"),
    "peak_performer": ("Reach a new personal Elo peak", "Reach a new personal rating peak"),
}


def reword_achievements(apps, schema_editor):
    Achievement = apps.get_model("pingpong", "Achievement")
    for slug, (_, new) in NEW_COPY.items():
        Achievement.objects.filter(slug=slug).update(description=new)


def unreword_achievements(apps, schema_editor):
    Achievement = apps.get_model("pingpong", "Achievement")
    for slug, (old, _) in NEW_COPY.items():
        Achievement.objects.filter(slug=slug).update(description=old)


def fill_elo_placeholders(apps, schema_editor):
    """Reverse only: rows written after KAN-73 have NULL `elo_*`, which the
    pre-0046 NOT NULL columns would reject. Fill a neutral placeholder (1500,
    change 0, K 0) -- it never was an Elo result. A real rollback restores the
    pre-KAN-73 dump instead."""
    apps.get_model("pingpong", "RatingChange").objects.filter(elo_old_rating__isnull=True).update(
        elo_old_rating=1500, elo_new_rating=1500, elo_change=0, elo_k_factor=0.0
    )


def drop_retired_flag_overrides(apps, schema_editor):
    """Per-user overrides of the retired `seasons` / `glicko2` flags mean nothing now."""
    apps.get_model("pingpong", "FeatureFlagOverride").objects.filter(
        name__in=["seasons", "glicko2"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pingpong", "0045_ratingchange_glicko_history"),
    ]

    operations = [
        # RatingChange: keep the Elo history under honest names.
        migrations.RenameField("ratingchange", "old_rating", "elo_old_rating"),
        migrations.RenameField("ratingchange", "new_rating", "elo_new_rating"),
        migrations.RenameField("ratingchange", "rating_change", "elo_change"),
        migrations.RenameField("ratingchange", "k_factor", "elo_k_factor"),
        migrations.AlterField(
            model_name="ratingchange",
            name="elo_old_rating",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="ratingchange",
            name="elo_new_rating",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="ratingchange",
            name="elo_change",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="ratingchange",
            name="elo_k_factor",
            field=models.FloatField(blank=True, null=True),
        ),
        # Forward: nothing. Reverse (runs before the AlterFields above are
        # undone): fill the NULLs the restored NOT NULL columns would reject.
        migrations.RunPython(migrations.RunPython.noop, fill_elo_placeholders),
        # PlayerRating: the index first, then the derived Elo columns.
        migrations.RemoveIndex(model_name="playerrating", name="playerrating_leaderboard"),
        migrations.RemoveField(model_name="playerrating", name="rating"),
        migrations.RemoveField(model_name="playerrating", name="peak"),
        migrations.AddIndex(
            model_name="playerrating",
            index=models.Index(
                fields=["club", "season", "format", "-glicko_rating"],
                name="playerrating_leaderboard",
            ),
        ),
        # Player: the lifetime combined Elo mirror.
        migrations.RemoveField(model_name="player", name="elo_rating"),
        migrations.RemoveField(model_name="player", name="elo_peak"),
        migrations.RemoveField(model_name="player", name="matches_for_elo"),
        migrations.AlterField(
            model_name="seasonstanding",
            name="rating_system",
            field=models.CharField(default="glicko2", max_length=10),
        ),
        migrations.RunPython(reword_achievements, unreword_achievements),
        migrations.RunPython(drop_retired_flag_overrides, migrations.RunPython.noop),
    ]
