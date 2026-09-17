# Validation

## Phase 3 — 2026-09-16

Final full regression suite: **90 passed, 0 skipped, 6 dependency deprecation warnings in 57.67 seconds**. Native PostgreSQL 16.15 and Python 3.12.14 on Windows.

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://billing_test@127.0.0.1:55432/billing_test'
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.local/phase3-final --tb=short --show-capture=no
```

The 70-test Phase 1/2 baseline was run first. Its PostgreSQL UTC session test exposed a server-local-timezone dependency after restart; the application now explicitly selects UTC for PostgreSQL connections. All baseline tests then passed. Phase 3 adds 20 tests (18 rating cases including parameterized DST and six total native PostgreSQL cases across the complete suite).

Verified:

- Golden CPU/RAM charges, 100 instance-hours at zero price, missing pricing, price/assignment boundaries, override precedence, no fallback across a selected book's gap, currency separation/mismatch, query clipping, 23/25-hour calendar days and centralized half-even rounding.
- Normal idempotency, normal gap retries preserving already rated segments, explicit versioned re-rating with superseded history, batching, and rollback after an injected failure during replacement.
- Product/book/version/rule/assignment/override API workflow, activation validation, admin/token requirements, fractional-float rejection, price and timestamp validation, charge audit, quality and optional seed idempotency.
- Native PostgreSQL schema parity, financial/rule immutability, absence of overlapping configuration, shared job locking, charge persistence after connection disposal, and populated 0002 → 0003 upgrade preservation. Phase 1/2 rows were compared before migration, afterward and after rating, with no changes.
- Ruff lint and format passed for all 62 Python files. `pip check` found no broken requirements. Node syntax checks passed for `app.js`, `history.js` and `costs.js`.

Browser demo at port 18083 uses only synthetic data and TEST prices. Observed **57,800 VND** total, **47,800 compute**, **10,000 storage**, zero unrated. Verified creating a book, draft, rule, activation, future project assignment and override, and opening a charge with its original usage and pricing audit. The future-only browser fixtures do not change the demonstrated current-period total. Force re-rating via the UI produced 12 replacements with a successful audited run and preserved the same total. Current inventory/history remain available; rating quality cards and run pagination render correctly.

A process restart preserved all 12 original charges, 12 source usage records and two price books exactly (before the intentional force replay). CLI bootstrap rerun preserved POC-VND; CLI normal rating returned SUCCESS with zero new charges. Both the history-only demo and pricing demo passed readiness after upgrading/restarting.

**Not verified:** Docker is not installed (command and standard executable path checked). Compose migration/container restart tests could not execute locally. CI was extended to start at schema 0002, apply the normal startup migration, check financial routes and verify explicitly seeded pricing survives container restart; that CI run has not been observed. No real OpenStack credentials were supplied, so target-cloud authentication, endpoint permissions and all-project completeness remain unverified. Native PostgreSQL and local process restart evidence must not be presented as Docker deployment success.

See the [20-part Phase 3 report](phase3-report.md), [pricing operations](pricing.md) and [rating contract](rating.md).

## Phase 2 — 2026-09-15

Windows, Python 3.12.14, native PostgreSQL 16.15. Final command used the disposable local PostgreSQL database and a fresh temporary test directory:

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://billing_test@127.0.0.1:55432/billing_test'
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.local/phase2-final --tb=short
```

**70 passed, 0 skipped, 6 dependency deprecation warnings in 26.98s.** Four tests exercised native PostgreSQL, including immutable closed periods/usage, overlap exclusion, reconnect persistence, migration parity and advisory lock exclusion between sync and metering. The remaining tests include preserved Phase 1 behavior, migration of existing rows, lifecycle changes/disappearance, golden resource-hours, exact clipping, DST, replay, rollback, policy versioning and API audit/filter validation.

Ruff lint and format checks passed across app/migrations/tests/scripts. `pip check` found no broken requirements. Node syntax checks passed for both frontend scripts.

The synthetic browser demo at port 18082 was checked for historical totals, project consumption, resize periods, final/provisional audit lines, data quality and metering runs. Its first VM has 4 vCPUs from 10:00–12:00 UTC then 8 from 12:00–15:00, yielding 32 final vCPU-hours; its SHUTOFF period remains provisional. Re-metering through the browser reused 12 records and created none. CLI range replay independently reported SUCCESS, 0 created / 12 reused; CLI lifecycle inspection returned the three expected periods. The current dashboard remained functional with the demo's updated allocations (20 vCPU, 40 GiB RAM, 700 GiB Cinder). A screenshot inspection confirmed readable cards and range controls.

