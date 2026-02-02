from datetime import date, timedelta
from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.http import HttpResponseRedirect
from django.urls import path
from django.utils.html import format_html
from django_otp_webauthn.models import WebAuthnCredential

from .emails import send_verification_email
from .models import (
    EloHistory,
    Game,
    Location,
    Match,
    MatchConfirmation,
    Player,
    ScheduledMatch,
    Team,
    UserProfile,
)

# Unregister the default User admin
admin.site.unregister(User)

# ============================================================================
# INLINE CLASSES
# ============================================================================


class PasskeyInline(admin.TabularInline):
    """Show passkey count and details in user admin"""

    model = WebAuthnCredential
    extra = 0
    readonly_fields = ("name", "created_at", "sign_count")
    fields = ("name", "created_at", "sign_count")
    can_delete = True

    def has_add_permission(self, request, obj=None):
        # Users must register passkeys through the UI, not admin
        return False


class UserProfileInline(admin.StackedInline):
    """Show user profile information in user admin"""

    model = UserProfile
    fields = ("email_verified", "email_verification_sent_at", "created_at")
    readonly_fields = ("email_verification_sent_at", "created_at")
    can_delete = False


class MatchInline(admin.TabularInline):
    """Show matches at a location"""

    model = Match
    fk_name = "location"
    fields = ("date_played", "team1", "team2", "winner", "match_type")
    readonly_fields = ("winner",)
    extra = 0
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ScheduledMatchInline(admin.TabularInline):
    """Show scheduled matches at a location"""

    model = ScheduledMatch
    fk_name = "location"
    fields = ("scheduled_date", "scheduled_time", "team1", "team2", "notification_sent")
    extra = 0
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class TeamPlayerInline(admin.TabularInline):
    """Show players in a team"""

    model = Team.players.through
    extra = 0


class GameInline(admin.TabularInline):
    """Show games in a match"""

    model = Game
    fields = ("game_number", "team1_score", "team2_score", "winner", "duration_minutes")
    readonly_fields = ("winner",)
    extra = 1
    ordering = ("game_number",)


class MatchConfirmationInline(admin.TabularInline):
    """Show match confirmations"""

    model = MatchConfirmation
    fields = ("player", "confirmed_at")
    readonly_fields = ("player", "confirmed_at")
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class EloHistoryInline(admin.TabularInline):
    """Show Elo history for a match"""

    model = EloHistory
    fields = ("player", "old_rating", "new_rating", "rating_change", "k_factor")
    readonly_fields = ("player", "old_rating", "new_rating", "rating_change", "k_factor")
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


# ============================================================================
# CUSTOM FILTER CLASSES
# ============================================================================


