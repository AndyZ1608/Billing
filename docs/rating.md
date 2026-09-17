## Current policy: ACTIVE-only with Nova notifications

The current default is meter-v2: only ACTIVE VM intervals are billable. Nova notifications provide the fast path; REST polling remains reconciliation. Prices are unchanged. Cinder follows attached ACTIVE intervals and is counted once. Earlier allocation-policy descriptions below describe retained historical behavior. See [setup, migration and policy details](nova-notifications.md) before upgrading.

# Rating and charge contract

## Boundary

```text
Final usage_records → pricing resolver → rating engine → charge_records
                                                        ↓
                                               Future billing engine
```

Rating reads immutable Phase 2 `usage_records` and pricing-domain configuration. It does not import OpenStack SDK objects or use current Nova/Cinder allocations to determine historical usage. Existing lifecycle and metering calculations remain independent.

Only persisted FINAL usage is rated. Phase 2 open/provisional usage remains outside `charge_records`; dashboard costs explicitly say **Final usage only**. An unchanged running VM may have no final usage/charges yet. This is not a current-month forecast, invoice or full finalized billing cycle.

## Calculation and splitting

The resolver splits usage at changes in effective overrides, project/default assignments and active price versions. Intersections use half-open intervals. For each segment:

```text
rated_start = max(usage_start, applicable_pricing_start)
rated_end   = min(usage_end, applicable_pricing_end)
duration_seconds = rated_end - rated_start
rated_quantity = allocated_quantity × duration_seconds / 3600
subtotal = rated_quantity × unit_price
```

The engine validates final usage status, nonnegative finite allocated capacity, positive elapsed time, consistent stored duration, and the Phase 2 rounded usage formula. Invalid source records are isolated, recorded in run errors, and remain visible as pending final usage for investigation. A normal run can complete PARTIAL while other valid usage is persisted.

Golden example: 4 vCPU from 10:00–14:00, price 1000 until 12:00 then 1500. Segments are `8 × 1000 = 8000` and `8 × 1500 = 12000`, total **20000 VND**. A project assignment change splits in exactly the same way. 4 vCPU for 5400 seconds is 6 vCPU-hour; at 2000 the charge is **12000 VND**.

Every gap remains a charge segment with status UNRATED, null subtotal and a reason. Reasons include NO_PRODUCT, NO_PRICE_BOOK, NO_PRICE_RULE, PRICE_GAP, CURRENCY_MISMATCH, INVALID_PRICE, INVALID_UNIT, INVALID_CURRENCY and OVERLAPPING_PRICE_CONFIG. Unknown currency is shown as UNSPECIFIED, not invented. A valid zero price yields RATED with zero subtotal.

## Decimal and rounding

All monetary arithmetic uses Python Decimal precision 50. Durations are calculated from integer days/seconds/microseconds, never binary floating-point `total_seconds()`. JSON prices must be integers or decimal strings; fractional JSON floats are rejected. Responses serialize Decimal values as strings, and JavaScript displays them without converting them to Number for calculation.

| Value | Persistence |
| --- | --- |
| Allocated/original/rated quantity, seconds | NUMERIC(30,12), compatible with Phase 2 |
| Unit price | NUMERIC(24,8) |
| Subtotal | NUMERIC(38,8), allowing up to 30 integral digits |

`app/rating/money.py` centralizes half-even subtotal rounding to eight decimal places. Subtotal calculation uses the unrounded intermediate rated quantity. The recorded rated quantity has twelve decimal places for audit. Display totals use whole VND or two-decimal USD, also half-even; stored subtotals retain eight places. A future rating algorithm/rounding change needs a new rating calculation version.

Full-segment cost queries return the stored subtotal. A clipped query computes the exact intersection from the **stored allocated quantity and snapshotted unit price**, then applies the same rounding. It never queries the latest price for a historical amount. The original stored charge remains available via detail. Summing independently rounded clipped/daily segments can differ by the last stored decimal unit; presentation rounding must not become the Phase 4 accounting source.

SQLite NUMERIC storage is not equivalent to PostgreSQL exact NUMERIC and lacks the PostgreSQL guard triggers. SQLite is for fast tests and synthetic walkthroughs. Use PostgreSQL for financial records, especially high-precision or large values.

## Charge snapshot and audit

Each `charge_records` row contains cloud/project/resource identity; usage UUID; product UUID/code/category; meter/unit; rated bounds/seconds; allocated, original usage and rated quantities; price/currency/subtotal; pricing source; book/version/rule/assignment/override references; effective pricing bounds; formula; rating version/run UUID; source_usage_status; status/reason and timestamps.

The detail endpoint includes the original immutable usage row. The UI links charge → original usage → lifecycle observation. Book version labels and product codes are snapshotted, and pricing identities/rules remain auditable. Financial fields never change after insertion. PostgreSQL permits only the transition to SUPERSEDED with a timestamp and replacement run reference; deletion and amount edits are rejected. Native constraints also validate matching usage scope/bounds and prevent overlapping current charge segments for a usage UUID.

`RATING_CALCULATION_VERSION` defaults to `rating-v1`. The implemented formula is duration-based linear pricing. Selecting a new rating version does not automatically replace charges; force re-rating is required for existing RATED usage. Normal retry can fill an old UNRATED segment using the configured version. Existing RATED segments remain stable.

Phase 2 can hold multiple metering calculation versions. Rating processes only the configured `METERING_CALCULATION_VERSION`, and cost reports filter by that usage version to prevent double counting alternative metering results. Charge detail remains available for older versions. Different currencies are never summed into one scalar amount.

