# Lifecycle and metering contract

## Scope and confidence

Phase 2 integrates observed **allocated capacity**, with no prices or monetary calculations. Historical quantities come from lifecycle periods and final usage records. Current inventory remains independent.

Every resource is keyed by cloud UUID, resource type and OpenStack UUID. First Phase 2 discovery starts a `BASELINE` period at observation time, even for a resource created years earlier. OpenStack creation/update times are retained as references. No pre-baseline allocation or usage is invented. Later periods have `OBSERVED` confidence; polling still approximates transition time.

The lifecycle fingerprint includes owning project, status, flavor identity and CPU/RAM/root/ephemeral/boot dimensions, or volume size/type. An unchanged poll updates the resource head's freshness without another period. A change closes the old period and creates the successor in the same inventory transaction. Stored old allocations are never replaced with the latest flavor or volume size. If the same known flavor temporarily becomes unavailable, known values may be retained with quality flags; a different unknown flavor never inherits them.

Periods use `[valid_from, valid_to)`. One open period is permitted per resource, and PostgreSQL rejects any overlap. Non-positive or out-of-order transitions roll back that service's transaction with `lifecycle_inconsistency`. Open periods can only be closed; closed periods are immutable.

## Disappearance and reappearance

Only a fully consumed, successful service inventory with no malformed/conflicting rows advances absence reconciliation. API failures, partial pagination and rejected rows cannot confirm disappearance. Default `RESOURCE_MISSING_CONFIRMATION_COUNT=3` (minimum 2; legacy `MISSING_SCAN_THRESHOLD` accepted).

The first absence records `missing_since`; the allocation remains open and current inventory remains counted under its current policy. At the threshold, current inventory excludes the resource, the lifecycle closes at **first missing**, and separate `closed_at` / `deletion_confirmed_at` fields record when that decision was made. This is inferred disappearance, not an OpenStack deletion timestamp. A pending reappearance clears absence without splitting the interval. A reappearance after confirmation starts a new period; the gap and closed history remain intact.

Explicit instance deletion closes immediately. A supplied deletion time is used only if it is strictly after the period start, at or after the latest sighting, and no later than the current observation. Otherwise the observed deletion time is used. Generic `updated_at` is not used to date resizes. Volumes absent from Cinder follow repeated-scan confirmation; transitional `deleting` state follows policy until disappearance is confirmed.

## Seven meters

| Meter | Allocated quantity | Usage unit | Summary field |
| --- | --- | --- | --- |
| `compute.instance` | 1 | instance-hour | `instance_hours` |
| `compute.vcpu` | vCPUs | vCPU-hour | `vcpu_hours` |
| `compute.ram` | RAM MiB / 1024 | GiB-hour | `ram_gib_hours` |
| `compute.root_disk` | Nova local root GiB | GiB-hour | `root_disk_gib_hours` |
| `compute.ephemeral_disk` | Nova ephemeral GiB | GiB-hour | `ephemeral_disk_gib_hours` |
| `storage.volume` | 1 | volume-hour | `volume_hours` |
| `storage.volume_capacity` | Cinder allocated GiB | GiB-hour | `volume_gib_hours` |

Volume-boot VMs have zero Nova root allocation. Their boot volume is counted once through Cinder. An arbitrary volume attachment does not prove volume boot. Unknown capacities remain null and their meters are excluded with quality issues; known dimensions still contribute. No disk utilization, thin provisioning, swap, snapshots or network usage is inferred.

## Versioned status policy

`config/metering.yaml` is separate from the existing `config/billing.yaml` current policy. Its shipped technical default is:

| Instance state | Instance / CPU / RAM / root / ephemeral |
| --- | --- |
| ACTIVE, SHUTOFF, PAUSED, SUSPENDED, RESCUE, SHELVED | all counted |
| SHELVED_OFFLOADED | root only |
| ERROR, DELETED, SOFT_DELETED | all excluded |

Cinder counts `available`, `in-use`, `reserved`, `attaching`, `detaching`, `extending`; explicitly excludes `error`, `error_deleting`, `deleting`, `creating`. Unrecognized states are excluded and reported, rather than silently choosing a billing rule. These are configurable technical assumptions, not provider pricing commitments. Unknown-state/capacity metering can be `PARTIAL` while persisting known quantities.

Each cloud/calculation version binds an immutable policy JSON snapshot and SHA-256 fingerprint. Changing a bound policy causes a conflict. Set a new version such as `meter-v2`, restart, and run metering for the desired range. Older records and policies stay readable. Formula changes also require a new calculation version and compatibility with existing versions' calculations. A future formula migration must preserve its old evaluator when historical clipped/provisional reports need it.

## Time, integration and rounding

All persistence uses UTC instants. Aware ISO timestamps are required; no implicit server/browser timezone interpretation is accepted. Ranges are start-inclusive/end-exclusive, positive, and limited to 3660 days. Reports clamp a future end to their current cutoff; a wholly future selection has zero usage.

For each interval intersecting the range:

```text
effective_start = max(period_start, requested_start)
effective_end   = min(period_end or now, requested_end, now)
seconds         = max(0, effective_end - effective_start)
usage           = allocated_quantity × Decimal(seconds) / Decimal(3600)
```

