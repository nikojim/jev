---
name: billing-refunds
description: Policy for duplicate charges, refunds, invoices and failed payments.
triggers: [charged, charge, refund, invoice, billing, bill, payment, "credit card", cobro, factura, reembolso, pago, vat, tax]
---
- Duplicate charges: urgency high, next step "Verify in payment processor and refund the duplicate". The reply confirms that the refund takes 5–10 business days.
- Pro-rated refunds apply only to cancellations within the first 7 days of a billing cycle. Mention this in the reply when relevant.
- Failed payment that blocks access for an enterprise account: urgency critical, escalate, next step "Grant 72h grace period while payment is retried".
- Invoice copies, VAT details and tax-exemption certificates: urgency low.
- If the ticket mixes billing with login problems or unauthorized changes, lower your confidence and mention the other team in the next steps.