Docker is unavailable on this machine, so Compose deployment was **not run**. CI now contains build/start/readiness/historical-route smoke steps; those new steps have not been executed here. No real OpenStack credentials were supplied, so live cloud authentication, endpoint access and all-project completeness remain unverified. All demo data is synthetic. See [Phase 2 report](phase2-report.md) and [metering limits](metering.md#operational-limits-and-phase-3-boundary).

## Phase 1 validation retained for reference

Validated on Windows with Python 3.12.14 and PostgreSQL 16.15.

## Automated checks

- **40 pytest tests passed**, including the two native PostgreSQL integration tests.
- PostgreSQL: Alembic schema/model parity, JSONB, UTC observations, correct totals, idempotency, advisory lock exclusion across independent connection pools, and interrupted-run recovery.
- SQLite with foreign keys enabled: migration upgrade/downgrade, duplicate UUID rejection, project ownership constraints, service isolation and reconciliation, API responses/validation, status and dimension policies, normalization with actual SDK resource objects, and credential field exclusion.
- Ruff lint and format checks passed; Python dependency compatibility check passed; JavaScript syntax check passed.

## Browser verification

Ran the explicit synthetic demo on loopback port 18081 and verified:

- Overview: 2 projects, 3 instances, 14 vCPU, 28 GiB RAM, 60 GiB Nova root, 30 GiB ephemeral, 2 Cinder volumes / 600 GiB.
- Searching Project B reduced the project table to one row; sort controls remained functional.
- Project B detail displayed VM3 with 8 vCPUs / 16 GiB RAM, volume boot, zero Nova root disk, 20 GiB ephemeral, and its attached 500 GiB Cinder volume.
- Manual sync created a run in the displayed history without changing inventory totals.

## Not verified in this environment

- `docker compose up -d --build`: Docker was not installed. A CI workflow validates Compose configuration and builds the image on a Docker-capable runner.
- Authentication and inventory against a real OpenStack deployment: no cloud credentials were supplied. SDK signatures, authentication construction and real resource-object normalization were checked; network calls were mocked.
- Cloud-specific policy overrides and all-project visibility must be validated with the target administrator before using the totals operationally.

The PostgreSQL binary used here was a local test-only dependency; it is not part of the application or Docker build. The deployed application uses the Compose PostgreSQL service. No real OpenStack credentials or inventory were used.


## Phase 4 validation — 2026-09-16

Baseline before Phase 4: **90 passed**, including six native PostgreSQL tests.
Final full regression run: **110 passed, 0 skipped, 6 dependency deprecation warnings,
136.82 seconds**. PostgreSQL 16.15 on localhost, disposable per-test schemas; seven native
PostgreSQL tests included. Existing warning sources are Starlette/AnyIO and OpenStack SDK.

PowerShell command (test-only local database):

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://billing_test@127.0.0.1:55432/billing_test'
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.local/phase4-final --tb=short --show-capture=no
```

Checks: Ruff lint passed; 70 Python files formatted; `pip check` found no broken requirements;
Node syntax checks passed for app.js, history.js, costs.js and billing.js. Financial code
review found no binary-float money calculation or direct OpenStack SDK dependency.

PostgreSQL tests verify populated 0003 → 0004 preservation of **all prior mapped tables**,
metadata parity, finalized invoice/line/link and applied-adjustment immutability, financial
audit protection, anti-reuse counter, native overlapping-charge exclusion, shared advisory
lock conflict on a second application, numbering idempotency and locked charge rerating.
Existing Phase 1/2/3 database regressions continue to pass.

New tests also cover exact allocation/reconciliation, separate price/currency lines, zero
lines, adjustment signs and repeated apply, state transitions/rollback, actor/token boundaries,
invalid financial inputs, timezone input after database reload, unpriced/missing/provisional
usage and open-metering blockers. A browser-discovered SQLite timezone-normalization issue
was fixed and covered by a persistence regression.

Browser workflow used only isolated synthetic data on port 18084: local-day cycle,
calculation/review, two finalized project invoices (109,600 and 128,400 VND), 1,000 VND applied
credit, cycle finalization and close. Original totals stayed unchanged; Project A net was
108,600 VND. Final cycle validation reported zero blocking issues, zero unrated usage and
zero provisional/unknown metering periods. Source links and audit views were inspected.

A real application-process stop/start preserved every row across eight financial tables:
1 cycle, 2 invoices, 14 lines, 31 charge links, 1 applied adjustment, 17 audit events,
1 number-counter row and 31 charges: **98 unchanged rows**. The restarted UI showed the same
invoice numbers and net amounts.

**Docker is unavailable locally.** No Docker migration, container restart or image deployment
is claimed. Compose CI was updated to begin at 0003, migrate on startup and check billing
routes, but its execution has not been observed. No real OpenStack or production financial
data was used. See [Phase 4 report](phase4-report.md) for scope and limitations, especially
conservative blocking of continuously open allocations without a metering cutoff contract.
