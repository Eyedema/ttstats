---
paths:
  - "ttstats/pingpong/templates/pingpong/passkey_management.html"
  - "ttstats/pingpong/templates/registration/**"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/admin.py"
---

# Passkeys

- Optional WebAuthn/FIDO2 via django-otp + django-otp-webauthn.
- **Use localhost, not 127.0.0.1**, in dev; WebAuthn rejects IP addresses.
  HTTPS is required in production.
- Required button IDs: `passkey-register-button`,
  `passkey-register-status-message`, `passkey-registration-placeholder`,
  `passkey-verification-button`, `passkey-verification-status-message`,
  `passkey-verification-placeholder`.
- The template must include `<template id="...-available-template">` and
  `<template id="...-unavailable-template">`.
- `PasskeyManagementView` handles a missing `django_otp_webauthn` with try/except.
- Admin inline: staff can view and delete passkeys but cannot add them.
