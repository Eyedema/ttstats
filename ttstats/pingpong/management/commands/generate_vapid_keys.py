"""Print a VAPID keypair for web push.

    python manage.py generate_vapid_keys

Run once per deployment and put the two values in the environment. The
keypair is the server's identity to every push service, so rotating it
invalidates every existing subscription -- every user has to re-enable
notifications on every device. Generate once, keep the private key safe.
"""

import base64

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand


def _b64(raw):
    """base64url without padding, which is what the Web Push spec uses."""
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


class Command(BaseCommand):
    help = "Generate a VAPID keypair for web push notifications"

    def handle(self, *args, **options):
        from py_vapid import Vapid01

        vapid = Vapid01()
        vapid.generate_keys()

        private_value = vapid.private_key.private_numbers().private_value
        private_key = _b64(private_value.to_bytes(32, 'big'))
        public_key = _b64(
            vapid.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )

        self.stdout.write(self.style.SUCCESS("VAPID keypair generated.\n"))
        self.stdout.write("Add these to the production environment:\n\n")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public_key}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private_key}")
        self.stdout.write("VAPID_ADMIN_EMAIL=mailto:you@example.com\n")
        self.stdout.write(
            self.style.WARNING(
                "\nThe public key is handed to browsers and is not a secret. "
                "The private key is. Rotating this pair unsubscribes every "
                "device, so generate it once and keep it."
            )
        )
