## Current policy: ACTIVE-only with Nova notifications

The current default is meter-v2: only ACTIVE VM intervals are billable. Nova notifications provide the fast path; REST polling remains reconciliation. Prices are unchanged. Cinder follows attached ACTIVE intervals and is counted once. Earlier allocation-policy descriptions below describe retained historical behavior. See [setup, migration and policy details](nova-notifications.md) before upgrading.

# Internal OpenStack billing

## Scope and operation

The existing FastAPI/SQLAlchemy modular monolith and Docker Compose deployment remain intact. Inventory -> observed lifecycle -> usage_records -> charge_records remains the authoritative pipeline. The primary dashboard now emphasizes CPU, RAM and SSD costs, project UUIDs, VM details and reconciliation. Existing invoicing stays under Advanced; no Phase 5 features were added.

Configure the dedicated account through environment variables (or the private, ignored `.env`):

```dotenv
OS_AUTH_URL=https://keystone.example/v3
OS_USERNAME=
OS_PASSWORD=
OS_PROJECT_NAME=
OS_USER_DOMAIN_NAME=Default
OS_PROJECT_DOMAIN_NAME=Default
OS_REGION_NAME=RegionOne
OS_INTERFACE=internal
BILLING_TIMEZONE=Asia/Ho_Chi_Minh
BILLING_CURRENCY=VND
PRICE_CPU_PER_VCPU_HOUR=10000
PRICE_RAM_PER_GIB_HOUR=11000
PRICE_SSD_PER_GIB_HOUR=500
```

Application credentials remain supported through `OS_APPLICATION_CREDENTIAL_ID` and `OS_APPLICATION_CREDENTIAL_SECRET` (take precedence). Use `OS_CACERT` for a private CA. TLS verification stays enabled. Never paste credentials into logs, screenshots or source control. Keep `OPENSTACK_CLOUD_ID` stable. `RESOURCE_MISSING_CONFIRMATION_COUNT=3` controls confirmed disappearance.

The SDK exhausts Keystone projects, Nova `servers(details=True, all_projects=True)` and Cinder `volumes(details=True, all_projects=True)`. In the installed SDK the latter option maps to `all_tenants`. Cloud policy must permit those requests; HTTP success alone cannot prove complete visibility. See the official [Nova proxy](https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html) and [Cinder proxy](https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage_v3.html) documentation.

## Explicit pricing bootstrap

Rates are configuration, applied through the existing immutable price book system. After migrations/startup, select the trusted effective date and run, for example:

```sh
docker compose exec billing-app python -m app.cli pricing seed-internal --from 2026-09-16T00:00:00+07:00
docker compose exec billing-app python -m app.cli metering run
docker compose exec billing-app python -m app.cli rating run
```

This creates INTERNAL-VND and CPU/RAM/local-root/ephemeral/Cinder-capacity rules. Instance-count and volume-count meters have valid zero prices. It does not fabricate usage before observation. Existing project assignments and overrides remain effective and take precedence over the default. Review them in Pricing before expecting every project to use internal rates. The dashboard shows configured rates and actual rates found in the selected period separately.

Repeated identical bootstrap is safe. Changing environment prices does not rewrite an active price version. Use Pricing to create new effective-dated versions and explicit rerating for corrections; billed charges remain protected. Back up the database before operational changes. No schema migration was required for this iteration.

## Calculation and storage policy

`hours = Decimal(duration_seconds) / Decimal(3600)`; usage = allocated capacity * hours; cost = usage * effective unit price. RAM GiB = MiB / 1024. Existing NUMERIC storage and centralized eight-decimal HALF_EVEN monetary rounding are reused. Final amounts are summed from stored charges; clipped ranges use the same Decimal policy. The UI preserves precise decimal strings and separates current capacity from selected-period consumption.

Golden examples: 4 vCPU, 8 GiB RAM, 50 GiB SSD for two hours = 80,000 + 176,000 + 50,000 = 306,000 VND. The half-hour 2/4/20 example = 37,000 VND. A 2/4/20 VM for two hours resized to 4/8/20 for three hours = 562,000 VND.

SSD = Nova image-backed local root + ephemeral + Cinder volume capacity. Volume-backed Nova root is zero; its Cinder boot volume is counted once. Unknown boot/flavor allocations remain quality issues rather than invented capacity. All included Cinder types currently use the single SSD rate; operators must confirm this matches their infrastructure.

A singly attached Cinder volume is attributed to its VM using the lifecycle period's observed attachment and matching project UUID. Shared or unattached volumes remain project costs, so VM totals can be smaller than project totals. Attachment changes now split volume lifecycle periods without changing schema. Attachment history missed before this change cannot be reconstructed reliably.

