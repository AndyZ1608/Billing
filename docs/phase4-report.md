# Phase 4 engineering report

## 1. Phase 4 architecture

The new `app/invoicing` module transforms charge snapshots into cycles, project/currency
invoices, grouped lines, source links and separate adjustments. Monetary calculations do
not call Nova/Cinder, calculate usage or resolve historical prices. Upstream readiness
contracts return completeness signals only.

## 2. Existing architecture preserved

The existing FastAPI/SQLAlchemy modular monolith, in-process job lock, PostgreSQL, static
JavaScript dashboard and two-service Docker Compose model remain. Inventory, collection,
lifecycle, metering, pricing and rating modules retain their public behavior. Rating adds
one financial lock check. No services, message brokers, payment systems or tax engine added.

## 3. Tables added

Seven tables: `billing_cycles`, `invoices`, `invoice_lines`, `invoice_charge_links`,
`billing_adjustments`, `billing_audit_log`, `invoice_number_counters`. Existing charge rows
are referenced, not destructively migrated or rewritten.

## 4. Alembic migration

`0004_billing_cycles_and_immutable_invoices.py` adds explicit frozen schema definitions,
indexes, constraints, a seeded number counter and PostgreSQL financial guards. The populated
Phase 3 upgrade test compares all pre-existing mapped tables before/after upgrade. Existing
Phase 2 → current migration tests also remain. Financial downgrade is destructive to new
Phase 4 records and is not an operational rollback strategy; restore a tested backup instead.

## 5. Cycle state machine

OPEN → CALCULATING → DRAFT → REVIEW → FINALIZED → CLOSED. Calculation is atomic across
projects. Explicit audited admin reopening is allowed only before any invoice is finalized.
There is no force administrative close. Manual timezone-aware cycles support monthly bounds.

## 6. Invoice state machine

DRAFT → REVIEW → FINALIZED. Draft regeneration replaces lines/links transactionally and
increments a version. Finalized invoices cannot regenerate, edit or delete through normal
APIs. VOID is reserved, not exposed as a workflow. Repeat finalization returns the same record.

## 7. Charge eligibility

Only RATED, intersecting `[start,end)` charges from the cycle's captured metering version.
UNRATED and SUPERSEDED are excluded; currencies produce separate invoices. Finalization also
blocks unpriced/missing usage coverage and unresolved open or unmetered source periods.
Future-end cycles cannot finalize. All cycle input is normalized to UTC before persistence.

## 8. Charge locking

Links store exact included period, amount, quantity, invoice and line. Finalization sets
`locked_at`. PostgreSQL excludes overlapping locked slices of the same charge. A deferred
constraint requires a finalized parent at commit. Any locked portion prevents whole-charge
supersession. The cloud job lock coordinates billing with rating, metering and collection.

## 9. Line grouping

Within a project/cycle/currency, lines group product identity/code, meter, unit and unit
price. Multiple prices remain separate. Zero lines are retained with a warning and complete
traceability. Amounts sum stored charge allocations, not recalculated display quantities.

## 10. Snapshots

Finalized headers preserve project name, period, timezone, currency and totals. Lines store
product name/code, meter, unit, quantity, price, amount and period. Immutable linked charges
preserve price-book/version/rule and original usage references. Current project renames do
not alter issued invoice display.

## 11. Numbering

`INV-YYYYMM-00000001`: local cycle-start month plus a globally monotonic counter. The
counter is seeded in migration and updated under a database row lock. Unique number and
invoice identity constraints, and counter anti-decrement protection prevent committed
number reuse. Rolled-back transactions issue nothing. No MAX+1 query is used.

## 12. Validation

A dedicated BillingValidationService returns INFO/WARNING/ERROR-shaped issues. ERROR blocks.
Checks cover period readiness, rating/metering completeness, provisional data, current
eligible-source set, nonempty lines, source grouping/interval/quantity/amount reconciliation,
currency, duplicate billing and finalized link locks. Original subtotal equals lines and
linked amounts; tax is zero. Expected domain failures return structured 409 responses.

## 13. Adjustments

Admin-created DRAFT → APPROVED → APPLIED records. Input is a positive Decimal-string
magnitude; CREDIT is stored negative and DEBIT positive. Reasons are mandatory. Only APPLIED
records affect `applied_adjustment_total` and `net_amount`. Original invoice totals/lines
remain immutable. Repeated apply is idempotent. There are no financial delete endpoints.

## 14. Re-rating conflicts

