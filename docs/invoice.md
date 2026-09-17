# Invoices and financial snapshots

An invoice belongs to one project, cycle and currency. States are `DRAFT → REVIEW → FINALIZED`.
`VOID` is reserved by the schema; no void/delete endpoint is provided. Drafts are clearly
marked **NOT FINAL**. There is no PAID/OVERDUE state or legal tax-invoice claim.

## Lines and sources

Grouping is product identity/code, meter, unit and exact unit price within the invoice's
project/currency. Distinct prices remain separate lines. Lines snapshot product name/code,
meter, unit, quantity, price, amount and period. Every line has source charge links;
line amount equals the sum of `included_amount` and quantity equals linked quantities.
The authoritative amount is a sum of charged amounts, not quantity times a rounded display
price. Zero amounts are deliberately retained for traceability.

The detail API and UI expose source links → full charge audit → original usage → lifecycle.
Price-book/version/rule references remain in immutable charge snapshots; invoice display
does not resolve current prices. The project name is refreshed and frozen at finalization.
Changing current project metadata cannot alter the historical invoice.

## Finalization

`BillingValidationService` checks:

- Ended billing period, complete rating and metering readiness.
- No UNRATED gaps, never-rated usage or unresolved open allocations.
- No forbidden provisional charges.
- Nonempty lines and the exact current eligible charge set (stale drafts must regenerate).
- Source identity/grouping, included intervals, quantity and monetary reconciliation.
- One currency, no overlapping charge portion already billed elsewhere.
- Locked source relationships for already-finalized records.

The admin finalization transaction validates, obtains a number, snapshots project name,
locks links, finalizes the header and writes audit. Failure rolls everything back.
Calling finalize again returns the same finalized invoice and number.

Number format: `INV-YYYYMM-00000001`, where YYYYMM is the cycle start in its snapshotted
timezone. A migration-seeded global counter is updated under a row lock plus the cloud job
lock. Numbers are unique and committed issued numbers cannot be reused or decremented.
A rolled-back transaction has issued no invoice; its counter increment rolls back as well.
There is no `MAX(number)+1` query. Never reset the counter or manually delete financial data.

PostgreSQL guards reject modifying/deleting finalized headers, adding/editing/deleting their
lines or links, deleting audit records and superseding locked charges. The application
exposes no arbitrary line/header mutation API. Draft regeneration replaces lines/links
atomically and keeps invoice identity/version. Only DRAFT invoices can regenerate.

## Totals

Original snapshot fields are immutable:

```text
subtotal = sum(usage lines)
adjustment_total = 0    # adjustments are separate in Phase 4
tax_total = 0
grand_total = subtotal
```

Effective read values add the adjustment ledger:

```text
original_total = grand_total
applied_adjustment_total = sum(APPLIED signed adjustments)
net_amount = original_total + applied_adjustment_total
```

These distinct names avoid changing the original issued total when a correction is approved.
No multi-currency total, VAT, fee, payment balance or FX calculation is performed.

## API

- `GET /api/v1/invoices`: project, cycle, status, currency, intersecting aware period bounds,
  project-name/invoice-number search, newest/oldest/amount sort, limit/offset.
- `GET /api/v1/invoices/{id}`: snapshot, effective totals and structured validation.
- `POST /api/v1/invoices/{id}/regenerate`, `/review`, `/finalize`.
- `GET /api/v1/invoices/{id}/lines`, `/charges` (optional `line_id`), `/audit`: paginated.
- `GET/POST /api/v1/invoices/{id}/adjustments`.

JSON read responses provide machine-readable export; there is no separate bulk export or
PDF generator. Preserve pagination when exporting source charges. The UI includes a cycle
list, invoice search/filter/sort, line-level source drill-down, adjustment workflow, immutable
snapshot labels and validation/audit sections.
