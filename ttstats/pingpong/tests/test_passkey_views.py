import pytest
from django.urls import reverse
from django_otp_webauthn.models import WebAuthnCredential
from .conftest import PlayerFactory


@pytest.mark.django_db
class TestPasskeyManagement:
    def test_passkey_management_requires_login(self, client):
        """Unauthenticated users redirected to login"""
        response = client.get(reverse('pingpong:passkey_management'))
        assert response.status_code == 302
        assert '/accounts/login/' in response.url

    def test_passkey_management_shows_credentials(self, client):
        """Authenticated user sees their passkeys"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        # Create test credential
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Test Device"
        )

        response = client.get(reverse('pingpong:passkey_management'))
        assert response.status_code == 200
        assert 'Test Device' in response.content.decode()

    def test_delete_passkey(self, client):
        """User can delete their own passkey"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        credential = WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Test Device"
        )

        response = client.post(
            reverse('pingpong:passkey_management'),
            {'credential_id': credential.pk}
        )
        assert response.status_code == 302
        assert not WebAuthnCredential.objects.filter(pk=credential.pk).exists()

    def test_cannot_delete_other_user_passkey(self, client):
        """User cannot delete another user's passkey"""
        player1 = PlayerFactory(with_user=True)
        player2 = PlayerFactory(with_user=True)
        user1 = player1.user
        user2 = player2.user
        client.force_login(user1)

        credential = WebAuthnCredential.objects.create(
            user=user2,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Other Device"
        )

        response = client.post(
            reverse('pingpong:passkey_management'),
            {'credential_id': credential.pk}
        )
        # Should return 404 (get_object_or_404 with user filter)
        assert response.status_code == 404
        assert WebAuthnCredential.objects.filter(pk=credential.pk).exists()

    def test_passkey_management_shows_empty_state(self, client):
        """User with no passkeys sees empty state"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        response = client.get(reverse('pingpong:passkey_management'))
        assert response.status_code == 200
        assert 'No passkeys registered yet' in response.content.decode()

    def test_passkey_management_shows_multiple_credentials(self, client):
        """User can see multiple registered passkeys"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        # Create multiple credentials
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test1',
            public_key=b'pubkey1',
            name="Device 1"
        )
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test2',
            public_key=b'pubkey2',
            name="Device 2"
        )

        response = client.get(reverse('pingpong:passkey_management'))
        assert response.status_code == 200
        content = response.content.decode()
        assert 'Device 1' in content
        assert 'Device 2' in content
