from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django_otp_webauthn.models import WebAuthnCredential
from .models import Player, Match, Game, Location, UserProfile, ScheduledMatch

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


admin.site.register(Player)
admin.site.register(Match)
admin.site.register(Game)
admin.site.register(Location)
admin.site.register(UserProfile)
admin.site.register(ScheduledMatch)
