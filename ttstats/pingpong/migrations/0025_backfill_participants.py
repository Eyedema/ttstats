"""Backfill MatchParticipant / ScheduledMatchParticipant and winner_side.

Reads the Team FKs and fans them out into participant rows. Runs on historical
models with no signals, so no Elo/confirmation/cache side effects fire.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

BATCH = 500

SIDE_ONE = 1
SIDE_TWO = 2


def _team_player_map(Team):
    """{team_id: [player_id, ...]} in two queries."""
    mapping = {}
    for team in Team.objects.prefetch_related("players"):
        mapping[team.id] = [p.id for p in team.players.all()]
    return mapping


def _side_for(winner_id, team1_id, team2_id):
    if winner_id is None:
        return None
    if winner_id == team1_id:
        return SIDE_ONE
    if winner_id == team2_id:
        return SIDE_TWO
    return None


def _participant_rows(Model, parent_field, parents, team_players):
    """Build participant instances for a queryset of matches/scheduled matches."""
    rows = []
    for parent in parents:
        seen = set()
        for side, team_id in (
            (SIDE_ONE, parent.team1_id),
            (SIDE_TWO, parent.team2_id),
        ):
            if not team_id:
                continue
            for player_id in team_players.get(team_id, ()):
                if player_id in seen:
                    # A player on both sides is corrupt data; the unique
                    # constraint would reject the second row anyway.
                    logger.warning(
                        "Player %s appears on both sides of %s %s; keeping side %s",
                        player_id,
                        parent_field,
                        parent.id,
                        SIDE_ONE,
                    )
                    continue
                seen.add(player_id)
                rows.append(
                    Model(
                        **{parent_field: parent},
                        player_id=player_id,
                        side=side,
                    )
                )
    return rows


def backfill(apps, schema_editor):
    Team = apps.get_model("pingpong", "Team")
    Match = apps.get_model("pingpong", "Match")
    Game = apps.get_model("pingpong", "Game")
    ScheduledMatch = apps.get_model("pingpong", "ScheduledMatch")
    MatchParticipant = apps.get_model("pingpong", "MatchParticipant")
    ScheduledMatchParticipant = apps.get_model(
        "pingpong", "ScheduledMatchParticipant"
    )

    team_players = _team_player_map(Team)

    matches = list(Match.objects.all())
    MatchParticipant.objects.bulk_create(
        _participant_rows(MatchParticipant, "match", matches, team_players),
        batch_size=BATCH,
    )

    scheduled = list(ScheduledMatch.objects.all())
    ScheduledMatchParticipant.objects.bulk_create(
        _participant_rows(
            ScheduledMatchParticipant, "scheduled_match", scheduled, team_players
        ),
        batch_size=BATCH,
    )

    # Match.winner -> winner_side
    orphan_matches = 0
    to_update = []
    for match in matches:
        side = _side_for(match.winner_id, match.team1_id, match.team2_id)
        if match.winner_id is not None and side is None:
            orphan_matches += 1
        match.winner_side = side
        to_update.append(match)
    Match.objects.bulk_update(to_update, ["winner_side"], batch_size=BATCH)

    # Game.winner -> winner_side, resolved against the parent match's teams
    match_teams = {m.id: (m.team1_id, m.team2_id) for m in matches}
    orphan_games = 0
    games = list(Game.objects.all())
    for game in games:
        team1_id, team2_id = match_teams.get(game.match_id, (None, None))
        side = _side_for(game.winner_id, team1_id, team2_id)
        if game.winner_id is not None and side is None:
            orphan_games += 1
        game.winner_side = side
    Game.objects.bulk_update(games, ["winner_side"], batch_size=BATCH)

    summary = (
        f"Backfilled participants for {len(matches)} matches and "
        f"{len(scheduled)} scheduled matches; "
        f"{orphan_matches} matches and {orphan_games} games had a winner that "
        f"matched neither team and were left NULL."
    )
    logger.info(summary)
    print(summary)


def unbackfill(apps, schema_editor):
    """Prod data is expendable, so the reverse simply drops what we added."""
    apps.get_model("pingpong", "MatchParticipant").objects.all().delete()
    apps.get_model("pingpong", "ScheduledMatchParticipant").objects.all().delete()
    apps.get_model("pingpong", "Match").objects.all().update(winner_side=None)
    apps.get_model("pingpong", "Game").objects.all().update(winner_side=None)


class Migration(migrations.Migration):

    dependencies = [
        ("pingpong", "0024_add_match_participants"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
