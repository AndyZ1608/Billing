# Phase 2 implementation plan and migration contract

The existing Phase 1 collector, inventory tables, aggregation and API stay in place.

## Additive schema (revision 0002)

- Extend observations with nullable owner/hash fields, preserving all old rows. New resource observations are change-oriented; unchanged polls update inventory/head freshness without adding another snapshot. Project observations keep their Phase 1 behavior.
- Add confirmation timestamps to instances/volumes; existing missing counters remain authoritative.
- Add `resource_state_periods`: stable allocation/state snapshots with `[valid_from, valid_to)` UTC bounds, source observations, confidence and closure evidence.
- Add `resource_lifecycle_heads`: one indexed cursor per cloud/resource, pointing at its latest period. No replay or full history scan on each sync.
- Add `metering_policy_versions`: immutable version-to-policy bindings and an incremental closed-period watermark.
- Add `metering_runs`: run audit, range, safe errors and counters.
- Add `usage_records`: append-only Decimal allocations, seconds and hours, linked to source period and metering run. Canonical records split at UTC midnight and are unique per source/meter/bounds/version.

No Phase 1 rows are deleted or automatically backfilled into fictitious history. The next successful resource observation establishes a Phase 2 BASELINE. OpenStack creation timestamps remain reference evidence rather than proof of historical allocation.

## Safety decisions

1. Sync updates inventory, observation and lifecycle inside the existing service transaction. Only open periods can be closed; closed allocation rows never change during sync.
2. Metering and sync share the cloud advisory lock. Closed-period queries use closure time, not effective end time, so retrospective disappearance confirmation cannot fall behind the watermark.
3. Only closed periods are persisted as final usage. Open periods and unmaterialized closed periods are computed provisionally for query intersections. They never close the ledger merely to render a report.
4. Re-metering replays complete canonical segments for intersecting closed periods. It inserts missing records without deleting or duplicating existing records. A changed policy requires an explicit new calculation version; queries select one version, never sum versions.
5. PostgreSQL enforces nonoverlap, one open period per resource, positive bounds and usage uniqueness. Triggers guard closed history and usage immutability. SQLite tests also exercise application interval checks.
6. Reporting uses IANA timezones for calendar buckets, while canonical storage stays UTC. Usage math uses Decimal and exact integer microseconds, never float.
7. Missing confirmation closes the open period at first missing, records confirmation time separately, and preserves an inferred gap on later reappearance. Failed service scans do not change lifecycle or absence evidence.

Validation will include all Phase 1 behavior, golden metering examples, resize and deletion histories, query clipping, timezone/DST boundaries, versioning, PostgreSQL migration safety and a browser smoke test. Docker will be tested if its runtime is available; otherwise that limitation will be reported explicitly.
