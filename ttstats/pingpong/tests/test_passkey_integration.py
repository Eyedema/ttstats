import pytest
from django.urls import reverse
from .conftest import PlayerFactory


@pytest.mark.django_db
class TestPasskeyIntegration:
    def test_passkey_registration_page_loads(self, client):
        """User can access registration page"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        response = client.get(reverse('pingpong:passkey_management'))
        assert response.status_code == 200
        assert 'Register New Passkey' in response.content.decode()

    def test_login_page_shows_passkey_option(self, client):
        """Login page displays passkey button"""
        response = client.get(reverse('pingpong:login'))
        assert response.status_code == 200
        assert 'Login with Passkey' in response.content.decode()

    def test_password_login_still_works(self, client):
        """Traditional password login unaffected"""
        player = PlayerFactory(with_user=True)
        user = player.user
        user.username = "test"
        user.save()
        user.profile.email_verified = True
        user.profile.save()

        response = client.post(reverse('pingpong:login'), {
            'username': 'test',
            'password': 'testpass123'
        })
        assert response.status_code == 302
        assert response.url == reverse('pingpong:dashboard')

    def test_passkey_management_link_in_navigation(self, client):
        """Navigation includes passkey management link"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        response = client.get(reverse('pingpong:dashboard'))
        assert response.status_code == 200
        assert 'Passkeys' in response.content.decode()

    def test_unverified_user_can_manage_passkeys(self, client):
        """Unverified users can still manage passkeys (for future passwordless)"""
        player = PlayerFactory(with_user=True)
        user = player.user
        user.profile.email_verified = False
        user.profile.save()
        client.force_login(user)

        response = client.get(reverse('pingpong:passkey_management'))
        assert response.status_code == 200

    def test_passkey_management_accessible_from_any_page(self, client):
        """Passkey management URL is accessible from any authenticated context"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        # Access from different pages
        pages = [
            reverse('pingpong:dashboard'),
            reverse('pingpong:player_list'),
            reverse('pingpong:match_list'),
        ]

        for page in pages:
            client.get(page)  # Navigate to page
            response = client.get(reverse('pingpong:passkey_management'))
            assert response.status_code == 200
