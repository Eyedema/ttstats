import django.db.models.deletion
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('pingpong', '0009_match_winner_confirmations'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='match',
            name='player1',
        ),
        migrations.RemoveField(
            model_name='match',
            name='player2',
        ),
        migrations.RenameField(
            model_name='game',
            old_name='winner',
            new_name='old_winner'
        ),
        migrations.AddField(
            model_name='game',
            name='winner',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="games_won",
                to="pingpong.team"
            )
        ),
        migrations.RenameField(
            model_name='game',
            old_name='player1_score',
            new_name='team1_score'
        ),
        migrations.RenameField(
            model_name='game',
            old_name='player2_score',
            new_name='team2_score'
        )
    ]