`config/metering.yaml` controls historical state policy: ACTIVE, SHUTOFF, PAUSED, SUSPENDED, RESCUE and SHELVED retain allocation billing. SHELVED_OFFLOADED bills retained local root only; Cinder remains independently metered. ERROR and deleted states are excluded by default. Unknown states raise quality issues. Follow the existing metering-version procedure when changing historical policy; the legacy current-inventory policy remains available separately.

Closed metered usage uses persisted charges. Open or awaiting-metering intervals are explicitly provisional estimates using the same pricing resolver; dashboard reads do not create charges. Missing prices remain visible as unrated segments. Non-VND amounts are not added to VND totals. Polling establishes a trusted observation baseline and cannot reconstruct unknown earlier state changes. Only successful source scans advance missing confirmation; service failures never delete all resources.

## Dashboard and API

Overview -> Project -> VM exposes allocations, selected-period usage/cost, lifecycle and source references. Date presets use the billing timezone with UTC storage and half-open intervals. Advanced preserves pricing/rating audit and invoice features.

- `GET /api/v1/diagnostics/openstack`: safe last-sync statuses, counts and per-project reconciliation.
- `POST /api/v1/sync`: serialized collection; UI Test connection / sync performs this read-only OpenStack inventory sync.
- `GET /api/v1/billing/summary?start=...&end=...`: internal cloud costs.
- `GET /api/v1/billing/projects?start=...&end=...`: project costs.
- `GET /api/v1/billing/projects/{uuid}?start=...&end=...`: project detail.
- `GET /api/v1/billing/instances?start=...&end=...`: VM costs, optionally project-filtered.
- `GET /api/v1/billing/instances/{uuid}?start=...&end=...`: allocation and paginated cost trace.

Both dates are required when one is supplied. Existing no-date summary/project API behavior is preserved for compatibility; VM APIs default to this month. Use `as_of` for a shared estimate cutoff across requests. Major internal summaries expose data_quality_status, unrated segments and rated versus estimated costs. HEALTHY describes observed source health, not independently proven cloud-wide authorization.

## Live reconciliation checklist (pending supplied account)

Use exactly the same credential environment for the OpenStack CLI; do not put passwords in command arguments:

```sh
openstack project list
openstack server list --all-projects --long
openstack volume list --all-projects --long
openstack server show VM_UUID
openstack flavor show FLAVOR_UUID
openstack volume show VOLUME_UUID
```

1. Test authentication and all three services via Sync. Investigate denied cross-project requests before trusting costs.
2. Compare discovered project UUIDs/counts to CLI; include projects with zero VMs.
3. Compare every project's VM count and ownership UUID, then Cinder ownership/count.
4. In at least three projects select several VMs. Record project/VM UUID, flavor, vCPU, RAM MiB/GiB, root, ephemeral, attached volume UUID/size, state and observation time. Compare CLI, inventory API and dashboard.
5. Confirm volume-backed roots are not included as local root. Compare project capacity totals, including unattached/shared volumes.
6. Observe a known interval, including a resize and confirmed deletion if permitted in a test project. Calculate Decimal seconds-based usage and costs manually; compare lifecycle, usage, charge trace and dashboard.
7. Repeat unchanged sync/meter/rating; verify stable counts. Simulate failures in tests rather than disrupting production services.
8. Keep a timestamped reconciliation record. Investigate PARTIAL/INCOMPLETE, placeholders, pending deletion, unknown states and unrated segments before treating costs as trusted.

No live credentials were configured during implementation. Real OpenStack authentication, source counts and source-to-dashboard agreement therefore remain unverified. Docker is unavailable in this development environment; native PostgreSQL and SQLite tests cover persistence/migrations but do not constitute a Docker deployment test.

## Known limits

No IP, bandwidth, router, load balancer, backup, Kubernetes, tax/payment or commercial contract work in this iteration. Accuracy is bounded by polling and trusted first observation. Very large selected ranges still use the existing metering query representation in memory; pricing is cached per report and charge lookups are batched. This POC retains its existing loopback/read-access security model; deploy behind appropriate network/access controls.


## VND presentation

Cost cards, project/VM tables, monetary breakdowns and invoice views display VND to the nearest whole dong using ROUND_HALF_UP (ties away from zero). Browser formatting uses decimal strings and BigInt, never Number arithmetic. The API's existing display_total uses Decimal ROUND_HALF_UP. Raw API calculation fields, stored NUMERIC values, audit snapshots and quantity fields retain full precision. USD presentation and all billing formulas remain unchanged. Displayed components may differ from a displayed total by a dong because each value is rounded independently only for presentation; totals are calculated from precise amounts.

Validation: `node --test tests/test_money_display.cjs` and `python -m pytest tests/test_money_display.py` cover exact ties, credits, very large values, fractional display exclusion, formatter loading and preservation of raw API values.
