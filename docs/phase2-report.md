# Phase 2 engineering report

Completed implementation and local validation on 2026-09-15. This report distinguishes implemented/tested behavior from deployment checks that could not run locally.

## 1. What changed from Phase 1

Added resource change observations, lifecycle periods, missing/deletion evidence, seven resource-hour meters, versioned immutable final usage, provisional reports, metering operations, historical APIs and dashboard pages. Existing current-quantity aggregation and APIs remain separate and covered by the existing tests. Resource observations now deduplicate unchanged billable state; project snapshots retain their original behavior.

## 2. New architecture

The same Python modular monolith and two-container deployment remain: OpenStack collector → normalizer → current inventory and lifecycle → metering → `usage_records` → FastAPI/dashboard. `app/lifecycle` and `app/metering` isolate the new logic. Sync and metering share local and PostgreSQL cloud locks. No broker, new service, pricing engine or notification consumer was added. See [architecture](architecture.md).

## 3. New database tables

Five tables: `resource_state_periods`, `resource_lifecycle_heads`, `metering_policy_versions`, `metering_runs`, `usage_records`. Periods preserve owner, status, allocation, confidence, source observation and closure evidence. Ledger records preserve source period, meter, unit, capacity, seconds, Decimal quantity, calculation version and producing run.

## 4. Alembic migrations

Revision `0002` adds the tables and observation hash/project and inventory deletion-confirmation columns. It preserves Phase 1 rows and observations and deliberately does not backfill allocation history. PostgreSQL gets one-open-period uniqueness, range exclusion via `btree_gist`, and immutability/source-bounds triggers. Upgrade preservation, schema parity and native constraints were tested. Downgrade removes Phase 2 history and is not a safe rollback for valued records; take a verified backup and stop concurrent writers before migration.

## 5. Lifecycle algorithm

Index current heads; hash billable attributes; ignore unchanged allocation for history; close the prior interval and insert a successor atomically on a change. First observation is a BASELINE at discovery time. Resize, volume extension, state/type and ownership changes cannot overwrite prior allocations. Same-flavor unavailable dimensions may retain known values with warnings; a different unknown flavor cannot. Invalid chronology rolls back the service transaction.

## 6. Missing/deletion algorithm

Only complete valid service lists advance missing counters. First absence remains pending; the configured threshold confirms disappearance and closes at first missing while recording later confirmation. Pending reappearance keeps one interval; confirmed reappearance starts a new interval after a gap. Explicit instance deletion uses a validated deletion timestamp or observation fallback. API outages and incomplete/malformed inventories do not imply deletion.

## 7. Meter definitions

`compute.instance`, `compute.vcpu`, `compute.ram`, `compute.root_disk`, `compute.ephemeral_disk`, `storage.volume`, `storage.volume_capacity`. Units are instance-hour, vCPU-hour, volume-hour and GiB-hour. Volume boot contributes zero Nova root; its persistent capacity belongs to Cinder. Registry and source fields are listed in [metering contract](metering.md#seven-meters).

## 8. State billing policy

Strict configurable per-meter Nova state rules and counted/excluded Cinder states live in `config/metering.yaml`. The shipped policy counts SHUTOFF allocations, counts only root for SHELVED_OFFLOADED, and excludes ERROR/deleted states. Unknown states/capacities are reported and excluded where unknown. Policy snapshots are immutable per cloud/version; policy changes require a new calculation version.

## 9. Usage formulas

Usage = capacity × exact elapsed seconds / 3600 over the intersection of the lifecycle and requested range, clamped to the current cutoff. Python Decimal handles microseconds with precision 50; the immutable ledger uses NUMERIC(30,12), half-even rounding and JSON decimal strings. UTC daily ledger segments have stable identities. Local calendar reports honor IANA timezones and DST. Open/unmetered periods are explicitly provisional and never mutate final records.

## 10. New API endpoints

Under `/api/v1/metering`: summary, projects, project summary/resources, resource detail/lifecycle/usage, general usage, final record and observation audit, runs, run POST, meter/policy registry, calendar range conversion and data quality. Range APIs validate aware dates, positive bounds, timezone, meter and identity filters; lists paginate. Exact endpoint table and CLI operations are in [metering documentation](metering.md#api-and-operations), with generated schemas at `/docs`.

## 11. New dashboard pages

Historical Usage has date/project/timezone/version filters, seven total/final/provisional cards, project/resource consumption and daily tables. Project tabs link inventory and history. Resource pages show lifecycle allocations and usage-to-observation audit links. Metering Runs supports manual execution; Data Quality reports absence, unknown states, failed/partial jobs and lifecycle overlaps. Report requests share a fixed effective time cutoff. Existing current inventory remains available.

## 12. Tests added

Added lifecycle baseline/deduplication, state/resize/ownership, volume extension/type/deletion, pending/confirmed reappearance, outage isolation, unavailable flavor, explicit deletion and chronology regressions. Added golden allocation-hour calculations, clipping, microseconds, UTC day and DST boundaries, future clamp, unknown states, policy versions, late-confirmation watermarks, replay/rollback/locking, API filtering/audit/calendar and volume-boot separation. Added actual Phase 1 upgrade preservation and native PostgreSQL immutable history/non-overlap/shared-lock checks.

## 13. Test results

**70 passed, 0 skipped, 6 dependency deprecation warnings** with native PostgreSQL enabled; latest run completed in 26.98 seconds. Ruff lint/format, dependency compatibility and both JavaScript syntax checks passed. CLI replay succeeded with 0 new / 12 reused final records; lifecycle inspection returned the expected three VM periods. Browser checks confirmed historical and project/resource views, 32 final vCPU-hours for the synthetic resize, replay idempotency, quality/runs pages, and the current inventory view. See [validation details](validation.md).

## 14. Docker deployment result

**Not executed locally: Docker is not installed.** Compose still defines `billing-app` and `postgres`, now with the metering policy mounted read-only. The existing CI workflow was extended to start Compose and check readiness, the registry and historical route after tests/build; this new CI execution has not been observed. Native PostgreSQL validation does not establish Docker deployment success. Run the documented `docker compose up -d --build` on a Docker-capable host before deployment acceptance.

## 15. Known limitations

POC with one configured cloud/region, no app login, no pricing or invoices. PostgreSQL provides production constraints that SQLite demos do not. Large report results are materialized before pagination and need scale testing. Open unchanged periods remain provisional until closure; this phase does not finalize monthly cycles. No automatic retention, privileged-admin tamper protection, or late-evidence adjustment workflow. Real OpenStack authentication and all-project visibility were not tested because no credentials were supplied.

## 16. Polling accuracy limitations

Resources created/deleted between polls can be invisible. Changes between observations are dated at observation, except validated explicit deletion evidence. Outages and silently restricted visibility increase uncertainty. First discovery does not prove historical allocation. Confirmed disappearance is inference; it can revise previously provisional totals back to first missing. No polling-derived value is presented as an exact provider event history.

## 17. Recommended Phase 3

Build a versioned pricing catalog, effective-dated price books and a rating engine consuming final `usage_records`. Select one calculation version, deduplicate on usage UUID, and store charge-to-usage audit references. Design a billing cutoff/finalization contract for long-running open allocations before rating complete months, and an explicit adjustment model for later evidence. Phase 3 should not understand Nova/Cinder lifecycle transitions or derive usage from current inventory.
