---
name: outage-response
description: Urgency rules for errors, outages, crashes and integration failures.
triggers: ["500", "502", "503", "is down", "went down", "checkout is down", outage, crash, crashes, error, timeout, "not working", "nothing works", webhook, sync, caído, falla, "no funciona"]
---
- Production outage, or a checkout/payment flow that is down: urgency critical, escalate, team Technical Support.
- Severity: production outage or checkout down ≥ 90; integration failure blocking order processing 65–80; a single user blocked 40–60; cosmetic issue < 10.
- Enterprise customer whose integration/sync is failing and orders or data are not being processed: high.
- A single user blocked with no workaround: high. A workaround exists: medium. Cosmetic issue: low.
- Next steps should ask for (or reference) the request id, timestamp (UTC), affected endpoint and app version when they are missing.
- If the ticket is too vague to diagnose (e.g. "nothing works"), keep confidence at or below 0.6 and make the first next step "Contact customer to gather details".
