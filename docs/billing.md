# Billing cycles

Phase 4 consumes the Phase 3 charge ledger. It does not call OpenStack, calculate resource
usage or resolve prices. One invoice represents one cloud, project, cycle and currency.
Future billing accounts can replace the project grouping boundary without changing the
source-charge audit chain.

## Workflow and states

1. Complete collection, metering and rating. Review Data Quality.
2. Configure `BILLING_OPERATOR_TOKEN` and `BILLING_ADMIN_TOKEN` privately in `.env`.
3. Create an explicit cycle with timezone-aware `period_start` and `period_end`.
4. Calculate; inspect draft invoice lines, links and validation.
5. Move the cycle to REVIEW. This reviews its invoices atomically.
6. Finalize each invoice using the admin token.
7. Finalize the cycle once every required invoice is finalized, then close it.

`OPEN → CALCULATING → DRAFT → REVIEW → FINALIZED → CLOSED` is enforced by the service
and PostgreSQL transition guard. CALCULATING is an intermediate transactional state:
failed calculation rolls back the entire cycle calculation. All projects succeed together;
there is no optional partial billing-run table in this implementation. Structured logs
record run start/completion, while successful changes have persistent audit records.

An admin may explicitly reopen REVIEW → DRAFT **only before any invoice in the cycle
is finalized**. This action is audited and enables correction of stale drafts. There is
no arbitrary status PATCH, force close, or finalized-cycle reopen. Calculation can be
repeated in OPEN/DRAFT; invoice identity is stable and draft version increments.

## Periods and selection

Intervals use `[start,end)`. Aware input is normalized to UTC before persistence, including
SQLite demos. `BILLING_TIMEZONE` (default `Asia/Ho_Chi_Minh`) is snapshotted into each cycle
and invoice. September local midnight boundaries become August 31 17:00 UTC through
September 30 17:00 UTC. Create monthly cycles by supplying calendar-month bounds; there
is no recurring scheduler.

Only RATED charges intersecting the cycle are selected. SUPERSEDED and UNRATED are excluded.
The cycle captures `METERING_CALCULATION_VERSION`; rating-run references select that
version's charges. It cannot accidentally sum two metering generations. Multiple currencies
produce separate invoices. Zero-priced charges remain as zero lines with full traceability.

### Completeness is a hard gate

The rating completeness contract checks coverage for all relevant persisted usage, including
never-rated records and partial price gaps. A separate metering readiness contract reports
unresolved source periods and unknown capacity/state. Billing uses only those readiness
signals; invoice amounts still come solely from charges.

**An open billable lifecycle period blocks finalization even if it has no charge yet.**
Phase 2 persists only FINAL usage from closed periods; open/unmetered periods are provisional.
Phase 4 does not invent a cycle cutoff or change that upstream contract. Long-running unchanged
resources therefore require a future metering checkpoint design before automated month-end
billing is possible. Do not change resources artificially to make a cycle pass. Drafts remain
available and show validation blockers. This conservative behavior prevents issuing an
incomplete invoice that looks final.

`ALLOW_PROVISIONAL_IN_DRAFT=true` controls eligibility for any future persisted provisional
charges. `ALLOW_PROVISIONAL_IN_FINAL=false` blocks such charges. Current Phase 3 only persists
FINAL charges, and unresolved metering coverage always blocks, even if that flag is enabled.
Cycles whose period has not ended also cannot pass review/finalization.

## Exact allocation and charge locks

Cross-cycle charges are apportioned from the **stored subtotal**, never repriced. For a
boundary `t`, define `prefix(t) = money(subtotal × elapsed_to_t / total_duration)`.
An included amount is `prefix(end) − prefix(start)`. Adjacent slices sum exactly to the
original subtotal, including sub-unit rounding remainders. Quantity uses the same prefix
method at 12 decimals. All arithmetic uses Decimal with high precision; amount storage is
NUMERIC(38,8), using Phase 3 HALF_EVEN policy. Invoice lines sum these stored allocations.

`invoice_charge_links` stores charge, invoice, line, included interval, quantity, amount
and `locked_at`. PostgreSQL excludes overlapping locked intervals for the same charge,
allowing disjoint portions in adjacent cycles. A deferred constraint prohibits committing
locked links without a finalized invoice. Whole-charge re-rating is refused if **any**
portion is locked; correcting that charge requires reconciliation and an adjustment.

All API financial writes share the existing cloud advisory lock and process gate with
collection/metering/rating. Generation, finalization and adjustment actions each use one
transaction. Unique invoice identity, unique links, overlap exclusions and row-locked
numbering provide database backstops. Requests encountering concurrent work return 409.

## API and security

Read routes remain on the existing internal read boundary; deploy behind trusted network/
reverse-proxy access controls. Write calls require `Content-Type: application/json` and
`Authorization: Bearer <configured token>`. Empty tokens disable writes in every environment.
Operator tokens can create/calculate/review/regenerate. Admin tokens additionally finalize,
close, reopen and create/approve/apply adjustments. Actors are verified capability names
`billing-admin` / `billing-operator`; client actor labels cannot impersonate identities.
This is shared-token capability separation, not enterprise user-level RBAC.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/v1/billing/summary` | Original invoice totals separated by currency and status counts |
| GET/POST | `/api/v1/billing/cycles` | Paginated list / create |
| GET | `/api/v1/billing/cycles/{id}` | Cycle snapshot |
| GET | `/api/v1/billing/cycles/{id}/summary` | Counts, currency totals, completeness and blockers |
| POST | `/api/v1/billing/cycles/{id}/calculate` | Generate/regenerate all drafts atomically |
| POST | `/api/v1/billing/cycles/{id}/review` | Review all invoices |
| POST | `/api/v1/billing/cycles/{id}/reopen` | Audited admin correction before any finalization |
| POST | `/api/v1/billing/cycles/{id}/finalize` | Validate global completion |
| POST | `/api/v1/billing/cycles/{id}/close` | Administrative close after finalization |
| GET | `/api/v1/billing/quality` | Paginated cycles, reconciliation blockers and recent rerating conflicts |

Example creation (token omitted here):

```json
{"name":"September 2026","period_start":"2026-09-01T00:00:00+07:00","period_end":"2026-10-01T00:00:00+07:00"}
```

Financial validation returns HTTP 409 with `{code,message,issues}`; malformed input returns
422. `ERROR` blocks; `WARNING` (including zero lines) and `INFO` do not. No secrets enter audit
snapshots. Quality results cap detailed issue samples at 100 while retaining counts.

## Operational limits

PostgreSQL is required in production. SQLite is a synthetic demo/test backend without
PostgreSQL financial triggers or exact NUMERIC guarantees. Charge iteration and readiness
queries are batched; grouping keys and per-invoice validation references remain in memory.
Large installations need measured scale tests. New charges arriving after partial cycle
finalization block closing; this phase does not auto-create supplemental invoices or
reconciliation deltas. Applied adjustments are manual, explicit corrections. Polling gaps
and unknown upstream history remain visible limitations, not invented historical usage.
