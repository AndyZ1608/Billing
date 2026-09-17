# Internal billing engineering report

## 1. Existing code inspected

Inspected OpenStack client/normalization, sync transactions and locking, inventory ORM, lifecycle observations/periods, metering policy/query/engine, pricing resolver/bootstrap, rating money/engine/query, API routing, static dashboard, migrations and test fixtures. Pre-change regression: 110 passed (six dependency warnings), including native PostgreSQL coverage.

## 2. Changes made

Reject conflicting project ownership aliases; fill missing flavor fields from the per-sync cache; sort attachments deterministically; split volume lifecycle on attachment changes; preserve observed first-seen timestamps; distinguish unchanged sync rows. Added safe reconciliation/diagnostics, explicit configurable INTERNAL-VND bootstrap, and project/VM cost views over existing usage and charge records. Open usage is explicitly estimated.

## 3. Files changed

- `app/core/config.py`, `.env.example`: Decimal internal price configuration.
- `app/openstack/normalize.py`, `app/openstack/diagnostics.py`: ownership validation, flavor fallback, diagnostics.
- `app/sync/engine.py`, `app/lifecycle/engine.py`: reconciliation counters, observations, attachment boundaries.
- `app/models/__init__.py`, `app/schemas/__init__.py`, `app/billing/aggregate.py`: Decimal RAM and run duration.
- `app/pricing/internal.py`, `app/cli.py`: explicit seed command.
- `app/billing/internal.py`, `app/api/internal.py`, `app/api/routes.py`, `app/api/invoicing.py`, `app/main.py`: internal report and compatible API integration.
- `app/static/index.html`, `app/static/app.js`, `app/static/history.html`, `app/static/history.js`, `app/static/costs.html`, `app/static/billing.html`: internal navigation, costs, VM trace, reconciliation.
- `scripts/history_demo.py`, `tests/test_internal_billing.py`: separate synthetic demo and correctness tests.
- README, architecture/pricing/rating docs, this report and internal-billing guide.

## 4. No major refactor

Framework, ORM, directory structure, modular monolith, Docker services and financial domain boundaries preserved. No new migration or table. Existing commercial functionality retained under Advanced. No Phase 5 work.

## 5–6. OpenStack and cross-project queries

Existing openstacksdk projects/servers/volumes calls retained. Installed SDK mapping verified: `all_projects=True` becomes `all_tenants` for Nova/Cinder. Collection exhausts pagination before committing source results. Cross-project permission errors are visible. Real account visibility still requires CLI comparison.

## 7. Project mapping

Cloud/project/resource UUIDs remain authoritative. Conflicting project aliases are rejected. Same VM names across projects do not merge. Project renaming preserves UUID-linked history.

## 8. VM allocation

Resolved numeric vCPU, RAM MiB/GiB, root and ephemeral sizes are retained in lifecycle snapshots. RAM divides by 1024 using Decimal. Missing inline flavor values can use cached fallback metadata. Current flavor changes do not rewrite older periods.

## 9. SSD rule

Image-backed local root + ephemeral + Cinder capacity. Volume-backed Nova root contributes zero. Historical singly attached volumes are attributed to their VM; shared/unattached storage remains project cost. See internal-billing.md for limitations.

## 10–11. State policy and hourly formula

Existing configurable metering policy governs allocation states. Unknown states remain quality issues. Decimal quantity = allocation * elapsed seconds / 3600. Confirmed deletion stops usage; failed source scans do not advance deletion confirmation.

## 12. VND pricing

Environment-configured 10000 CPU, 11000 RAM, 500 SSD rates are explicitly seeded as INTERNAL-VND. Existing overrides/assignments and immutable historical prices remain respected. No currency conversion or mixed-currency sum.

## 13. Tests added

Exact two-hour/half-hour/resize golden cases; zero duplication on repeated seed/rating; open usage estimates without charge persistence; boot-volume double-count prevention; three-project six-VM UUID separation and rename; conflicting ownership and missing flavor fields; actual SDK query mapping; internal API responses and Nova failure completeness; historical volume detachment attribution.

## 14. Results

Final validation: the regression run passed 112 tests and encountered seven PostgreSQL setup errors because the local test server was stopped (366.31 seconds). After restarting the existing server, all seven PostgreSQL tests passed (39.47 seconds). The additional attachment-detachment test passed separately (4.81 seconds): 120 passing tests across these runs, with no remaining assertion failures. Six dependency deprecation warnings appeared in the main run. This was not a single clean 120-test invocation. Ruff and JavaScript syntax checks passed. Browser walkthrough verified populated cloud/project/VM tables and lifecycle trace against a synthetic fixture.

## 15. Reconciliation

Synthetic demo: discovered/stored projects 2/2, VMs 3/3, volumes 2/2. Per-project A: 2 VMs/1 volume; B: 1 VM/1 volume. VM1 first two-hour segment displayed CPU 80000, RAM 176000, root 50000, plus its separately configured 10 GiB ephemeral disk cost 10000. This differs intentionally from the 50-GiB-total golden fixture, which has no ephemeral disk.

## 16. Limitations

Live credentials absent; live authentication/permissions/resource accuracy unverified. Docker unavailable; no Docker deployment/restart claim. Polling cannot recreate pre-observation history or earlier unobserved attachment changes. Cinder types currently share one SSD rate. Large date ranges retain the existing usage-query memory characteristics. Existing access model retained. No network/commercial expansion.

## 17–18. Credentials and live validation

Exact environment variables, explicit pricing bootstrap, CLI reconciliation commands and ordered three-project manual validation are in [internal-billing.md](internal-billing.md). Configure the dedicated account privately before executing that acceptance stage. This report does not claim real-cloud billing acceptance.
