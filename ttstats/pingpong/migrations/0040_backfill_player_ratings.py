"""Backfill PlayerRating/RatingChange by replaying confirmed history (KAN-47).

FROZEN AS A NO-OP by KAN-73. This migration used to call the live replay
(`elo_replay.rebuild_player_ratings`) against the historical 0039 models. The
Elo arithmetic it replayed is gone, and the live code no longer fits the 0039
schema, so it cannot run any more.

That is safe:
* every database with history that we run (prod, the Supabase clone) applied
  it before KAN-73, and migrations never re-run;
* a from-scratch database has no confirmed matches, so the backfill had
  nothing to do.

**A database with history first migrated past 0040 after KAN-73** (an old
dump, say) gets no rating rows here: run `manage.py recalculate_ratings`
right after `migrate`. Until then its old matches are guarded against being
rated twice by their `EloHistory` (`rating_pipeline.already_applied`).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("pingpong", "0039_player_rating"),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
