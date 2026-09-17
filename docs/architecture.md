# Architecture and correctness boundaries

## Deployment and flow

```text
Existing OpenStack cloud
  Keystone          Nova             Cinder
       \              |               /
        └──────── openstacksdk ───────┘
                       ↓
             Collector (read-only)
        Complete paginated batch per service
                       ↓
                  Normalizer
          UUIDs, UTC, dimensions, quality flags
                       ↓
                  PostgreSQL
       Current rows + sync runs + change observations
                       ↓
              Lifecycle state periods
                       ↓
          Versioned resource-hour metering
                       ↓
             Immutable usage_records
                       ↓
       Pricing resolver + batched rating engine
                       ↓
       Charge records + financial audit snapshots
                       ↓
             Policy-based aggregation
                       ↓
                FastAPI /api/v1
                       ↓
            Same-origin browser dashboard
```

Two Compose containers: `billing-app` and `postgres`. The application is a modular monolith. No separate worker deployment or broker is necessary for one scheduler and one collector. Alembic runs before Uvicorn; PostgreSQL health gates startup. Persistent database storage outlives containers.

## Modules

| Module | Responsibility |
| --- | --- |
| `core` | Environment settings and structured safe logging |
| `openstack` | Authentication/SDK boundary and pure field normalization |
| `sync` | Scheduling, mutual exclusion, per-service transactions, reconciliation, observations |
| `models`, `db`, `migrations` | Relational schema, sessions, explicit schema evolution |
| `billing` | Configurable current-quantity policy and separate SQL aggregates |
| `lifecycle` | Billable change detection, baseline periods, safe closure and disappearance evidence |
| `metering` | Strict versioned policy, meter registry, Decimal integration, canonical ledger and historical queries |
| `pricing` | Database catalog, effective-dated resolver, admin validation, immutable configuration and audit |
| `rating` | Batched final usage rating, Decimal money, supersession, currency-separated cost reports |
| `schemas`, `api` | Versioned public representations, validation, pagination, health |
| `static` | Dashboard, project detail, manual sync and safe DOM rendering |

The `InventoryClient` protocol and injected client factory make OpenStack interactions mockable. A repository wrapper over every SQLAlchemy operation would add little value at this scale, so queries stay next to the service/API logic that owns them.

## Identity and relational model

- `clouds`: stable application-assigned cloud UUID, display name/region, connection and service freshness. No credentials or endpoint URLs are stored.
- `projects`: composite primary key `(cloud_id, project_id)`, current name/domain/enabled state and discovery/reconciliation timestamps. OpenStack UUIDs may arrive without hyphens; normalization canonicalizes them. Names never determine identity.
- `instances`, `volumes`: cloud/resource composite primary keys and composite foreign keys to the owning project. Flavor IDs are opaque strings because OpenStack supports non-UUID flavor identifiers; flavor names are descriptive only.
- `sync_runs`: application-generated UUID, timestamps, outcome, distinct discovered counts, service statistics and safe errors.
- `resource_observations`: cloud/resource/run UUIDs, event time/type and normalized JSONB snapshot. Resource snapshots deduplicate billable state; project observations still append each poll. Current rows are idempotent updates under the cloud lock.
- `resource_lifecycle_heads`: indexed mutable pointer and last-observed state for each resource.
- `resource_state_periods`: half-open allocation intervals, source observations, baseline confidence and closure evidence. Open bounds may be closed once; closed periods cannot be edited or deleted in PostgreSQL.
- `metering_policy_versions`: immutable policy snapshot/hash per cloud/version, with mutable processing watermark.
- `metering_runs`: outcomes, counts, safe errors and checkpoint audit.
- `usage_records`: immutable final meter quantities with unique source-period/meter/UTC-segment/version identity.

OpenStack creation/update/deletion times are separate from billing database insertion/update and observation times. PostgreSQL `timestamptz` columns store instants; application-generated timestamps and JSON/API times use UTC. RAM GiB is derived from normalized integer RAM MiB. Unknown dimensions stay null.

The schema uses JSONB on PostgreSQL, with a portable JSON type only for the fast SQLite test suite. Alembic's initial revision is explicit and independent of live ORM metadata. Migration parity and actual PostgreSQL behavior are tested.

## Sync lifecycle and failure isolation

