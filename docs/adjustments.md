# Credit and debit adjustments

Corrections never rewrite finalized invoice lines. An adjustment is a separate signed
financial record linked to a finalized invoice and its project/currency.

1. An admin creates DRAFT with type CREDIT or DEBIT, positive decimal-string magnitude,
   matching currency, nonblank reason_code and reason_text.
2. Admin approval records `approved_by` and `approved_at`.
3. Applying an APPROVED record sets APPLIED and `applied_at` atomically.

CREDIT is stored negative; DEBIT positive. An unapproved adjustment never affects net amount.
Repeated approve/apply to the existing target state is idempotent; there is no double posting.
Applying DRAFT or moving an APPLIED adjustment backwards is rejected. No delete/edit routes
are exposed. PostgreSQL freezes applied records and permits only the documented transitions.
Same-admin approval is allowed for this POC; there is no separation-of-duties IAM system.

Examples: original 1,000,000 VND plus CREDIT magnitude 100,000 gives applied adjustment
-100,000 and net 900,000. A separate DEBIT 50,000 gives net 950,000. Original subtotal and
invoice lines stay unchanged. A net credit balance is allowed; payment/refund handling is
outside this phase.

Routes:

- `GET /api/v1/adjustments`: paginated global view within the configured cloud.
- `GET/POST /api/v1/invoices/{id}/adjustments`.
- `POST /api/v1/adjustments/{id}/approve`.
- `POST /api/v1/adjustments/{id}/apply`.

Use the configured billing admin bearer token and JSON content type. Amounts must be decimal
strings (binary JSON floating-point amounts are rejected). Supported reasons:
BILLING_CORRECTION, SERVICE_CREDIT, MANUAL_CORRECTION, RESOURCE_DISPUTE, PRICING_CORRECTION, OTHER.
Every creation/approval/application has an immutable audit event with authenticated capability
actor and snapshots. Tokens are never stored in audit records.

A force rating run hitting any invoice-locked charge records `BILLED_CHARGE_CONFLICT`, leaves
that usage's old charges intact, and becomes PARTIAL. Other unbilled usage may still be rated.
There is no automatic delta/credit computation. Operators investigate the original charge
and create an explicit justified adjustment. Future credit/debit documents can consume these
records without changing the original invoice or the OpenStack/usage/pricing boundaries.
