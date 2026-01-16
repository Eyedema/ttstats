from django.conf import settings
from django.core.mail import send_mail


def send_match_confirmation_email(match, player):
    """
    Helper function to send confirmation email to a player.

    Args:
        match: Match instance
        player: Player who needs to confirm
    """
    user = player.user

    player_team = None
    opponent_team = None

    if player in match.team1.players.all():
        player_team, opponent_team = match.team1, match.team2
        score = f"{match.team1_score}-{match.team2_score}"
        opponent_name = f"{match.team2}"
    elif player in match.team2.players.all():
        player_team, opponent_team = match.team2, match.team1
        score = f"{match.team2_score}-{match.team1_score}"
        opponent_name = f"{match.team1}"
    else:
        return

    # Determine result for this player
    if player in match.winner.all():
        result = "won"
        emoji = "🎉"
        if player in match.team1.players.all():
            score = f"{match.team1_score}-{match.team2_score}"
        else:
            score = f"{match.team2_score}-{match.team1_score}"
    else:
        result = "lost"
        emoji = "💪"
        if player in match.team1.players.all():
            score = f"{match.team1_score}-{match.team2_score}"
        else:
            score = f"{match.team2_score}-{match.team1_score}"

    # Build absolute URL
    protocol = getattr(settings, "SITE_PROTOCOL", "http")
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    confirmation_url = f"{protocol}://{domain}/pingpong/matches/{match.pk}/"

    subject = f"{emoji} Match Complete - Please Confirm"

    message = f"""Hi {player.name},

Your match against {opponent_team} is complete!

📊 Match Summary:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  You {result.upper()} {score}
  Date: {match.date_played.strftime("%B %d, %Y at %I:%M %p")}
  Type: {match.get_match_type_display()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ Please confirm this result:
{confirmation_url}

Once both players confirm, this match will be included in rankings and statistics.

Good game!
Table Tennis Tracker Team
"""

    # HTML version (optional)
    html_message = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>{emoji} Match Complete!</h2>
        <p>Hi {player.name},</p>
        <p>Your match against <strong>{opponent_team}</strong> is complete!</p>
        
        <div style="background: #f3f4f6; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3 style="margin-top: 0;">📊 Match Summary</h3>
            <p style="font-size: 24px; font-weight: bold; margin: 10px 0;">
                You {result.upper()} {score}
            </p>
            <p style="margin: 5px 0;">📅 {match.date_played.strftime("%B %d, %Y at %I:%M %p")}</p>
            <p style="margin: 5px 0;">🏓 {match.get_match_type_display()}</p>
        </div>
        
        <a href="{confirmation_url}" 
           style="display: inline-block; background: #2563eb; color: white; padding: 12px 24px; 
                  text-decoration: none; border-radius: 6px; font-weight: bold;">
            ✅ Confirm Match Result
        </a>
        
        <p style="margin-top: 30px; color: #6b7280; font-size: 14px;">
            Once both players confirm, this match will be included in rankings and statistics.
        </p>
        
        <p>Good game!<br>Table Tennis Tracker Team</p>
    </body>
    </html>
    """

    # Print to console for development
    print(f"\n{'=' * 60}")
    print(f"📧 MATCH CONFIRMATION EMAIL TO: {user.email}")
    print(f"   Match: {match.team1} vs {match.team2}")
    print(f"   Result: {player.name} {result} {score}")
    print(f"   URL: {confirmation_url}")
    print(f"{'=' * 60}\n")

    # Send email
    try:
        send_mail(
            subject=subject,
            message=message,
            html_message=html_message,
            from_email="noreply@tabletennis.com",
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception as e:
        print(f"❌ Email send error: {e}")