Force rating encountering a billed charge records BILLED_CHARGE_CONFLICT per usage, preserves
all old charges for that usage, and reports PARTIAL. Other unbilled usage may proceed. The
PostgreSQL trigger is an additional backstop. Operators reconcile through explicit adjustments;
there is no automatic delta-generation engine.

## 15. APIs

Billing summary, cycle list/create/detail/summary/calculate/review/reopen/finalize/close;
invoice list/filter/search/sort/detail/regenerate/review/finalize/lines/charges/audit;
adjustment list/create/approve/apply; billing quality. Read collections are paginated.
See [billing](billing.md), [invoice](invoice.md), [adjustments](adjustments.md) and `/docs`.
All monetary JSON values are decimal strings. Billing writes require configured operator/admin
tokens; no anonymous mutation fallback. Reads retain the existing internal POC boundary.

## 16. UI

Navigation adds Billing Cycles, Invoices and Adjustments. Cycle forms, generation/review/
finalization/close controls, summary blockers, invoice search/filter/sort, immutable draft/final
labels, source-charge drill-down, validation, audit and adjustment approval/application are
implemented. Data Quality includes billing readiness, reconciliation and rating-lock conflicts.

A full browser workflow on an isolated synthetic database generated two invoices totaling
238,000 VND (Project A 109,600; Project B 128,400), finalized both and closed the cycle.
A 1,000 VND credit left Project A's original total unchanged and produced net 108,600 VND.
The final cycle had zero unrated usage, zero provisional/unknown metering periods and zero
blocking issues. Test prices are not production prices.

## 17. Tests added

Tests cover draft identity/grouping/traceability, zero lines, multiple prices/currencies,
exact adjacent-cycle allocation including tiny rounding remainders, source corruption,
missing/unrated/provisional/open usage blockers, state transitions, atomic rollback,
project snapshots, credit/debit signs, idempotent adjustments/finalization, input validation,
API security, local-time boundaries after reload, source locks and billed re-rating conflicts.
Native PostgreSQL checks cover all-table populated migration preservation, immutable financial
records/audit/counter, overlap exclusion and rejection of a competing application operation.

## 18. Full test results

**110 passed, 0 skipped, 6 dependency deprecation warnings in 136.82 seconds**, including seven native PostgreSQL tests. All Phase 1–3 regression tests remain. Ruff lint and format checks pass across 70 Python files; pip dependency checks and syntax checks for all four JavaScript files pass. See [validation evidence](validation.md).

## 19. Deployment/migration evidence

Native PostgreSQL 16 migration, guard, locking and persistence tests run locally. A real
application-process restart preserved all 98 rows across eight synthetic financial tables,
including two finalized invoices, 14 lines, 31 links, one applied adjustment and 31 charges.
Ruff, formatting, dependency compatibility and JavaScript syntax were checked.

**Docker Compose migration/restart was not run: Docker is unavailable on this workstation.**
Compose remains the existing app/PostgreSQL pair with persistent volume. CI is updated to
start at 0003, upgrade via startup and inspect billing routes; that CI execution has not been
observed here. Native PostgreSQL/process tests are not a claim of Docker deployment success.
README includes a backup command using the actual postgres service and database variables.

## 20. Known limitations and concerns

- Finalization deliberately blocks long-running open allocations. Phase 2 has no calendar
  checkpoint contract; Phase 4 does not fabricate one. Draft billing is available, but this
  is not automatic month-end settlement for continuously open resources.
- Late data after partial invoice finalization blocks closing; no supplemental-invoice,
  automated reconciliation delta or force-close workflow is implemented.
- Only FINAL usage is persisted by Phase 3. Provisional configuration is a forward-facing
  policy hook; unresolved metering always remains a blocker.
- Shared-token capabilities are not per-user IAM or separation of duties. Production needs
  PostgreSQL and suitable authenticated network access; SQLite is demo/test-only.
- Calculation is atomic for the whole cycle; no optional partial billing_runs table.
  Grouping and per-invoice validation still retain selected keys/references in memory.
- No legal credit note, tax, payment, PDF, email, ERP or FX support. Upstream polling/history
  limitations and production scale/backup recovery testing remain relevant.

## 21. Recommended Phase 5

First design upstream period checkpoints with immutable usage generations and explicit
billing completeness watermarks for continuously allocated resources. Then add Customer /
Billing Account mapping and contracts, invoice documents/PDF, tax/VAT foundation, payment
status/reconciliation and notifications. Budgets/alerts can consume the existing charge
ledger. Future features must preserve usage → charge → invoice → adjustment traceability.
