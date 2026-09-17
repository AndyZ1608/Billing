## Current policy: ACTIVE-only with Nova notifications

The current default is meter-v2: only ACTIVE VM intervals are billable. Nova notifications provide the fast path; REST polling remains reconciliation. Prices are unchanged. Cinder follows attached ACTIVE intervals and is counted once. Earlier allocation-policy descriptions below describe retained historical behavior. See [setup, migration and policy details](docs/nova-notifications.md) before upgrading.

## Internal billing focus

The primary workflow is now OpenStack discovery -> lifecycle -> precise CPU/RAM/SSD usage -> VND cost -> project/VM dashboard. Existing invoice features remain under Advanced. See [Internal billing setup and live validation](docs/internal-billing.md) for credentials, explicit INTERNAL-VND bootstrap, state/storage rules, APIs and CLI reconciliation. Internal test rates: CPU 10,000 VND/vCPU-hour; RAM 11,000 VND/GiB-hour; SSD 500 VND/GiB-hour. These are environment configuration, not automatic overrides of historical prices.

# OpenStack Billing — Phase 4

A standalone modular monolith that collects OpenStack projects, Nova instances and Cinder volumes, retains observations, and summarizes **current allocated / current billable resource quantities by project**.

Phase 2 adds immutable lifecycle history and seven resource-hour meters. Current inventory remains a separate view: a 4-vCPU VM contributes 4 current vCPUs; observed for 2 hours, it contributes 8 vCPU-hours. Phase 3 adds a database pricing catalog, versioned price books, project assignments/overrides, and auditable pre-tax monetary charges from FINAL usage. Phase 4 adds billing cycles, draft/review/finalized invoices, immutable charge links and credit/debit adjustments. **No tax calculation, payments or currency conversion.** See [billing](docs/billing.md), [invoices](docs/invoice.md), [adjustments](docs/adjustments.md) and the [Phase 4 report](docs/phase4-report.md). See [metering semantics and operations](docs/metering.md), [Phase 2 engineering report](docs/phase2-report.md), [pricing administration](docs/pricing.md), [rating contract](docs/rating.md), [Phase 3 engineering report](docs/phase3-report.md), and [validation evidence](docs/validation.md).

## Architecture

```text
OpenStack (Keystone / Nova / Cinder)
    → SDK collector → normalizer → PostgreSQL inventory + observations
    → current quantity aggregation + lifecycle periods → versioned usage records
    → pricing resolver → rating engine → immutable charge_records
    → billing cycles → immutable invoices/lines/source links → adjustments
    → FastAPI REST API → inventory, usage, cost, pricing and billing dashboards
```

One Python 3.12 application, one PostgreSQL 16 database, and one in-process sync worker. FastAPI serves static HTML/CSS/JavaScript. The browser communicates only with the billing backend. There is no Redis, Celery, message broker or microservice boundary. See [architecture and reconciliation decisions](docs/architecture.md).

## Requirements

- Docker Engine / Docker Desktop with Docker Compose v2.
- Network access **from the application container** to Keystone and all selected regional Nova/Cinder service endpoints.
- An OpenStack account with the cross-project read permissions described below.
- For local development/tests: Python 3.12+ and pip. A live OpenStack deployment is not needed for tests.

## Quick start with Docker Compose

```bash
cp .env.example .env
# Edit .env with your OpenStack credentials and region/interface.
docker compose up -d --build
docker compose ps
```

PowerShell: use `Copy-Item .env.example .env` instead of `cp` if preferred.

