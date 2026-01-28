import pytest
from django.core import mail
from django.urls import reverse
from django_otp_webauthn.models import WebAuthnCredential
from .conftest import UserFactory, PlayerFactory


@pytest.mark.django_db
class TestPasskeyEmails:
    def test_registration_sends_email(self):
        """Email sent when passkey is registered"""
        user = UserFactory()

        # Create credential (triggers signal)
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Test Device"
        )

        # Check email sent
        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert email.subject == "New Passkey Registered - TTStats"
        assert user.email in email.to
        assert "Test Device" in email.body

    def test_deletion_sends_email(self, client):
        """Email sent when passkey is deleted"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        credential = WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Test Device"
        )

        # Clear the registration email
        mail.outbox.clear()

        # Delete credential
        client.post(
            reverse('pingpong:passkey_management'),
            {'credential_id': credential.pk}
        )

        # Check deletion email sent
        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert email.subject == "Passkey Removed - TTStats"
        assert user.email in email.to
        assert "Test Device" in email.body

    def test_email_contains_security_warning(self):
        """Emails include security warnings"""
        user = UserFactory()

        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Device"
        )

        email = mail.outbox[0]
        assert "didn't authorize" in email.body.lower()

    def test_registration_email_contains_passkey_url(self):
        """Registration email includes link to passkey management"""
        user = UserFactory()

        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Device"
        )

        email = mail.outbox[0]
        assert "/pingpong/passkeys/" in email.body

    def test_deletion_email_contains_passkey_url(self, client):
        """Deletion email includes link to passkey management"""
        player = PlayerFactory(with_user=True)
        user = player.user
        client.force_login(user)

        credential = WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Device"
        )

        mail.outbox.clear()

        client.post(
            reverse('pingpong:passkey_management'),
            {'credential_id': credential.pk}
        )

        email = mail.outbox[0]
        assert "/pingpong/passkeys/" in email.body

    def test_multiple_passkey_registrations_send_separate_emails(self):
        """Each passkey registration sends its own email"""
        user = UserFactory()

        # Register two passkeys
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

        # Should have two emails
        assert len(mail.outbox) == 2
        assert "Device 1" in mail.outbox[0].body
        assert "Device 2" in mail.outbox[1].body

    def test_email_html_version_contains_styling(self):
        """HTML email includes proper styling"""
        user = UserFactory()

        WebAuthnCredential.objects.create(
            user=user,
            credential_id=b'test123',
            public_key=b'pubkey',
            name="Device"
        )

        email = mail.outbox[0]
        # Check that HTML alternative exists and contains styling
        assert len(email.alternatives) > 0
        html_content = email.alternatives[0][0]
        assert '<html>' in html_content
        assert 'style=' in html_content
        assert 'Security Notice' in html_content
