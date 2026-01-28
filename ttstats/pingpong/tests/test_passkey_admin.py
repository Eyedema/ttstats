import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory
from django_otp_webauthn.models import WebAuthnCredential
from pingpong.admin import CustomUserAdmin, PasskeyInline
from .conftest import UserFactory


@pytest.mark.django_db
class TestPasskeyAdmin:
    def test_passkey_count_display(self):
        """Admin shows correct passkey count"""
        user = UserFactory()
        admin_site = AdminSite()
        user_admin = CustomUserAdmin(User, admin_site)

        # No passkeys
        assert user_admin.passkey_count(user) == "0 passkeys"

        # Add one passkey
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test1',
            public_key=b'pub1',
            name="Device 1"
        )
        assert user_admin.passkey_count(user) == "1 passkey"

        # Add another
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test2',
            public_key=b'pub2',
            name="Device 2"
        )
        assert user_admin.passkey_count(user) == "2 passkeys"

    def test_passkey_inline_readonly(self):
        """Passkey inline fields are readonly"""
        inline = PasskeyInline(WebAuthnCredential, AdminSite())
        assert 'name' in inline.readonly_fields
        assert 'created_at' in inline.readonly_fields
        assert 'sign_count' in inline.readonly_fields

    def test_cannot_add_passkey_in_admin(self):
        """Cannot add passkeys through admin"""
        inline = PasskeyInline(WebAuthnCredential, AdminSite())
        request = RequestFactory().get('/admin/')

        assert inline.has_add_permission(request) is False

    def test_passkey_count_in_list_display(self):
        """Passkey count appears in user list display"""
        admin_site = AdminSite()
        user_admin = CustomUserAdmin(User, admin_site)

        assert 'passkey_count' in user_admin.list_display

    def test_passkey_inline_can_delete(self):
        """Admin can delete passkeys through inline"""
        inline = PasskeyInline(WebAuthnCredential, AdminSite())
        assert inline.can_delete is True

    def test_passkey_inline_fields(self):
        """Passkey inline shows correct fields"""
        inline = PasskeyInline(WebAuthnCredential, AdminSite())
        assert 'name' in inline.fields
        assert 'created_at' in inline.fields
        assert 'sign_count' in inline.fields

    def test_passkey_count_description(self):
        """Passkey count has proper description"""
        admin_site = AdminSite()
        user_admin = CustomUserAdmin(User, admin_site)

        assert user_admin.passkey_count.short_description == 'Passkeys'

    def test_custom_user_admin_includes_passkey_inline(self):
        """CustomUserAdmin includes PasskeyInline"""
        admin_site = AdminSite()
        user_admin = CustomUserAdmin(User, admin_site)

        # Create request with user attribute
        request = RequestFactory().get('/admin/')
        request.user = UserFactory(is_staff=True, is_superuser=True)

        inline_classes = [inline.__class__ for inline in user_admin.get_inline_instances(request)]
        assert PasskeyInline in inline_classes
