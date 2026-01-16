import django.db.models.deletion
from django.db import migrations, models


def populate_teams_from_players(apps, schema_editor):
    Match = apps.get_model('pingpong', 'Match')
    Team = apps.get_model('pingpong', 'Team')

    for match in Match.objects.all():
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
        ('pingpong', '0007_userprofile'),
    ]

    operations = [
        migrations.CreateModel(
            name='Team',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID'
                )),
                ('players', models.ManyToManyField(
                    related_name="teams",
                    to='pingpong.Player'
                )),
                ('name', models.CharField(
                    max_length=100,
                    blank=True
                )),
            ]
        ),
        migrations.AddField(
            model_name='match',
            name='is_double',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='match',
            name='team1',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="matches_as_team1",
                to="pingpong.team"
            )
        ),
        migrations.AddField(
            model_name='match',
            name='team2',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="matches_as_team2",
                to="pingpong.team"
            )
        ),
        migrations.RunPython(populate_teams_from_players)
    ]
