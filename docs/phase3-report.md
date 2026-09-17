# Phase 3 engineering report

Implementation and local validation completed across 2026-09-15/16. Docker and live OpenStack deployment remain separate, unverified acceptance checks in this environment.

## 1. Existing architecture understood

The existing FastAPI/SQLAlchemy modular monolith uses a single application worker and PostgreSQL, deployed through two Compose services. The SDK collector maintains current inventory, change observations, immutable closed lifecycle periods and versioned final usage. Current quantities and historical metering remain independent. Baseline validation ran all 70 existing tests; it exposed a PostgreSQL session-timezone dependency. Setting UTC on each application connection resolved it, and all 70 baseline tests passed before Phase 3 validation.

## 2. Phase 3 architecture added

Added `app/pricing` for catalog, effective-dated configuration, resolver, audit and explicit bootstrap; `app/rating` for Decimal money, batched rating and cost reporting. Rating reads final `usage_records`, never Nova/Cinder allocations or SDK objects. Pricing writes and rating reuse the existing cloud lock key. Application metering completion can trigger incremental rating. The monolith and two-container model remain intact.

## 3. New database tables

Nine tables: `billing_products`, `price_books`, `price_book_versions`, `price_rules`, `project_price_book_assignments`, `project_price_overrides`, `pricing_audit_log`, `rating_runs`, `charge_records`. Existing usage receives an incremental scan index. Price-effective, project/resource/meter/time, currency/status, product and run indexes support resolution and audit queries.

## 4. Alembic migrations

Revision `0003` is additive. It does not update/delete Phase 1/2 inventory, observations, periods, usage or metering runs. A native PostgreSQL test created a populated Phase 2 database, upgraded it, compared all prior rows, bootstrapped/rated, and compared those rows again unchanged. PostgreSQL adds overlap exclusions, immutable pricing/audit guards, immutable financial snapshots and matching-usage guards. Schema/model parity and upgrade/downgrade/re-upgrade were checked. Downgrade drops Phase 3 financial tables and is inappropriate for valued records without a verified recovery plan.

## 5. Product Catalog design

Products are database entities with a unique code and unique Phase 2 meter mapping. Unit/category validation prevents inconsistent mappings. All seven Phase 2 meters can be priced. Optional seed definitions are confined to an explicit TEST bootstrap; the rating formula has no hard-coded price constants.

## 6. Price Book design

Reusable books hold code/name/description and one VND or USD currency. Effective-dated assignments select a project book; null-project assignments select the cloud default. There is no FX or mixed-currency scalar total. Book/product identities are immutable in this implementation.

## 7. Price versioning

Versions have `[effective_from,effective_to)` bounds and DRAFT → ACTIVE → RETIRED transitions. Only active versions resolve. Adding rules is limited to drafts; active rules/bounds cannot be edited. At least one enabled rule is required for activation, and active overlaps are rejected. Incomplete products/rules are visible as UNRATED rather than silently supplied. Corrections retire and replace definitions, then explicitly re-rate affected completed charges.

## 8. Pricing precedence

Project product override → effective project book → effective cloud default. A chosen project book with a gap does not fall back silently. Overrides must match a selected book's currency when one exists; mismatch remains UNRATED. Assignments and overrides reject ambiguous effective overlaps. Retirement preserves configuration history while removing it from new resolution.

## 9. Rating algorithm

Read final usage in keyset batches; cache pricing for the run; collect relevant boundaries; resolve segments; combine adjacent segments with identical pricing references; integrate allocated capacity over exact elapsed seconds; multiply by unit price. Missing prices create UNRATED segments. Persist each usage atomically using a savepoint, and commit batches. A bad usage marks the run partial without partially replacing its charge set or preventing other valid usage from completing.

## 10. Decimal / rounding policy

