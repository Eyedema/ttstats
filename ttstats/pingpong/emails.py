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
    if player in match.winner.players.all():
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


def send_scheduled_match_email(scheduled_match, player):
    """
    Send notification email to a player about a scheduled match.

    Args:
        scheduled_match: ScheduledMatch instance
        player: Player who is being notified
    """
    user = player.user
    if not user or not user.email:
        return

    # Build absolute URL
    protocol = getattr(settings, "SITE_PROTOCOL", "http")
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    calendar_url = f"{protocol}://{domain}/pingpong/calendar/"

    # Format date and time
    date_str = scheduled_match.scheduled_date.strftime("%A, %B %d, %Y")
    time_str = scheduled_match.scheduled_time.strftime("%I:%M %p")
    location_str = scheduled_match.location.name if scheduled_match.location else "TBD"

    subject = f"🏓 Match Scheduled - {date_str}"

    message = f"""Hi {player.name},

A match has been scheduled for you!

📅 Match Details:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Opponent: {str(scheduled_match.team2)}
  Date: {date_str}
  Time: {time_str}
  Location: {location_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📆 View your calendar:
{calendar_url}

Good luck!
Table Tennis Tracker Team
"""

    html_message = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>🏓 Match Scheduled!</h2>
        <p>Hi {player.name},</p>
        <p>A match has been scheduled for you!</p>

        <div style="background: #f3f4f6; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h3 style="margin-top: 0;">📅 Match Details</h3>
            <p style="margin: 5px 0;"><strong>You:</strong> {scheduled_match.team1.name}</p>
            <p style="margin: 5px 0;"><strong>Opponent:</strong> {scheduled_match.team2.name}</p>
            <p style="margin: 5px 0;">📆 <strong>Date:</strong> {date_str}</p>
            <p style="margin: 5px 0;">🕐 <strong>Time:</strong> {time_str}</p>
            <p style="margin: 5px 0;">📍 <strong>Location:</strong> {location_str}</p>
        </div>

        <a href="{calendar_url}"
           style="display: inline-block; background: #2563eb; color: white; padding: 12px 24px;
                  text-decoration: none; border-radius: 6px; font-weight: bold;">
            📆 View Calendar
        </a>

        <p style="margin-top: 30px;">Good luck!<br>Table Tennis Tracker Team</p>
    </body>
    </html>
    """

    # Print to console for development
    print(f"\n{'=' * 60}")
    print(f"📧 SCHEDULED MATCH EMAIL TO: {user.email}")
    print(f"   Match: {scheduled_match.team1} vs {scheduled_match.team2}")
    print(f"   Date: {date_str} at {time_str}")
    print(f"   Location: {location_str}")
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

def send_passkey_registered_email(user, device_name):
    """Notify user when new passkey is registered"""
    subject = "New Passkey Registered - TTStats"

    # Build absolute URL
    protocol = getattr(settings, "SITE_PROTOCOL", "http")
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    passkey_url = f"{protocol}://{domain}/pingpong/passkeys/"

    message = f"""Hi {user.username},

A new passkey "{device_name}" was registered on your account.

If you didn't authorize this, please log in immediately and remove it:
{passkey_url}

If you have any concerns, please contact support.

- TTStats Team
"""

    html_message = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>🔐 New Passkey Registered</h2>
        <p>Hi {user.username},</p>
        <p>A new passkey <strong>"{device_name}"</strong> was registered on your account.</p>

        <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0;">
            <p style="margin: 0; color: #92400e;">
                <strong>⚠️ Security Notice:</strong> If you didn't authorize this, please take action immediately.
            </p>
        </div>

        <a href="{passkey_url}"
           style="display: inline-block; background: #dc2626; color: white; padding: 12px 24px;
                  text-decoration: none; border-radius: 6px; font-weight: bold;">
            Review Passkeys
        </a>

        <p style="margin-top: 30px; color: #6b7280; font-size: 14px;">
            If you have any concerns, please contact support immediately.
        </p>

        <p>- TTStats Team</p>
    </body>
    </html>
    """

    # Print to console for development
    print(f"\n{'=' * 60}")
    print(f"📧 PASSKEY REGISTERED EMAIL TO: {user.email}")
    print(f"   Device: {device_name}")
    print(f"   URL: {passkey_url}")
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


def send_passkey_deleted_email(user, device_name):
    """Notify user when passkey is deleted"""
    subject = "Passkey Removed - TTStats"

    # Build absolute URL
    protocol = getattr(settings, "SITE_PROTOCOL", "http")
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    passkey_url = f"{protocol}://{domain}/pingpong/passkeys/"

    message = f"""Hi {user.username},

The passkey "{device_name}" was removed from your account.

If you didn't authorize this, please log in immediately and review your security:
{passkey_url}

Consider changing your password if you suspect unauthorized access.

- TTStats Team
"""

    html_message = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>🔐 Passkey Removed</h2>
        <p>Hi {user.username},</p>
        <p>The passkey <strong>"{device_name}"</strong> was removed from your account.</p>

        <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0;">
            <p style="margin: 0; color: #92400e;">
                <strong>⚠️ Security Notice:</strong> If you didn't authorize this, please review your account security.
            </p>
        </div>

        <a href="{passkey_url}"
           style="display: inline-block; background: #dc2626; color: white; padding: 12px 24px;
                  text-decoration: none; border-radius: 6px; font-weight: bold;">
            Review Passkeys
        </a>

        <p style="margin-top: 30px; color: #6b7280; font-size: 14px;">
            Consider changing your password if you suspect unauthorized access.
        </p>

        <p>- TTStats Team</p>
    </body>
    </html>
    """

    # Print to console for development
    print(f"\n{'=' * 60}")
    print(f"📧 PASSKEY DELETED EMAIL TO: {user.email}")
    print(f"   Device: {device_name}")
    print(f"   URL: {passkey_url}")
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


def send_verification_email(user_profile):
    """Send email verification email to user"""
    user = user_profile.user
    token = user_profile.create_verification_token()
    user_profile.save()

    # Build absolute URL
    protocol = getattr(settings, "SITE_PROTOCOL", "http")
    domain = getattr(settings, "SITE_DOMAIN", "localhost:8000")
    verification_url = f"{protocol}://{domain}/pingpong/verify-email/{token}/"

    subject = "Verify Your Email - TTStats"

    message = f"""Hi {user.username},

Thank you for signing up for TTStats!

Please verify your email address by clicking the link below:
{verification_url}

This link will expire in 24 hours.

If you didn't create an account, you can safely ignore this email.

- TTStats Team
"""

    html_message = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>Welcome to TTStats!</h2>
        <p>Hi {user.username},</p>
        <p>Thank you for signing up! Please verify your email address to get started.</p>

        <a href="{verification_url}"
           style="display: inline-block; background: #2563eb; color: white; padding: 12px 24px;
                  text-decoration: none; border-radius: 6px; font-weight: bold; margin: 20px 0;">
            ✅ Verify Email Address
        </a>

        <p style="margin-top: 30px; color: #6b7280; font-size: 14px;">
            This link will expire in 24 hours. If you didn't create an account, you can safely ignore this email.
        </p>

        <p>- TTStats Team</p>
    </body>
    </html>
    """

    # Print to console for development
    print(f"\n{'=' * 60}")
    print(f"📧 VERIFICATION EMAIL TO: {user.email}")
    print(f"   Token: {token}")
    print(f"   URL: {verification_url}")
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
