from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.http import HttpResponseRedirect
from django_otp_webauthn.models import WebAuthnCredential
from .models import Player, Match, Game, Location, UserProfile, ScheduledMatch, EloHistory, MatchConfirmation, Team

# Unregister the default User admin
admin.site.unregister(User)


class PasskeyInline(admin.TabularInline):
    """Show passkey count and details in user admin"""
    model = WebAuthnCredential
    extra = 0
    readonly_fields = ('name', 'created_at', 'sign_count')
    fields = ('name', 'created_at', 'sign_count')
    can_delete = True

    def has_add_permission(self, request, obj=None):
        # Users must register passkeys through the UI, not admin
        return False


@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    """Custom User admin with passkey information"""
    inlines = [PasskeyInline]

    list_display = ('username', 'email', 'is_staff', 'passkey_count', 'date_joined')

    def passkey_count(self, obj):
        """Display number of registered passkeys"""
        count = WebAuthnCredential.objects.filter(user=obj).count()
        return f"{count} passkey{'s' if count != 1 else ''}"
    passkey_count.short_description = 'Passkeys'


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    """Custom Player admin with Elo rating information"""
    list_display = ('name', 'nickname', 'elo_rating', 'elo_peak', 'matches_for_elo', 'user', 'created_at')
    list_filter = ('playing_style', 'created_at')
    search_fields = ('name', 'nickname', 'user__username')
    readonly_fields = ('created_at', 'elo_rating', 'elo_peak', 'matches_for_elo')  # Elo is auto-calculated


@admin.register(EloHistory)
class EloHistoryAdmin(admin.ModelAdmin):
    """Elo History admin (read-only)"""
    list_display = ('player', 'match', 'old_rating', 'new_rating', 'rating_change', 'k_factor', 'created_at')
    list_filter = ('created_at', 'player')
    search_fields = ('player__name', 'match__id')
    readonly_fields = ('match', 'player', 'old_rating', 'new_rating', 'rating_change', 'k_factor', 'created_at')
    change_form_template = "admin/pingpong/EloHistory/change_form.html"

    def has_add_permission(self, request):
        return False  # Elo history is auto-generated

    def has_change_permission(self, request, obj=None):
        return False  # Elo history is immutable
    
    
    def response_change(self, request, obj):
        if "_run_command" in request.POST:
            call_command('recalculate_elo', obj.id)
            self.message_user(request, "Elo recalculated® successfully")
            return HttpResponseRedirect(".")
        return super().response_change(request, obj)


admin.site.register(Match)
admin.site.register(Game)
admin.site.register(Location)
admin.site.register(UserProfile)
admin.site.register(Team)
admin.site.register(MatchConfirmation)
admin.site.register(ScheduledMatch)