Decimal precision 50 throughout monetary integration. Quantities/durations use NUMERIC(30,12), prices NUMERIC(24,8), subtotals NUMERIC(38,8). A central half-even utility rounds stored subtotals to eight decimals; VND/USD display totals use zero/two decimals. Intermediate rated quantity is not rounded before multiplication. API decimals are strings, fractional JSON floats are rejected, and browser code does not calculate money with Number. See [rounding details](rating.md#decimal-and-rounding).

## 11. Charge model

Charge snapshots retain source usage UUID, owner/resource/meter, product/code/category, allocated/original/rated quantity, bounds/seconds, unit price/currency/subtotal, pricing source and references, effective pricing bounds, formula, rating version/run, FINAL source status and quality reason. Audit detail embeds the original usage. Historical display uses stored financial data, not current prices.

## 12. Re-rating model

Normal runs rate new usage and fill UNRATED portions only. Existing RATED segments remain unchanged, even when another part of the usage lacks pricing. Unchanged retries reuse their rows. Force requires explicit dates and replaces the complete set for each intersecting canonical usage record: old rows become SUPERSEDED with timestamp/replacement-run UUID, new rows are appended. Force start is audited. Unique/exclusion constraints prevent duplicate current charge coverage. Repeated force runs create intentional audit generations, not duplicate active totals.

## 13. APIs added

Pricing: product/book list/create/detail; version list/create/activate/retire; rules list/create; assignment/override list/create/retire; audit and admin configuration. Rating: run, runs, summary, projects/project detail, charges/charge detail and quality. Queries support ranges, project/resource/meter/product/status/currency and reporting timezone. Lists paginate. Financial writes require explicit administrator headers plus an optional bearer token. Full route tables are in [pricing](pricing.md#api) and [rating](rating.md#operations-and-api).

## 14. UI added

Costs page shows final-only totals, compute/storage amounts, unrated count, currency, last run, project/resource/meter rankings, daily/monthly breakdown and charge audit. Project pages include Cost links. Pricing UI supports books, drafts, rules, activation/retirement, assignments and overrides. Rating Runs exposes outcomes and administrative execution. Existing Data Quality now adds rating/pricing counts and sample issues. Current Inventory and Historical Usage remain available.

## 15. Tests added

New tests cover exact CPU/RAM charges, zero/missing price, price and assignment boundaries, override precedence, gaps, currency mismatch/separation, clipping, daily/monthly DST boundaries, money rounding, batching, versioned re-rating, normal retry preserving rated portions, transactional failure isolation, admin/token validation, API workflow and explicit bootstrap idempotency. PostgreSQL tests cover charge/rule immutability, locking, overlaps, populated Phase 2 upgrade preservation and reconnect persistence. The final zero-price case checks 100 instance-hours spanning UTC ledger days.

## 16. Full test results

**90 passed, 0 skipped, 6 dependency deprecation warnings in 57.67 seconds**, including six native PostgreSQL tests. The full suite includes every Phase 1/2 regression and the 100-instance-hour zero-price regression. Ruff lint/format checks passed across 62 Python files; dependency compatibility and all three JavaScript syntax checks passed. See [validation](validation.md) for the command and boundaries of this evidence.

Browser validation confirmed the synthetic total **57,800 VND**: compute **47,800**, storage **10,000**, zero unrated. Book/draft/rule/activation/assignment/override forms and charge audit worked. Browser force re-rating created 12 replacements while preserving the same total and recording the force run. CLI normal rating afterward found no new work. An application-process restart preserved all 12 original charges, 12 usage rows and two books exactly before the browser force replay.

## 17. Docker migration/deployment result

**Not run locally: Docker is not installed** (neither command nor standard executable path is present). Compose remains the original app/PostgreSQL pair with persistent volume. CI now starts from revision 0002, upgrades via normal application startup, checks inventory/history/cost/pricing/rating routes, explicitly seeds test pricing and checks it survives application-container restart. These new CI steps have not been observed executing here. Native PostgreSQL migration tests and local process-restart checks are evidence for those layers, not proof of Docker deployment success.

## 18. Known limitations

One configured cloud/region, one application worker and trusted POC access. The optional financial admin token is not general IAM; actor labels are not verified identities. SQLite is demo/test-only for money; PostgreSQL supplies exact NUMERIC and database guard protections. Pricing is linear unit pricing with no minimum fees, tax, FX, discounts or invoices. Only final usage is rated; open allocations are excluded until Phase 2 closes them. Report group cardinality and pricing catalogs are held in memory after streaming rows and need production scale testing. Very large values beyond NUMERIC capacity fail safely and require investigation.

## 19. Unrated/data-quality concerns

No prices are seeded automatically. A deployment without configured products/books will correctly produce UNRATED usage; operators must review it before treating totals as complete. Price coverage gaps, unknown products, missing books/rules, currency mismatches, invalid usage and partial/failed runs remain visible. Future-date completeness is not guaranteed by activation. The populated pricing demo has no unresolved usage; the preserved history-only demo has no implicit production price assignment. Polling/lifecycle accuracy limits from Phase 2 still apply. No real OpenStack credentials were supplied.

## 20. Recommended Phase 4

Consume current RATED `charge_records` with explicit currency and version selection. Add billing cycles, a finalization/cutoff contract for long-running open allocations, charge locking, invoice drafts/finalization and explicit adjustment/credit logic. Locked or invoiced charges must not be silently superseded. The future billing layer should not inspect Nova/Cinder, lifecycle transitions or raw metering logic.
