---
paths:
  - "ttstats/pingpong/templates/pingpong/passkey_management.html"
  - "ttstats/pingpong/views.py"
  - "ttstats/pingpong/admin.py"
  - "ttstats/templates/registration/**"
---

# Passkeys

### Passkey Authentication
- Optional WebAuthn/FIDO2 via django-otp + django-otp-webauthn
- **Use localhost, not 127.0.0.1** for dev — WebAuthn rejects IP addresses
- HTTPS required in production
- Required button IDs: `passkey-register-button`, `passkey-register-status-message`, `passkey-registration-placeholder`, `passkey-verification-button`, `passkey-verification-status-message`, `passkey-verification-placeholder`
- Template must include `<template id="...-available-template">` and `<template id="...-unavailable-template">` elements
- `PasskeyManagementView` handles missing `django_otp_webauthn` with try/except import
- Admin inline: staff can view/delete passkeys but cannot add them (security requirement)