1. Manual or scheduled invocation acquires a nonblocking local lock.
2. A dedicated PostgreSQL connection acquires the cloud's session advisory lock. Losing contenders return HTTP 409 (scheduled attempts simply skip).
3. With the lock held, abandoned runs are recovered and a new `RUNNING` record is committed.
4. Keystone authentication runs using a bounded-timeout auth session. Missing/bad authentication produces a safe failure record and preserves inventory.
5. Keystone, Nova and Cinder are collected independently. The entire generator must finish before that service is stored. A late-page failure discards the batch, never interpreting unseen pages as deletions.
6. Normalization deduplicates resource UUIDs, rejects malformed records, flags unknown fields, and uses embedded flavor dimensions where available. Per-run flavor lookups are cached, including failures.
7. Each valid service batch commits inventory, observations and lifecycle periods atomically. Malformed records disable that service's absence reconciliation for this run; other valid records are still saved.
8. Run/service status and cloud freshness are updated, then locks are released even on failure. Automatic incremental metering then acquires the same cloud lock. When explicit unlock fails, the physical connection is invalidated so a locked connection is not returned to the pool.

`SUCCESS` means every service batch completed without quality issues. `PARTIAL` means at least one service persisted, but a service failed or data quality was incomplete. `FAILED` means authentication failed, all service persistence failed, or an internal failure prevented completion. Authentication can be `CONNECTED` while the overall run is partial/failed: the status has a deliberately narrower meaning.

PostgreSQL uses repeatable-read transactions so project and quantity queries within an API request share a database view. Inventory from different services may have different observation times; cloud/service status exposes this. This phase does not promise an atomic snapshot across independent OpenStack services.

## Disappearance is evidence, not a deletion event

The first complete scan that omits a resource sets `missing_since` and increments `missing_scans`. The previous allocation remains countable. Once `MISSING_SCAN_THRESHOLD` complete absent scans have accumulated since the last sighting, `is_missing` excludes the row from current quantities. A failed or malformed scan leaves counters unchanged. Seeing the resource again resets all missing state.

The application never synthesizes an OpenStack deletion timestamp. Missing rows and absence observations remain for audit. At confirmation, lifecycle closes at the first missing observation while retaining the later confirmation time. Pending reappearance keeps the period open; reappearance after confirmation starts a new period with a gap. Explicit terminated timestamps can exclude known deleted instances immediately. Unknown projects receive placeholder UUID-keyed rows; project discovery later replaces descriptive fields without moving identity.

A permission reduction that silently narrows a successful list is indistinguishable from absence to this polling implementation. An operator must validate all-project visibility and scope stability. The repeated-scan threshold reduces transient mistakes but cannot solve that ambiguity.

## Quantity calculation and storage separation

Nova and Cinder aggregate independently, grouped by project UUID. Joining raw VM rows directly to raw volume rows would multiply both counts; separate aggregates avoid that failure.

- A counted VM contributes one instance and configured CPU/RAM/root/ephemeral dimensions.
- Policy can disable selected dimensions for a status without schema changes.
- An image-backed VM's root allocation comes from its flavor. An explicitly volume-backed VM contributes zero Nova root allocation, while its persistent volume contributes once to Cinder.
- An arbitrary attachment does not prove the boot device. Unknown boot source produces an unknown root dimension and a warning.
- Known quantities sum; unknown dimensions do not masquerade as zero in resource records. Summary completeness and pending-absence counters accompany numeric totals.
- Disabled/missing project discovery alone does not discard its independently observed resources. Project metadata and resource allocation are distinct facts.

## Inventory, lifecycle and metering boundaries

Current inventory still uses the Phase 1 SQL aggregates and `config/billing.yaml`. Historical reports integrate `resource_state_periods` and `usage_records`, never current allocation rows. Ownership, flavor, dimensions and status are frozen in each interval; a resize closes the old interval and opens a new one. Names do not define identity.

First discovery establishes a BASELINE at observation time. OpenStack creation is a reference timestamp and never supplies an assumed allocation start. Generic OpenStack `updated_at` does not date a resize reliably. A supplied explicit deletion timestamp is accepted only after the period start, at or after the last sighting, and no later than the current observation; otherwise closure uses observation time.

Metering shares the sync process gate and PostgreSQL advisory lock. It processes newly closed periods using **closed_at**, so late disappearance confirmation cannot be missed behind a watermark on effective `valid_to`. It splits complete closed periods into canonical UTC days, writes deterministic final rows, and advances the version watermark atomically. Unique constraints provide a second idempotency boundary. Range replay selects intersecting periods but still materializes their complete canonical segments.

Reports clip intervals to the requested range and current time, split by reporting-timezone calendar days, and expose separate final/provisional quantities. Open periods and unmetered closed periods are provisional only; they never insert changing ledger rows. The dashboard freezes one effective cutoff across a report's follow-up requests. Independent requests can still see database changes if a sync commits between them.

PostgreSQL `btree_gist` and a `[)` range exclusion constraint reject overlapping periods. Triggers reject changes/deletion of closed periods, changes/deletion of final usage, usage insertions outside their closed source period, and policy snapshot edits. SQLite is a fast development/test option and does not provide these PostgreSQL trigger protections.