## Idempotency, concurrency and transaction safety

Normal rating selects new usage or usage with UNRATED segments using an indexed anti-join. It does not scan all prior successful charges into Python. Usage is processed in ordered UUID batches (200 by default); configuration is loaded once as a run-local resolver snapshot. Per-usage existing charge reads are bounded; there is no per-segment pricing query.

Rating, sync, metering and pricing writes share the cloud PostgreSQL advisory lock key and application process gate. A concurrent operation returns 409 or a scheduled attempt skips. This is the existing single-cloud/single-worker deployment model, not distributed worker fencing.

Each usage is processed in a savepoint: resolve, split, supersede and insert either all succeed or all roll back. Batch commits retain successful progress. One bad usage adds a safe error (at most 100 detailed examples per run) and marks the run PARTIAL. A later fatal failure retains earlier committed batches and a FAILED run; retry is idempotent. Abandoned RUNNING markers become FAILED when the next job acquires the lock.

An active segment unique index and PostgreSQL overlap exclusion independently protect persistence. Normal retries preserve all existing RATED segments; only UNRATED portions can be superseded and retried. If an UNRATED result is unchanged, its UUID is reused without generating replacement history.

## Explicit re-rating

Administrative force re-rating requires start and end. It selects usage records intersecting the range and re-evaluates each **complete canonical usage interval**, which may extend beyond the requested bounds. This avoids combining an old partially rerated charge set with a new set using different pricing boundaries. The UI explains this scope. Project/resource/currency display filters do not restrict a run; runs operate on the configured cloud and metering version.

Old active rows become SUPERSEDED, retaining every financial field plus `superseded_at` and `superseded_by_rating_run_id`. New rows are RATED or UNRATED with the new run UUID. A force attempt is audited before work begins. Repeated force operations create intentional new audit generations, but only one current non-overlapping set remains. Normal repeated runs create no duplicate current charges.

Corrections to pricing/assignment configuration do not themselves mutate charges. A normal retry may fill existing gaps; changing already RATED amounts requires force. There is no deletion-based replay, credit note or adjustment/invoice engine in this phase.

## Operations and API

Automatic rating runs after metering in the application when `RATING_ENABLED=true`. With no prices, newly materialized usage is visible as UNRATED. Manual operations:

```bash
python -m app.cli rating run --actor operator
python -m app.cli rating run --from 2026-09-01T00:00:00Z --to 2026-10-01T00:00:00Z --force --actor finance-admin
```

The CLI reads the configured database. Schema changes remain Alembic's responsibility. Shell access is the CLI administration boundary.

Paths under `/api/v1/rating`:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/run` | Normal `{}` or `{start,end,force}` administrative run |
| GET | `/runs` | Paginated job status, counts, version and safe errors |
| GET | `/summary` | Separate currency totals, service/project/resource/product/meter, day/month breakdowns and last run |
| GET | `/projects`, `/projects/{project_id}` | Project costs grouped by currency |
| GET | `/charges` | Paginated charges, including clipped query values and original bounds |
| GET | `/charges/{id}` | Stored charge plus original usage audit |
| GET | `/quality` | Unrated reasons, pending usage, missing products/books, price gaps, overlap detection and failed runs |

Range endpoints require aware `start`/`end`, positive ranges up to 3660 days, and accept project UUID, resource UUID, `meter`, product UUID, status, currency and IANA timezone. Cost summaries always use the configured cloud and current metering calculation version. The default charge view excludes SUPERSEDED; select that status explicitly for prior-generation audit. Read endpoints need no pricing admin token in this POC; run POST uses the financial administration headers described in [pricing](pricing.md).

Reports stream intersecting charges in chunks, then retain grouping keys for totals and ranking; they do not persist every aggregate. Large cardinality reports still need scale testing and more database-side aggregation. The UI shows up to 50 resource/project rankings, while full group results and paginated charge detail are available in APIs.

Daily and monthly reports use local calendar days in `BILLING_TIMEZONE`, default Asia/Ho_Chi_Minh. DST boundaries use real UTC instants, including 23/25-hour days. Filtering clips usage to the query intersection and never includes an out-of-range full subtotal by accident.

## Data quality and Phase 4

Review UNRATED counts/reasons and pending final usage before treating totals as complete. A no-price total of zero is not evidence of free usage: its UNRATED segments remain visible. Provisional charges are always zero in count because open usage is intentionally excluded. Configuration overlap checks supplement API/database validation; priced gaps are detected where actual usage intersects them, not as a promise that every future calendar date has complete prices.

Phase 4 now consumes current RATED charges with explicit metering version and currency selection.
Finalized invoice links lock source charges, including partially billed intervals. Normal
rating skips already-rated usage; forced replacement raises a per-usage BILLED_CHARGE_CONFLICT
and records a PARTIAL rating run without superseding billed history. PostgreSQL also rejects
such updates. Operators reconcile corrections through explicit billing adjustments. See
[billing](billing.md) and [adjustments](adjustments.md). Open metering periods block invoice
finalization even though this rating engine deliberately persists no provisional charges.


## Internal billing operational view

See [Internal billing](internal-billing.md). The existing domain boundaries remain intact. Persisted final charges remain authoritative; the internal dashboard separately estimates open usage with the existing resolver. INTERNAL-VND is an explicit effective-dated bootstrap, never an automatic historical price rewrite.
