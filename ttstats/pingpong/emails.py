from django.conf import settings
from django.core.mail import send_mail


def send_match_confirmation_email(match, player, opponent):
    """
    Helper function to send confirmation email to a player.

    Args:
        match: Match instance
        player: Player who needs to confirm
        opponent: Other player in the match
    """
    user = player.user

    # Determine result for this player
    if match.winner == player:
        result = "won"
        emoji = "🎉"
        if match.player1 == player:
            score = f"{match.team1_score}-{match.team2_score}"
        else:
            score = f"{match.team2_score}-{match.team1_score}"
    else:
        result = "lost"
        emoji = "💪"
        if match.player1 == player:
            score = f"{match.team1_score}-{match.team2_score}"
        else:
            score = f"{match.team2_score}-{match.team1_score}"

    # Build absolute URL
    protocol = getattr(settings, "SITE_PROTOCOL", "http")
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    confirmation_url = f"{protocol}://{domain}/pingpong/matches/{match.pk}/"

    subject = f"{emoji} Match Complete - Please Confirm"

    message = f"""Hi {player.name},

Your match against {opponent.name} is complete!

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
        <p>Your match against <strong>{opponent.name}</strong> is complete!</p>
        
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
    print(f"   Match: {match.player1} vs {match.player2}")
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