Open the dashboard at [localhost:8080](http://localhost:8080), the API explorer at [localhost:8080/docs](http://localhost:8080/docs), or [OpenAPI JSON](http://localhost:8080/openapi.json).

Startup waits for healthy PostgreSQL, runs `alembic upgrade head`, and starts the app as a non-root user. The initial sync begins immediately; later runs use `SYNC_INTERVAL_SECONDS`. The API remains available during synchronization. With blank credentials the app starts, records `configuration_missing`, and displays the failure. It does not invent an inventory.

### Isolation and credentials

The POC has **no UI/API login**. Compose binds the application to `127.0.0.1` and does not publish PostgreSQL. Keep this on a trusted administrator workstation or behind an authenticated HTTPS reverse proxy. Set `APP_BIND_ADDRESS` only when that isolation is in place. Inventory includes project names, host names and resource identifiers.

`.env`, local data and virtual environments are ignored by Git and Docker build context. OpenStack credentials live only in server-side environment/configuration memory. The API uses explicit response schemas; it never returns settings or raw SDK payloads. Logs contain selected event fields and error codes, never exception messages, token bodies, SQL parameters, or credentials. Do not enable SDK HTTP debug logging in this application.

The sample PostgreSQL password is for an isolated local POC. If changed, update **both** `POSTGRES_PASSWORD` and the URL-encoded password in `DATABASE_URL`. Changing initialization variables does not change credentials inside an existing PostgreSQL volume.

## Configuration

| Variable | Purpose / default |
| --- | --- |
| `APP_ENV` | `development`; `production` requires PostgreSQL |
| `APP_PORT` / `APP_BIND_ADDRESS` | Published port `8080`, bind `127.0.0.1` |
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL using `postgresql+psycopg://` |
| `OPENSTACK_CLOUD_ID` | Stable UUID for this cloud; do not change on rename |
| `OPENSTACK_CLOUD_NAME` | Display name only |
| `OS_AUTH_URL` | Keystone v3 URL, e.g. `https://identity.example/v3` |
| `OS_USERNAME`, `OS_PASSWORD`, `OS_PROJECT_NAME` | Project-scoped password authentication |
| `OS_USER_DOMAIN_NAME`, `OS_PROJECT_DOMAIN_NAME` | Default `Default` |
| `OS_APPLICATION_CREDENTIAL_ID`, `OS_APPLICATION_CREDENTIAL_SECRET` | Optional application credential pair; takes precedence over password mode |
| `OS_REGION_NAME`, `OS_INTERFACE` | `RegionOne` / `internal`; use an interface reachable from the container |
| `OS_CACERT` | Optional PEM CA file path **inside** container; TLS verification stays enabled |
| `OS_COMPUTE_API_VERSION` | `2.47`; embedded flavor dimensions. For older Nova, use `2.1` and flavor lookup |
| `OS_API_TIMEOUT_SECONDS` | Per-request timeout, `30`; bounded SDK connection/status retries |
| `SYNC_INTERVAL_SECONDS` | `300`, minimum `5` |
| `SYNC_ENABLED` | `true`; set `false` to disable automatic sync (manual sync still works) |
| `RESOURCE_MISSING_CONFIRMATION_COUNT` | `3`, minimum `2`; complete absent scans before confirmation; old `MISSING_SCAN_THRESHOLD` remains accepted |
| `BILLING_TIMEZONE` | `Asia/Ho_Chi_Minh`; IANA reporting timezone |
| `METERING_POLICY_PATH` | `config/metering.yaml`; separate from current inventory policy |
| `METERING_CALCULATION_VERSION` | `meter-v1`; increment when changing metering policy or formulas |
| `METERING_ENABLED` | `true`; run incremental metering after sync |
| `RATING_ENABLED` | `true`; run incremental rating after metering |
| `RATING_CALCULATION_VERSION` | `rating-v1`; explicit re-rating applies a changed version to prior rated usage |
| `PRICING_ADMIN_TOKEN` | Optional secret bearer token for financial write/run APIs; empty uses trusted-loopback POC access |
| `BILLING_POLICY_PATH` | `config/billing.yaml` |
| `RETAIN_RAW_PAYLOAD` | `false`; optionally retain a safe allowlisted observation on the current resource row |

Application credentials inherit the creator's permitted scope; they do not bypass policy. Their role assignments and any access rules must permit all required APIs. System-scoped authentication, federated login and `clouds.yaml` are outside this POC. Use explicit environment credentials. Mount a private CA file read-only through a Compose override if needed; do not disable TLS verification.

This release synchronizes **one configured cloud and one region**. Use a stable distinct cloud UUID for a different cloud/region; changing a region under an existing cloud UUID would reconcile against a different inventory.

### Aggregation policy

Edit [config/billing.yaml](config/billing.yaml), then `docker compose restart billing-app`.

```yaml
billing:
  nova:
    counted_states: [ACTIVE, SHUTOFF, PAUSED, SUSPENDED, RESCUE]
    state_metrics:
      SHUTOFF: {vcpu: false, ram: false, root_disk: true, ephemeral_disk: true}
  cinder:
    counted_states: [available, in-use, attaching, detaching, reserved, maintenance, backing-up]
```

The shipped file counts all listed Nova dimensions, including SHUTOFF CPU/RAM; the example above demonstrates an override. Resource collection retains **all statuses**. States not listed in the policy contribute nothing; including `ERROR`, `SHELVED`, transitional states, etc. is an explicit policy decision. A per-state dimension override does not change `instance_count`. Unknown or misspelled configuration fields fail validation.

Quantities use Nova flavor vCPUs, RAM MiB / 1024 for GiB, flavor local disk GiB, and Cinder volume size GiB. `ram_gb` is derived from `ram_mb`, avoiding redundant storage. Volume-boot VMs contribute **zero Nova root disk**; their boot volume appears in Cinder capacity. A data-volume attachment by itself never changes the root-disk classification. Ephemeral disk remains a Nova quantity. Unknown dimensions are null in resource APIs and excluded from numeric sums, with `incomplete_instances` / `incomplete_volumes` warnings. Such totals represent known quantities and are incomplete.

## OpenStack permissions and APIs

The collector performs no resource writes. Token creation is the only required POST to OpenStack. Endpoint discovery and paginated GET requests are handled by the SDK.

| Service | Required API | Permission / relevant policy |
| --- | --- | --- |
| Keystone v3 | `POST /v3/auth/tokens`; catalog/version discovery | Valid project-scoped password or application credential authentication |
| Keystone v3 | `GET /v3/projects` (all visible projects, paginated) | `identity:list_projects`, including projects outside the credential's own project |
| Nova v2.1 | `GET /servers/detail?all_tenants=1` | `os_compute_api:servers:detail` and `os_compute_api:servers:detail:get_all_tenants` |
| Nova v2.1 | `GET /flavors/{flavor_id}` when embedded dimensions are unavailable | `os_compute_api:flavors:show`; private flavors must be visible if their values are needed |
| Nova server details | Tenant UUID and optional compute-host / availability-zone attributes | Tenant UUID must be present; `os_compute_api:os-extended-server-attributes` controls privileged host attributes on releases that expose this policy |
| Cinder v3 | `GET /v3/{project_id}/volumes/detail?all_tenants=1` | `volume:get_all` **and an administrative context permitting all-tenants listing** |
| Cinder volume details | Owning project UUID (`os-vol-tenant-attr:tenant_id`) | `volume_extension:volume_tenant_attribute` |

Glance is not required by this implementation. Volume attachments come from the detailed volume response. Missing host/AZ fields do not block inventory.

**Policy names and scope enforcement vary by OpenStack release and deployment overrides.** A generic `reader` role alone does not guarantee cloud-wide visibility. In particular, Cinder can restrict listing to the current project when the caller lacks an administrative context even with the all-tenants flag. Configure these permissions with the cloud administrator and verify known resources in **at least two projects** against the dashboard before trusting totals. This app cannot prove that an apparently successful API list is complete when the cloud silently restricts visibility.

References: [Nova policies](https://docs.openstack.org/nova/latest/configuration/policy.html), [Cinder policies](https://docs.openstack.org/cinder/latest/configuration/block-storage/policy.html), [Keystone policies](https://docs.openstack.org/keystone/latest/configuration/policy.html), [SDK server resources](https://docs.openstack.org/openstacksdk/latest/user/resources/compute/v2/server.html), [SDK Cinder listing](https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage_v3.html).

## Synchronization and reconciliation

- Only one run per cloud executes at a time: an in-process lock plus a PostgreSQL session advisory lock shared by manual and scheduled syncs.
- `POST /api/v1/sync` returns `202` and a UUID, or `409` if already running. Runs appear in `/api/v1/sync-runs`.
- Generators are fully consumed before a service's inventory is written. A failure on a later page discards that service's fetched batch and retains its previous inventory and missing counters.
- Each service writes in its own database transaction. Nova success plus Cinder failure yields `PARTIAL`, preserving Cinder data and reporting service freshness separately.
- Malformed rows are rejected; valid rows still sync. That service's absence reconciliation is disabled for the run. Conflicting duplicates are treated the same way. Identical UUID duplicates are deduplicated.
- A first absent observation leaves a resource counted according to its last observed status. After three complete absent scans by default, `is_missing=true` excludes it. Failed or malformed scans do not advance the counter. Reappearance clears absence state.
- A resource missing after polling is **not proven deleted**. `deleted_at_openstack` is populated only from OpenStack's explicit timestamp, never from disappearance time. Missing rows and their history are retained.
- Project renames update the same UUID. Resource owners absent from Keystone become clearly flagged placeholder projects; later discovery resolves the same row.
- On the next acquired lock, abandoned `RUNNING` rows are marked `FAILED` with `interrupted_previous_run`.
- Resource observations append on billable state/allocation changes and reconciliation events. Unchanged resource polls update freshness without adding duplicate periods or observations. Project snapshots remain per poll. The first Phase 2 observation starts a BASELINE period; no prior allocation is inferred.

`CONNECTED` means Keystone authentication succeeded for the latest attempt. It does **not** imply every service succeeded or that inventory is fresh. Read service status and timestamps. `last_successful_sync` means a completely successful run; `last_failed_sync` includes partial runs. The dashboard shows both.

### Manual sync / API examples

```bash
curl -X POST http://localhost:8080/api/v1/sync -H 'Content-Type: application/json' -d '{}'
curl http://localhost:8080/api/v1/health
curl http://localhost:8080/api/v1/billing/current
curl 'http://localhost:8080/api/v1/billing/projects?q=Project&sort=vcpu_count&direction=desc'
```

PowerShell can use `Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/v1/sync -ContentType application/json -Body '{}'`.

### API index

| Method | Path |
| --- | --- |
| GET | `/api/v1/health`, `/api/v1/readiness` |
| GET | `/api/v1/cloud`, `/api/v1/sync-runs` |
| POST | `/api/v1/sync` (JSON content type required) |
| GET | `/api/v1/projects`, `/api/v1/projects/{project_id}` |
| GET | `/api/v1/projects/{project_id}/instances`, `/api/v1/projects/{project_id}/volumes` |
| GET | `/api/v1/billing/current`, `/api/v1/billing/projects`, `/api/v1/billing/projects/{project_id}` |
| GET | `/api/v1/billing/policy` |

List endpoints return `{items, total, offset, limit}`; default limit 50, maximum 500. Project billing supports partial name/UUID search and sorting by all summary columns. Resource tables include every retained status and missing row; their row counts need not equal policy-filtered summary counts. UUID API paths accept standard UUIDs, including OpenStack's 32-character form.

## Database migrations

Alembic is the sole schema management mechanism. The application never calls ORM `create_all`.

```bash
docker compose exec billing-app alembic current
docker compose exec billing-app alembic upgrade head
```

For development: edit models, create a revision with `alembic revision --autogenerate -m 'description'`, review it, and apply it. Back up first before applying migrations to valued data. Initial downgrade drops the inventory tables and is only appropriate for disposable development databases.

## Development and tests

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q
ruff check app migrations tests scripts
ruff format --check app migrations tests scripts
```

The fast suite uses migrated temporary SQLite databases, SDK resource objects and a fake OpenStack client. It tests aggregation, filtering, normalization, partial failures, missing/reappearing resources, idempotency, safe observations, API validation and database constraints. Tests never call a cloud. If the OS temporary directory is inaccessible, create `.local` and use a fresh `--basetemp=.local/test-run-1` path.

PostgreSQL integration tests validate native JSONB, timestamps, schema parity and advisory lock exclusion across independent connection pools:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/billing_test'
pytest -q
```

PowerShell uses `$env:TEST_DATABASE_URL='...'`. The account must be able to create schemas and install the trusted `btree_gist` extension (or have it preinstalled in `public`). Tests create and remove unique `billing_test_<uuid>` schemas, preserving public tables. Always use a disposable test database. CI runs these tests with PostgreSQL, builds the image, starts Compose, and checks readiness and historical routes. That workflow has not been executed in this local environment.

### Synthetic local demo

```bash
python -m scripts.demo
```

Open [localhost:8081](http://localhost:8081). If that port is reserved or occupied, use `python -m scripts.demo --port 18081`. This explicit developer mode uses `.local/demo.db` and synthetic fixtures: Project A has 6 vCPUs / 12 GiB RAM / 100 GiB Cinder; Project B has 8 vCPUs / 16 GiB RAM / 500 GiB Cinder. **It does not connect to OpenStack.** Standard Docker startup always uses the real SDK client.

## Phase 2 historical usage

Open `/history`, `/metering-runs`, or `/quality`. Project pages link current inventory, historical usage, instances and volumes. Resource names link to lifecycle and usage detail with source observations. Date ranges use local midnight in the selected reporting timezone and an **exclusive** end date; ongoing usage stops at the report cutoff.

```bash
curl 'http://localhost:8080/api/v1/metering/summary?start=2026-09-01T00:00:00Z&end=2026-10-01T00:00:00Z&timezone=Asia/Ho_Chi_Minh'
curl -X POST http://localhost:8080/api/v1/metering/run -H 'Content-Type: application/json' -d '{}'
docker compose exec billing-app python -m app.cli metering run
docker compose exec billing-app python -m app.cli metering run --from 2026-09-01T00:00:00Z --to 2026-10-01T00:00:00Z --force
docker compose exec billing-app python -m app.cli lifecycle inspect INSTANCE RESOURCE_UUID
```

`--force` replays canonical segments without deleting or overwriting records. Metering is cloud-wide even when the dashboard displays one project. Changing the policy requires a new `METERING_CALCULATION_VERSION`; restart the app and replay the desired historical range. Old versions remain readable and immutable. Do not sum versions together.

Migration `0002` preserves Phase 1 rows and observations, adds lifecycle/metering tables, and starts history on the next successful discovery. It does not reconstruct past usage from old snapshots. PostgreSQL enforces non-overlap and immutable closed periods/final records. Back up and stop concurrent writers before upgrading; a downgrade discards Phase 2 history.

For a populated, entirely synthetic walkthrough:

```bash
python -m scripts.history_demo --port 18082
```

Open [historical demo](http://127.0.0.1:18082/history). It uses `.local/history-demo.db`, simulates a VM resize and volume extension, and never contacts OpenStack.

## Phase 3 pricing and rated costs

Open `/costs`, `/pricing`, and `/rating-runs`. Current project pages now include a Cost tab, and Data Quality includes missing prices, unrated usage and rating failures. Monetary reports show **FINAL usage only**, separately by currency. Unchanged open allocations are not finalized or projected into a bill.

Pricing workflow: create products → book → draft version → rules → activate → cloud default/project assignment → optional override → rating → review unrated usage. Use [pricing documentation](docs/pricing.md) for effective dates and administration. All pricing inputs are database records; startup never seeds prices.

For explicit TEST pricing only:

```bash
docker compose exec billing-app python -m app.cli pricing seed-demo
docker compose exec billing-app python -m app.cli rating run --actor operator
```

This idempotently creates POC-VND sample rates without overwriting existing pricing/defaults. To use production prices, create and review your own book instead.

Financial write APIs require `Content-Type: application/json`, `X-Pricing-Admin: true`, and an audit actor label. If `PRICING_ADMIN_TOKEN` is configured, send `Authorization: Bearer <token>`. Cost reads remain separate. The token is not a general login; retain the POC's loopback binding or authenticated reverse proxy. The CLI uses the host/database access boundary.

```bash
curl 'http://localhost:8080/api/v1/rating/summary?start=2026-09-01T00:00:00Z&end=2026-10-01T00:00:00Z&currency=VND'
# ADMIN: supersede current charge sets for intersecting usage and preserve old rows:
docker compose exec billing-app python -m app.cli rating run --from 2026-09-01T00:00:00Z --to 2026-10-01T00:00:00Z --force --actor finance-admin
```

Normal rating fills only new/UNRATED usage; already RATED segments are stable. Correcting configuration does not change them automatically. Force re-rating replaces the complete charge set for each intersecting usage record, retaining SUPERSEDED rows and replacement run references. The [rating contract](docs/rating.md) covers scope, Decimal rounding, clipping, version selection and limitations.

Migration `0003` is additive and preserves all Phase 1/2 rows. It adds pricing, audit, rating runs, charge records, overlap/immutability guards and an incremental usage index. Stop concurrent writers and take a verified backup before upgrade. Normal Compose startup applies Alembic head. Do not downgrade a database containing valued pricing/charges: downgrade discards those new tables.

Populated synthetic walkthrough with a separate `.local/cost-demo.db`:

```bash
python -m scripts.history_demo --port 18083 --pricing-demo
```

Open [cost demo](http://127.0.0.1:18083/costs). It simulates lifecycle changes, explicitly seeds TEST POC-VND pricing and rates the final usage. It never connects to OpenStack.

## Health, logs and troubleshooting

`/api/v1/health` reports application, database, authentication, service status, projects discovered and sync freshness. `/api/v1/readiness` checks database access and migration version; OpenStack outages do not make a usable inventory UI unready. Compose uses readiness for its healthcheck.

```bash
docker compose logs --tail=100 billing-app
docker compose logs --tail=100 postgres
docker compose logs -f billing-app
```

Application logs are JSON events with run UUIDs, service, discovery/create/update/missing counts, safe error codes and duration. An `updated` count means an existing row was observed, even if its business fields were unchanged.

| Symptom / error | Check |
| --- | --- |
| `configuration_missing` | Auth URL and complete password/project or application credential pair |
| `http_401` | Credentials, expiration/revocation, Keystone v3 endpoint and token scope; SDK renews tokens using its auth session |
| `http_403` | Cross-project policies and application credential restrictions |
| `http_404` | Catalog URLs/version; deleted private flavor produces a dimension warning rather than dropping its VM |
| `api_timeout`, `http_500`, `service_error` | Container routing, DNS, TLS CA, region/interface and service availability; prior inventory is retained |
| `incomplete_resource_dimensions` | Inspect resource `quality_issues`; flavor may be unavailable or payload lacks dimensions |
| `malformed_or_conflicting_resources` | A resource ID/owner/payload is invalid or duplicate versions conflict; absence reconciliation is paused |
| Zero or unexpectedly small totals | Policy counted states, `is_missing`, unknown dimensions and all-project permissions; compare known cross-project inventory |
| Database unavailable / migration mismatch | PostgreSQL logs and `alembic current`; validate database URL without publishing its password |
| Sync appears stuck | Request timeouts are per API call, not a total run deadline; a large cloud has many pages/flavors. Check service events and timestamps |

## Stopping and backups

`docker compose stop` stops containers. `docker compose down` removes containers/network while keeping the named database volume. **Do not use `down -v` unless intentionally erasing all inventory and observation history.**

Before upgrading, take and verify a PostgreSQL logical backup. These examples avoid host shell binary redirection issues:

```bash
docker compose exec postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/billing.dump'
docker compose cp postgres:/tmp/billing.dump ./billing.dump
```

Store backups outside this repository with restricted access. Back up `.env` securely and separately; it contains secrets. Test restore using `pg_restore` into a new disposable database before relying on the backup. A Docker volume alone is not a backup. Project observations, lifecycle events and usage records grow over time; there is no automatic retention deletion.

## Known limitations and future roadmap

- POC, single configured region/cloud, no general app login or invoicing. Financial writes have separate admin headers and optional token protection.
- Polls cannot observe resources created and deleted between scans or know precise lifecycle transitions. Snapshot timestamps are observation times.
- Independent service commits can temporarily mix service observation times; the UI exposes freshness. PostgreSQL reads are repeatable within each API request.
- Complete empty lists can eventually mark everything missing if permissions change silently. Verify account scope operationally.
- Inventory uses flavor allocation, not CPU utilization, actual consumed disk space, thin-provisioned backend usage, snapshots, swap, image storage, backups, network bandwidth or host capacity. Flavor disk zero with an image requires image-size information; its root dimension is marked unknown (`image_sized_root_disk`) rather than reported as zero.
- Service pages are materialized in memory for reconciliation safety; SQL aggregates all configured-cloud projects before paging summary results. Suitable for POC scale, not millions of resources.
- Observations retain only normalized allowlisted fields, never arbitrary metadata/user-data/fault payloads. `raw_payload` is intentionally a safe subset, not an exact wire archive. Normalized source observations are available through the metering audit API.
- A hard process/database failure can leave a running marker until the next sync acquires the lock. Do not run schema migrations concurrently. Use the supplied one-worker deployment.

Phase 3 consumes `usage_records`; Phase 4 consumes `charge_records` for billing cycles and immutable invoices. Phase 5 can add customer/billing accounts, documents, tax foundation, payment status/reconciliation and notifications. Open metering periods block finalization; a future upstream checkpoint design is needed for long-running unchanged allocations. Ceilometer and CloudKit are not dependencies.


## Phase 4 billing workflow and upgrade

Back up PostgreSQL **before** upgrading. These commands use the repository's actual service
and in-container database/user variables; the archive is copied without binary shell redirection:

```bash
docker compose exec postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/backup-before-phase4.dump'
docker compose cp postgres:/tmp/backup-before-phase4.dump ./backup-before-phase4.dump
docker compose stop billing-app
docker compose run --build --rm billing-app alembic upgrade head
docker compose up -d --build
```

Migration `0004` only adds billing tables, indexes and guards; it preserves inventory,
lifecycle, usage, pricing and charges. Never drop/recreate the database or run a financial
migration downgrade on issued invoices. Back up/restore testing remains an operator duty.

Set private `BILLING_OPERATOR_TOKEN` and `BILLING_ADMIN_TOKEN` values in `.env`, then recreate
the app container to load them. Empty tokens disable billing writes. Read endpoints retain
the POC's internal read boundary. Operator/admin controls are in each billing page; tokens
stay in page memory. Keep loopback binding or use an authenticated TLS reverse proxy.

Open `/billing-cycles`: create aware period → calculate → inspect drafts → review cycle →
finalize each invoice → finalize/close cycle. `/invoices` exposes search, filter, amounts,
source charges, validation and audit. `/adjustments` manages approved credit/debit records.
Unrated, stale, provisional or unresolved metering data blocks finalization. The UI never
labels drafts as issued documents. Zero lines remain visible for audit.

A local synthetic demonstration (no real OpenStack, no production prices) is available:

```bash
python -m scripts.history_demo --port 18084 --billing-demo
```

It uses `.local/billing-phase4-demo.db`, explicit test prices and the local-only demo token
`synthetic-billing-demo-only`. This token is deliberately public test data and must never be
used for a deployed application. The synthetic resources transition to excluded states so
historical usage can be completely finalized; this does not change actual OpenStack resources.
Create a past cycle covering the demo's closed usage, then follow the workflow above.

Docker is not installed on the implementation workstation; local evidence uses native
PostgreSQL and process restart. Compose CI checks are defined but have not been executed here.
