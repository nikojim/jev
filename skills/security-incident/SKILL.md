---
name: security-incident
description: Triage suspected account compromise, phishing, vulnerability reports and privacy (GDPR/RGPD) requests.
triggers: [hacked, compromised, breach, phishing, suspicious, "wasn't me", unauthorized, vulnerability, xss, exploit, gdpr, rgpd, "personal data", "datos personales", soc 2]
priority: 10
---
- Route to **Security & Compliance** whenever there is any sign of unauthorized access, even if the ticket also mentions billing or login problems.
- Urgency:
  - critical: confirmed or likely account takeover, data exposure, or an actively exploited vulnerability.
  - high: phishing using our brand, a vulnerability report with no evidence it has been exploited, unexplained changes to the account.
  - medium: GDPR/RGPD access or deletion requests (legal deadline of 30 days).
  - low: compliance documents (SOC 2, DPA, security questionnaires).
- Always set `escalate: true` for critical and high.
- Severity: likely account takeover or data exposure ≥ 85; phishing using our brand or an unexploited vulnerability 60–80; GDPR requests and compliance documents ≤ 40.
- Reply rules: never ask the customer for their password or full card number. For a suspected takeover, tell them to reset their password and revoke sessions now. For vulnerability reports, thank the researcher and point them to a private disclosure channel.
- Next steps must include "Preserve logs" for any incident.
