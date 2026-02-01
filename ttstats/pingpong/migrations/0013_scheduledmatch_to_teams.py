import django.db.models.deletion
from django.db import migrations, models

def populate_scheduled_teams_from_players(apps, schema_editor):
    ScheduledMatch = apps.get_model('pingpong', 'ScheduledMatch')
    Team = apps.get_model('pingpong', 'Team')

    for match in ScheduledMatch.objects.all():
        team1, _ = Team.objects.get_or_create(
            defaults={
                'name': f"Team {match.player1.name}"
            }
        )
        team1.players.set([match.player1])

        team2, _ = Team.objects.get_or_create(
            defaults={
                'name': f"Team {match.player2.name}"
            }
        )
        team2.players.set([match.player2])

        match.team1 = team1
        match.team2 = team2
        match.save()


class Migration(migrations.Migration):

    dependencies = [
        ('pingpong', '0012_scheduledmatch'),
    ]

    operations = [
        migrations.AddField(
            model_name='ScheduledMatch',
            name='team1',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="scheduled_matches_as_team1",
                to="pingpong.team"
            )
        ),
        migrations.AddField(
            model_name='ScheduledMatch',
            name='team2',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="scheduled_matches_as_team2",
                to="pingpong.team"
            )
        ),
        migrations.RunPython(populate_scheduled_teams_from_players),
        migrations.RemoveField(
            model_name='ScheduledMatch',
            name='player1',
        ),
        migrations.RemoveField(
            model_name='ScheduledMatch',
            name='player2',
        ),
    ]