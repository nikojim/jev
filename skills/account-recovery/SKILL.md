---
name: account-recovery
description: Login, password reset, MFA, SSO and ownership-transfer procedures.
triggers: [password, login, "log in", "sign in", mfa, 2fa, "two-factor", sso, saml, locked, owner, contraseña, "iniciar sesión", bloqueado]
---
- Route to **Account & Access** unless there is a sign of unauthorized access (then Security & Compliance).
- SSO/SAML misconfiguration that locks out a whole organization: critical.
- The only admin is locked out (lost MFA device): high. Identity must be verified before access is restored, so the reply explains the verification process and must not promise immediate access.
- Password reset email not arriving: medium. Next steps: check the email suppression list and bounce logs.
- Ownership transfer: medium. It requires written confirmation from a company domain or a current billing contact.