Elapsed seconds include microseconds and are constructed from integer timedelta components, with no floating-point `total_seconds()`. Integration uses Decimal precision 50. Ledger quantities use `NUMERIC(30,12)` and half-even rounding; JSON decimals are strings. Displayed aggregate rounding may differ from summing independently rounded lines by a last decimal unit. Clipped reports recompute capacity × exact clipped duration and retain the source record ID; `/records/{id}` returns the original canonical stored record.

Examples: 4 vCPUs for 2 hours = 8 vCPU-hours; 8 GiB RAM for 2 hours = 16 GiB-hours; 50 GiB root for 2 hours = 100 GiB-hours. A resize from 2 to 4 vCPUs after 2 hours, followed by 3 more hours, yields `2×2 + 4×3 = 16`. A volume of 100 GiB for 4 hours then 200 GiB for 2 hours yields 800 GiB-hours.

Final ledger segments split at UTC midnight, independently of query filters. Calendar reports split at local midnight in the selected IANA timezone (default `Asia/Ho_Chi_Minh`). DST days therefore correctly have 23 or 25 hours where applicable. The calendar-range endpoint converts date inputs using the chosen timezone, not the browser locale.

## Final records, provisional usage and replay

Only closed periods generate stored `FINAL` usage. Open periods generate response-only `PROVISIONAL / OPEN_PERIOD` usage through the cutoff. Closed periods awaiting a run generate `PROVISIONAL / AWAITING_METERING` responses. Final and provisional sources are mutually exclusive for a period/version; policy-excluded or unknown meters produce no invented final values.

Incremental runs use the version watermark against **closure recording time `closed_at`**, with an inclusive boundary. This catches a deletion confirmed today whose effective first-missing time was yesterday. A successful transaction inserts missing segments and advances the watermark together; failure rolls both back and retains a failed run. Abandoned runs are recovered after the next lock acquisition.

Segment identity is `(source_state_period_id, meter_name, period_start, period_end, calculation_version)`. Both the process lock and cloud PostgreSQL advisory lock serialize sync/metering; the database unique constraint independently rejects duplicate segments. Each persisted record has a stable UUID, source period, owner, unit, capacity, seconds, quantity, version and creating run UUID.

Range replay selects closed periods intersecting the range and generates **their full canonical segments**, which can extend outside the requested range. Reports still clip to their range. This deliberate rule keeps identities stable across overlapping or differently aligned replay requests. `force` requires explicit bounds and means nondestructive replay: existing records are reused, never deleted or overwritten. A range run does not advance the incremental watermark. Dashboard replay is cloud-wide; its project/resource display filters do not restrict the run.

## API and operations

All paths below are under `/api/v1/metering`; Swagger at `/docs` supplies exact schemas.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/summary` | Total, final/provisional and daily usage, quality flags |
| GET | `/projects` | Paginated project historical totals |
| GET | `/projects/{project_id}` | One project's historical totals |
| GET | `/projects/{project_id}/resources` | Resource consumption ranking |
| GET | `/resources/{resource_type}/{resource_id}` | Latest lifecycle allocation |
| GET | `/resources/{resource_type}/{resource_id}/lifecycle` | Paginated immutable periods and evidence |
| GET | `/usage`, `/resources/{resource_type}/{resource_id}/usage` | Clipped usage audit lines |
| GET | `/records/{usage_record_id}` | Original final ledger record |
| GET | `/observations/{observation_id}` | Normalized source observation |
| GET | `/runs` | Metering run outcomes and counts |
| POST | `/run` | Incremental run, or `{start,end,force}` replay |
| GET | `/meters` | Registry, policy versions and UI defaults |
| GET | `/calendar-range` | Convert local date bounds to UTC |
| GET | `/quality` | Unknown states, absence, failed/partial runs and overlap checks |

Historical range endpoints accept `start`, `end`, `timezone`, `calculation_version`, and applicable `cloud_id`, `project_id`, `resource_type`, `resource_id`, `meter_name` filters. A resource ID requires its type. Lists paginate with `limit`/`offset`. Writes require JSON content type; busy/conflicting version returns 409. This POC has no login; retain loopback isolation or an authenticated proxy.

CLI: `python -m app.cli metering run`, optionally `--from TIMESTAMP --to TIMESTAMP --force`; `python -m app.cli lifecycle inspect INSTANCE UUID`; `python -m app.cli sync run`. CLI and HTTP share engine logic and the database lock. Automatic metering runs after sync when enabled.

## Operational limits and Phase 3 boundary

Polling cannot see resources created and deleted between scans, distinguish an invisible resource from a deleted one, or identify an exact resize between observations. Outages lengthen uncertainty. Confirmation delays can revise earlier **provisional** usage; final history is never silently revised. Late evidence requiring a correction needs a future explicit correction/version mechanism.

Long-running unchanged open periods remain provisional until a real lifecycle closure. Phase 2 does not finalize billing cycles for these periods. Phase 3 must design an explicit cutoff/finalization contract before rating all monthly allocation. It should consume final `usage_records` under one chosen calculation version and deduplicate by record UUID; it should not query Nova/Cinder or reimplement lifecycle logic. No catalog, price book, monetary charge or invoice module is implemented here.

POC reporting materializes intersecting periods/records before pagination. Large histories need bounded query batches and scale testing. PostgreSQL triggers protect normal database writes, not a privileged administrator disabling constraints. Retention, authentication, distributed fencing, late corrections and notification consumers remain outside this phase.
