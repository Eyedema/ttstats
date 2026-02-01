import django.db.models.deletion
from django.db import migrations, models


def populate_confirmations_from_old_confirmations(apps, schema_editor):
    Match = apps.get_model('pingpong', 'Match')
    MatchConfirmation = apps.get_model('pingpong', 'MatchConfirmation')

    for match in Match.objects.all():
        if match.player1_confirmed:
            confirmation1, _ = MatchConfirmation.objects.get_or_create(
                match=match,
                player=match.player1
            )

        if match.player2_confirmed:
            confirmation2, _ = MatchConfirmation.objects.get_or_create(
                match=match,
                player=match.player2
            )

def copy_winners(apps, schema_editor):
    Match = apps.get_model('pingpong', 'Match')
    Team = apps.get_model('pingpong', 'Team')
    
    for match in Match.objects.exclude(old_winner__isnull=True):
        # Create a transient team for this player if it doesn't exist
        # Or find the team that corresponds to this player
        
        # Since this is a migration, let's keep it robust:
        # We need to find the Team that corresponds to match.old_winner (Player)
        
        # 1. Try to find an existing team for this single player
        teams = Team.objects.filter(players=match.old_winner)
        target_team = None
        
        for team in teams:
            if team.players.count() == 1:
                target_team = team
                break
        
        # 2. If not found, create it
        if not target_team:
            target_team = Team.objects.create()
            target_team.players.add(match.old_winner)
            
        # 3. Assign
        match.winner = target_team
        match.save()



class Migration(migrations.Migration):

    dependencies = [
        ('pingpong', '0008_teams'),
    ]

    operations = [
        migrations.RenameField(
            model_name='match',
            old_name='winner',
            new_name='old_winner',
        ),
        migrations.AddField(
            model_name='match',
            name='winner',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    to='pingpong.team'),
        ),
        migrations.RunPython(copy_winners),
        migrations.RemoveField(
            model_name='match',
            name='old_winner',
        ),
        migrations.CreateModel(
            name='MatchConfirmation',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID'
                )),
                ('confirmed_at', models.DateTimeField(
                    auto_now_add=True
                )),
                ('match', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='pingpong.Match'
                )),
                ('player', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='pingpong.Player'
                )),
            ],
            options={
                'verbose_name': 'Match Confirmation',
                'verbose_name_plural': 'Match Confirmations',
                'unique_together': {('match', 'player')},  # Avoids duplicates
            },
        ),
        migrations.AddField(
            model_name='match',
            name='confirmations',
            field=models.ManyToManyField(
                related_name='player_matchconfirmations',
                through='pingpong.MatchConfirmation',
                to='pingpong.player'),
        ),
        migrations.RunPython(populate_confirmations_from_old_confirmations)
    ]