Phase 3 reads `usage_records` by calculation version and stable UUID to produce monetary charge snapshots. Phase 4 consumes those charges for billing cycles and invoices. See [metering contract](metering.md) for formulas, rounding, replay, status policy and limitations.

## Phase 3 pricing and rating

Nine additive tables hold products, books, book versions, rules, assignments, overrides, audit entries, rating runs and charge records. Null-project assignments are effective-dated cloud defaults. Unique meter mapping, nonnegative NUMERIC prices and half-open effective periods make pricing resolution deterministic. PostgreSQL exclusion constraints protect active version, assignment, override and charge overlaps. Triggers preserve financial snapshots, active pricing definitions and audit rows.

Resolver precedence is project override → project assignment → cloud default. A selected book with a price gap does not fall back silently. The engine splits at applicable configuration boundaries, uses exact allocated duration and snapshotted unit price, and records UNRATED gaps. Currencies remain separate. A zero price is a valid charge of zero.

Rating processes keyset batches of final usage, caches pricing once per run, and isolates each usage replacement in a savepoint. Pricing writes share the cloud job lock with sync/metering/rating. Normal runs only fill new or UNRATED portions; force re-rating supersedes complete current sets for usage intersecting an explicit range. Earlier financial rows remain immutable and trace the replacing run. Batch commits support recovery without replaying the whole successful ledger.

Rating never queries current Nova/Cinder allocations or imports SDK resources. Cost reporting reads charge snapshots and original usage/version references; project names are descriptive metadata. It streams charge rows into currency-separated groupings and applies calendar reporting timezone boundaries. This is suitable for the POC; high-cardinality groupings still require scale work.

Only FINAL usage is monetized. Open periods are not projected or finalized. Phase 4 locks current RATED charges and blocks unresolved metering periods rather than inventing cutoffs. See [pricing](pricing.md) and [rating](rating.md) for the stable contracts and correction semantics.

## Security and operational tradeoffs

Credentials are explicit environment input to Keystone authentication only. API schemas exclude settings and raw payloads. Normalization never persists SDK metadata, user-data, administrator passwords, token material, fault bodies or arbitrary nested fields. Optional `raw_payload` retains only the same allowlisted normalized subset used by observations. This deliberately sacrifices full wire-payload troubleshooting to keep arbitrary secrets out of the database.

The static UI renders resource text using DOM `textContent`, avoiding HTML execution from resource names. No browser request goes to OpenStack. JSON content type is required for manual sync; no CORS policy enables cross-origin writes. A same-origin content security policy applies to dashboard pages. General public API authentication is deferred; loopback binding is the default isolation boundary. Phase 3 financial writes additionally require an explicit admin header, audit actor, and optional configured bearer token. No tokens are stored in pricing audit snapshots; actor labels are not verified identities.

The POC materializes service inventories and retains lifecycle observations indefinitely. Before larger-scale/production rollout, add tested retention, batch/stream collection with completeness markers, better lifecycle reconciliation, identity integration, dependency patch management, backup/restore practice and operational monitoring. Session advisory locking is appropriate for this deployment; strict fencing under network partitions would require additional design for a distributed production worker fleet.


## Phase 4 invoice boundary

`app/invoicing` consumes charge snapshots. It adds seven tables: billing_cycles, invoices,
invoice_lines, invoice_charge_links, billing_adjustments, billing_audit_log and
invoice_number_counters. It never queries SDK services or resolves historical prices.
Upstream rating/metering readiness contracts supply validation signals, not monetary amounts.

Cycle selection uses intersecting RATED charges of the cycle's metering version. Exact
stored amount prefixes allocate crossing charges without cumulative rounding loss. Drafts
group product/meter/unit price within one project/currency. Finalization freezes project
and product display data, lines and source links, assigns a row-locked counter number and
writes audit atomically. PostgreSQL excludes overlapping locked charge slices and guards
immutable financial records. All financial mutations share the existing cloud advisory lock.

Rating cannot supersede a charge with any finalized invoice link. It records
BILLED_CHARGE_CONFLICT; manual signed adjustments correct the billed financial outcome.
Applied adjustments are separate from the original snapshot; net totals are calculated
from immutable original totals plus the applied adjustment ledger. No tax/payment logic.

Billing mutation tokens are mandatory, separating operator and admin capabilities; reads
remain internal POC reads. No identity-provider rewrite or additional deployment service.
See [billing](billing.md), [invoice](invoice.md), and [adjustments](adjustments.md).


## Internal billing operational view

See [Internal billing](internal-billing.md). The existing domain boundaries remain intact. Persisted final charges remain authoritative; the internal dashboard separately estimates open usage with the existing resolver. INTERNAL-VND is an explicit effective-dated bootstrap, never an automatic historical price rewrite.