class HasWinnerFilter(SimpleListFilter):
    """Filter matches by completion status"""

    title = "completion status"
    parameter_name = "has_winner"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Complete (has winner)"),
            ("no", "In Progress (no winner)"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(winner__isnull=False)
        if self.value() == "no":
            return queryset.filter(winner__isnull=True)
        return queryset


class HasDurationFilter(SimpleListFilter):
    """Filter games by duration presence"""

    title = "duration"
    parameter_name = "has_duration"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Has duration"),
            ("no", "No duration"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(duration_minutes__isnull=False)
        if self.value() == "no":
            return queryset.filter(duration_minutes__isnull=True)
        return queryset


class UpcomingFilter(SimpleListFilter):
    """Filter scheduled matches by upcoming/past"""

    title = "timing"
    parameter_name = "upcoming"

    def lookups(self, request, model_admin):
        return (
            ("upcoming", "Upcoming"),
            ("past", "Past"),
        )

    def queryset(self, request, queryset):
        today = date.today()
        if self.value() == "upcoming":
            return queryset.filter(scheduled_date__gte=today)
        if self.value() == "past":
            return queryset.filter(scheduled_date__lt=today)
        return queryset


class UnverifiedForDaysFilter(SimpleListFilter):
    """Filter unverified profiles by days since email sent"""

    title = "unverified duration"
    parameter_name = "unverified_days"

    def lookups(self, request, model_admin):
        return (
            ("1", "> 1 day"),
            ("7", "> 7 days"),
            ("30", "> 30 days"),
        )

    def queryset(self, request, queryset):
        if self.value():
            days = int(self.value())
            cutoff = date.today() - timedelta(days=days)
            return queryset.filter(
                email_verified=False, email_verification_sent_at__date__lt=cutoff
            )
        return queryset


# ============================================================================
# ADMIN CLASSES
# ============================================================================


@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    """Custom User admin with passkey and profile information"""

    inlines = [UserProfileInline, PasskeyInline]

    list_display = (
        "username",
        "email",
        #"email_verified_status",
        "is_staff",
        "passkey_count",
        "date_joined",
    )

    def passkey_count(self, obj):
        """Display number of registered passkeys"""
        count = WebAuthnCredential.objects.filter(user=obj).count()
        return f"{count} passkey{'s' if count != 1 else ''}"

    passkey_count.short_description = "Passkeys"
    passkey_count.admin_order_field = None  # Not sortable (aggregated)

    def email_verified_status(self, obj):
        """Show email verification status with color"""
        try:
            if obj.profile.email_verified:
                return format_html(
                    '<span style="color: green; font-weight: bold;">✓ Verified</span>'
                )
            else:
                return format_html(
                    '<span style="color: orange; font-weight: bold;">✗ Unverified</span>'
                )
        except UserProfile.DoesNotExist:
            return format_html('<span style="color: red;">No Profile</span>')

    email_verified_status.short_description = "Email Status"
    email_verified_status.admin_order_field = "profile__email_verified"


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """User profile admin with verification management"""

    list_display = (
        "user",
        #"email_verified_icon",
        "email_verification_sent_at",
        "days_since_sent",
        "created_at",
    )
    list_filter = (
        "email_verified",
        "email_verification_sent_at",
        "created_at",
        UnverifiedForDaysFilter,
    )
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
    )
    readonly_fields = (
        "email_verification_token",
        "email_verification_sent_at",
        "created_at",
        "days_since_sent",
    )
    actions = ["resend_verification_email", "manually_verify"]

    fieldsets = (
        ("User", {"fields": ("user",)}),
        (
            "Verification",
            {
                "fields": (
                    "email_verified",
                    "email_verification_token",
                    "email_verification_sent_at",
                    "days_since_sent",
                )
            },
        ),
        ("Metadata", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    def email_verified_icon(self, obj):
        """Show verification status with icon"""
        if obj.email_verified:
            return format_html(
                '<span style="color: green; font-weight: bold;">✓</span>'
            )
        return format_html('<span style="color: red; font-weight: bold;">✗</span>')

    email_verified_icon.short_description = "Verified"
    email_verified_icon.admin_order_field = "email_verified"

    def days_since_sent(self, obj):
        """Calculate days since verification email sent"""
        if obj.email_verification_sent_at:
            try:
                delta = date.today() - obj.email_verification_sent_at.date()
                return f"{delta.days} days ago"
            except (AttributeError, TypeError):
                return "Error"
        return "Never sent"

    days_since_sent.short_description = "Days Since Sent"
    days_since_sent.admin_order_field = "email_verification_sent_at"

    @admin.action(description="Resend verification email")
    def resend_verification_email(self, request, queryset):
        """Resend verification email to selected profiles"""
        count = 0
        for profile in queryset.filter(email_verified=False):
            send_verification_email(profile)
            count += 1
        self.message_user(request, f"Sent {count} verification email(s).")

    @admin.action(description="Manually verify email")
    def manually_verify(self, request, queryset):
        """Manually verify selected profiles"""
        count = queryset.filter(email_verified=False).update(
            email_verified=True, email_verification_token=""
        )
        self.message_user(request, f"Verified {count} profile(s).")


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    """Custom Player admin with Elo rating information"""

    list_display = (
        "name",
        "nickname",
        "elo_rating",
        "elo_peak",
        "matches_for_elo",
        "user",
        "created_at",
    )
    list_filter = ("playing_style", "created_at", "user__profile__email_verified")
    search_fields = ("name", "nickname", "user__username", "user__email")
    readonly_fields = ("created_at", "elo_rating", "elo_peak", "matches_for_elo")

    fieldsets = (
        ("Basic Info", {"fields": ("user", "name", "nickname", "playing_style", "notes")}),
        (
            "Elo Statistics",
            {
                "fields": ("elo_rating", "elo_peak", "matches_for_elo"),
                "classes": ("collapse",),
            },
        ),
        ("Metadata", {"fields": ("created_at",), "classes": ("collapse",)}),
    )


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    """Team admin with player and match statistics"""

    list_display = (
        "__str__",
        "name",
        "player_count",
        "matches_played",
        "matches_won",
        "win_rate",
    )
    list_filter = ("players",)
    search_fields = ("name", "players__name", "players__nickname")
    readonly_fields = ("matches_played", "matches_won", "win_rate")
    inlines = [TeamPlayerInline]

    fieldsets = (
        ("Basic Info", {"fields": ("name",)}),
        (
            "Statistics",
            {
                "fields": ("matches_played", "matches_won", "win_rate"),
                "classes": ("collapse",),
            },
        ),
    )

    def player_count(self, obj):
        """Count players in team"""
        try:
            return obj.players.count()
        except (AttributeError, TypeError):
            return 0

    player_count.short_description = "Players"
    player_count.admin_order_field = None  # Not sortable (aggregated)

    def matches_played(self, obj):
        """Count total matches"""
        try:
            return obj.matches_as_team1.count() + obj.matches_as_team2.count()
        except (AttributeError, TypeError):
            return 0

    matches_played.short_description = "Matches Played"
    matches_played.admin_order_field = None  # Not sortable (aggregated)

    def matches_won(self, obj):
        """Count matches won"""
        try:
            return Match.objects.filter(winner=obj).count()
        except (AttributeError, TypeError):
            return 0

    matches_won.short_description = "Matches Won"
    matches_won.admin_order_field = None  # Not sortable (aggregated)

    def win_rate(self, obj):
        """Calculate win rate percentage"""
        try:
            total = self.matches_played(obj)
            if total == 0:
                return "0%"
            won = self.matches_won(obj)
            return f"{(won / total * 100):.1f}%"
        except (AttributeError, TypeError, ZeroDivisionError):
            return "0%"

    win_rate.short_description = "Win Rate"
    win_rate.admin_order_field = None  # Not sortable (calculated)


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    """Location admin with match statistics"""

    list_display = (
        "name",
        "address_preview",
        "match_count",
        "scheduled_match_count",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = ("name", "address", "notes")
    readonly_fields = ("created_at", "match_count", "scheduled_match_count")
    inlines = [MatchInline, ScheduledMatchInline]

    fieldsets = (
        ("Basic Info", {"fields": ("name", "address", "notes")}),
        (
            "Statistics",
            {
                "fields": ("match_count", "scheduled_match_count"),
                "classes": ("collapse",),
            },
        ),
        ("Metadata", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    def address_preview(self, obj):
        """Show truncated address"""
        if not obj.address:
            return ""
        if len(obj.address) > 50:
            return f"{obj.address[:50]}..."
        return obj.address

    address_preview.short_description = "Address"
    address_preview.admin_order_field = "address"

    def match_count(self, obj):
        """Count matches at this location"""
        try:
            return obj.match_set.count()
        except (AttributeError, TypeError):
            return 0

    match_count.short_description = "Matches"
    match_count.admin_order_field = None  # Not sortable (aggregated)

    def scheduled_match_count(self, obj):
        """Count scheduled matches at this location"""
        try:
            return obj.scheduledmatch_set.count()
        except (AttributeError, TypeError):
            return 0

    scheduled_match_count.short_description = "Scheduled Matches"
    scheduled_match_count.admin_order_field = None  # Not sortable (aggregated)


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    """Match admin with comprehensive filtering and statistics"""

    list_display = (
        "id",
        "teams_display",
        "match_score",
        "winner_display",
        "date_played",
        "match_type",
        "location",
        #"confirmation_status",
        "is_double",
    )
    list_filter = (
        "is_double",
        "match_type",
        "best_of",
        "date_played",
        "location",
        HasWinnerFilter,
        "team1__players",
        "team2__players",
    )
    search_fields = (
        "team1__players__name",
        "team1__players__nickname",
        "team2__players__name",
        "team2__players__nickname",
        "notes",
        "id",
    )
    readonly_fields = (
        "winner",
        "created_at",
        "updated_at",
        "match_confirmed",
        "team1_score",
        "team2_score",
    )
    date_hierarchy = "date_played"
    inlines = [GameInline, MatchConfirmationInline, EloHistoryInline]

    fieldsets = (
        ("Teams", {"fields": ("is_double", "team1", "team2")}),
        (
            "Match Details",
            {"fields": ("date_played", "location", "match_type", "best_of", "notes")},
        ),
        (
            "Result",
            {
                "fields": ("winner", "team1_score", "team2_score", "match_confirmed"),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def get_queryset(self, request):
        """Optimize query with select_related and prefetch_related"""
        qs = super().get_queryset(request)
        return qs.select_related("team1", "team2", "winner", "location").prefetch_related(
            "team1__players", "team2__players", "games"
        )

    def teams_display(self, obj):
        """Show team matchup"""
        team1 = obj.team1 if obj.team1 else "N/A"
        team2 = obj.team2 if obj.team2 else "N/A"
        return f"{team1} vs {team2}"

    teams_display.short_description = "Matchup"
    teams_display.admin_order_field = "team1"

    def match_score(self, obj):
        """Show match score"""
        try:
            return f"{obj.team1_score}-{obj.team2_score}"
        except (AttributeError, TypeError):
            return "0-0"

    match_score.short_description = "Score"
    match_score.admin_order_field = None  # Not sortable

    def winner_display(self, obj):
        """Show winner with color formatting"""
        if obj.winner:
            return format_html(
                '<span style="color: green; font-weight: bold;">{}</span>', obj.winner
            )
        return format_html(
            '<span style="color: orange;">In Progress</span>'
        )

    winner_display.short_description = "Winner"
    winner_display.admin_order_field = "winner"

    def confirmation_status(self, obj):
        """Show confirmation status with icon"""
        try:
            if obj.match_confirmed:
                return format_html(
                    '<span style="color: green; font-weight: bold;">✓ Confirmed</span>'
                )
        except (AttributeError, TypeError):
            pass
        return format_html(
            '<span style="color: orange; font-weight: bold;">⏳ Pending</span>'
        )

    confirmation_status.short_description = "Status"
    confirmation_status.admin_order_field = None  # Not sortable (property)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    """Game admin with match context"""

    list_display = (
        "id",
        "match_link",
        "game_number",
        "score_display",
        "winner",
        "duration_minutes",
        "match_date",
    )
    list_filter = (
        "match__match_type",
        "match__date_played",
        "match__location",
        "game_number",
        HasDurationFilter,
    )
    search_fields = (
        "match__team1__players__name",
        "match__team2__players__name",
        "match__id",
    )
    readonly_fields = ("winner", "match_date", "match_link")

    fieldsets = (
        ("Match Info", {"fields": ("match", "match_link", "match_date", "game_number")}),
        (
            "Score",
            {"fields": ("team1_score", "team2_score", "winner", "duration_minutes")},
        ),
    )

    def get_queryset(self, request):
        """Optimize query with select_related"""
        qs = super().get_queryset(request)
        return qs.select_related("match", "match__team1", "match__team2", "winner")

    def match_link(self, obj):
        """Clickable link to parent match"""
        if not obj.match:
            return "N/A"
        try:
            url = f"/admin/pingpong/match/{obj.match.pk}/change/"
            return format_html('<a href="{}">{}</a>', url, obj.match)
        except (AttributeError, TypeError):
            return str(obj.match) if obj.match else "N/A"

    match_link.short_description = "Match"
    match_link.admin_order_field = "match"

    def score_display(self, obj):
        """Show game score"""
        return f"{obj.team1_score}-{obj.team2_score}"

    score_display.short_description = "Score"
    score_display.admin_order_field = "team1_score"

    def match_date(self, obj):
        """Show match date"""
        if not obj.match:
            return "N/A"
        try:
            return obj.match.date_played.date()
        except (AttributeError, TypeError):
            return "N/A"

    match_date.short_description = "Match Date"
    match_date.admin_order_field = "match__date_played"


@admin.register(MatchConfirmation)
class MatchConfirmationAdmin(admin.ModelAdmin):
    """Match confirmation admin with match context"""

    list_display = (
        "id",
        "match_link",
        "player",
        "confirmed_at",
        "match_date",
        "match_winner",
    )
    list_filter = ("confirmed_at", "player", "match__match_type", "match__date_played")
    search_fields = (
        "player__name",
        "player__nickname",
        "match__id",
        "match__team1__players__name",
        "match__team2__players__name",
    )
    readonly_fields = ("confirmed_at", "match_link", "match_date", "match_winner")
    date_hierarchy = "confirmed_at"

    fieldsets = (
        ("Confirmation", {"fields": ("match", "player", "confirmed_at")}),
        (
            "Match Info",
            {
                "fields": ("match_link", "match_date", "match_winner"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        """Optimize query with select_related"""
        qs = super().get_queryset(request)
        return qs.select_related("match", "match__team1", "match__team2", "match__winner", "player")

    def match_link(self, obj):
        """Clickable link to match"""
        if not obj.match:
            return "N/A"
        try:
            url = f"/admin/pingpong/match/{obj.match.pk}/change/"
            return format_html('<a href="{}">{}</a>', url, obj.match)
        except (AttributeError, TypeError):
            return str(obj.match) if obj.match else "N/A"

    match_link.short_description = "Match"
    match_link.admin_order_field = "match"

    def match_date(self, obj):
        """Show match date"""
        if not obj.match:
            return "N/A"
        try:
            return obj.match.date_played.date()
        except (AttributeError, TypeError):
            return "N/A"

    match_date.short_description = "Match Date"
    match_date.admin_order_field = "match__date_played"

    def match_winner(self, obj):
        """Show match winner"""
        if not obj.match:
            return "N/A"
        try:
            if obj.match.winner:
                return obj.match.winner
        except (AttributeError, TypeError):
            pass
        return "In Progress"

    match_winner.short_description = "Winner"
    match_winner.admin_order_field = "match__winner"


@admin.register(ScheduledMatch)
class ScheduledMatchAdmin(admin.ModelAdmin):
    """Scheduled match admin with notification management"""

    list_display = (
        "id",
        "teams_display",
        "scheduled_date",
        "scheduled_time",
        "location",
        "created_by",
        #"notification_sent_icon",
        "days_until",
    )
    list_filter = (
        "scheduled_date",
        "location",
        "notification_sent",
        "created_by",
        "team1__players",
        "team2__players",
        UpcomingFilter,
    )
    search_fields = (
        "team1__players__name",
        "team1__players__nickname",
        "team2__players__name",
        "team2__players__nickname",
        "notes",
        "location__name",
    )
    readonly_fields = ("created_at", "scheduled_datetime", "days_until")
    date_hierarchy = "scheduled_date"
    actions = ["mark_notifications_sent", "mark_notifications_not_sent"]

    fieldsets = (
        ("Teams", {"fields": ("team1", "team2")}),
        (
            "Schedule",
            {
                "fields": (
                    "scheduled_date",
                    "scheduled_time",
                    "scheduled_datetime",
                    "days_until",
                    "location",
                    "notes",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_by", "notification_sent", "created_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        """Optimize query with select_related and prefetch_related"""
        qs = super().get_queryset(request)
        return qs.select_related("team1", "team2", "location", "created_by").prefetch_related(
            "team1__players", "team2__players"
        )

    def teams_display(self, obj):
        """Show team matchup"""
        team1 = obj.team1 if obj.team1 else "N/A"
        team2 = obj.team2 if obj.team2 else "N/A"
        return f"{team1} vs {team2}"

    teams_display.short_description = "Matchup"
    teams_display.admin_order_field = "team1"

    def notification_sent_icon(self, obj):
        """Show notification status with icon"""
        if obj.notification_sent:
            return format_html(
                '<span style="color: green; font-weight: bold;">✓</span>'
            )
        return format_html('<span style="color: red; font-weight: bold;">✗</span>')

    notification_sent_icon.short_description = "Notified"
    notification_sent_icon.admin_order_field = "notification_sent"

    def days_until(self, obj):
        """Calculate days until match"""
        if not obj.scheduled_date:
            return "N/A"
        try:
            delta = obj.scheduled_date - date.today()
            days = delta.days
            if days > 0:
                return format_html(
                    '<span style="color: green;">In {} day{}</span>',
                    days,
                    "s" if days != 1 else "",
                )
            elif days == 0:
                return format_html('<span style="color: orange; font-weight: bold;">Today</span>')
            else:
                return format_html(
                    '<span style="color: red;">{} day{} ago</span>',
                    abs(days),
                    "s" if abs(days) != 1 else "",
                )
        except (AttributeError, TypeError):
            return "N/A"

    days_until.short_description = "Days Until"
    days_until.admin_order_field = "scheduled_date"

    @admin.action(description="Mark notifications as sent")
    def mark_notifications_sent(self, request, queryset):
        """Mark selected matches as notified"""
        count = queryset.update(notification_sent=True)
        self.message_user(request, f"Marked {count} match(es) as notified.")

    @admin.action(description="Mark notifications as not sent")
    def mark_notifications_not_sent(self, request, queryset):
        """Mark selected matches as not notified"""
        count = queryset.update(notification_sent=False)
        self.message_user(request, f"Marked {count} match(es) as not notified.")


@admin.register(EloHistory)
class EloHistoryAdmin(admin.ModelAdmin):
    """Elo history admin with recalculation action"""

    list_display = (
        "player",
        "match",
        "old_rating",
        "new_rating",
        "rating_change",
        "k_factor",
        "created_at",
    )
    list_filter = ("created_at", "player")
    search_fields = ("player__name", "match__id")
    readonly_fields = (
        "match",
        "player",
        "old_rating",
        "new_rating",
        "rating_change",
        "k_factor",
        "created_at",
    )

    # Point to the new template we just created
    change_list_template = "admin/pingpong/EloHistory/change_list.html"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path(
                "recalculate/",
                self.admin_site.admin_view(self.recalculate_elo_view),
                name="recalculate_elo",
            ),
        ]
        return my_urls + urls

    def recalculate_elo_view(self, request):
        if request.method == "POST":
            # Your command does not take arguments, so we just call it
            call_command("recalculate_elo")
            self.message_user(request, "Elo ratings recalculated successfully.")
            return HttpResponseRedirect("../")

        # Fallback if accessed via GET (optional)
        return HttpResponseRedirect("../")
