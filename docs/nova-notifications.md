# Nova notifications and ACTIVE-only billing

## Policy and historical safety

The default metering policy is now `meter-v2`: only ACTIVE VMs generate CPU, RAM, root and ephemeral usage. All other known states generate none; unknown states also generate none and appear as quality issues. Current Nova counted states in `config/billing.yaml` also default to ACTIVE.

Rates remain CPU 10,000 VND/vCPU-hour, RAM 11,000 VND/GiB-hour and SSD 500 VND/GiB-hour through INTERNAL-VND and existing effective-dated pricing. Decimal arithmetic and the centralized monetary policy remain unchanged.

Cinder SSD is counted once for the union of attached VMs' ACTIVE intervals in the same project. Unattached volumes are not billed. Volume-backed root remains zero in Nova local disk accounting. Shared capacity is not multiplied by attachment count. Shared storage stays at project scope when VM attribution is ambiguous. Cinder attachment/size timing still depends on REST polling.

Existing meter-v1 policies, usage, charges and finalized invoices remain immutable. Legacy regression fixtures explicitly test their historical allocation-based behavior. Set `METERING_CALCULATION_VERSION=meter-v2` before starting the new default policy. A stale meter-v1 environment with the changed file fails policy validation instead of silently changing history. Never edit a registered policy JSON or bill both calculation versions for the same interval.

The v2 policy applies to trusted lifecycle intervals materialized under v2, including earlier observed intervals when reprocessed. It does not automatically create a prospective cutover date or rewrite older invoices. Select the intended period and review versioned results before financial export.

## Architecture

The existing monolith runs an optional consumer thread alongside the polling scheduler. No microservice or broker is installed by the application.

Nova versioned notification -> dedicated RabbitMQ queue -> normalized event -> processed_notifications and existing lifecycle ledger -> metering/rating.

REST polling remains authoritative reconciliation for initial inventory, missing allocation, event loss and downtime. Both paths share the process lock and PostgreSQL cloud advisory lock. Missing/conflicting event data requests the existing polling job; a busy job is retried by scheduled polling.

## OpenStack configuration: operator action

Verify against the deployed Nova release and existing notification users:

```ini
[notifications]
notify_on_state_change = vm_state
notification_format = versioned
versioned_notifications_topics = versioned_notifications,billing_notifications

[oslo_messaging_notifications]
driver = messagingv2
```

Use `notification_format = both` if legacy consumers need unversioned notifications. Preserve existing topics. Configure broker transport using the deployment's existing oslo.messaging settings. This application does not modify Nova or RabbitMQ configuration.

See [Nova payload documentation](https://docs.openstack.org/nova/latest/reference/notifications.html) and [Nova configuration options](https://files.openstack.org/docs/nova/2024.1/configuration/config.html). Inspect a sanitized payload from the deployed release before production consumption.

## Billing environment

```dotenv
SYNC_ENABLED=true
SYNC_INTERVAL_SECONDS=300
METERING_CALCULATION_VERSION=meter-v2
NOVA_NOTIFICATION_ENABLED=true
NOVA_NOTIFICATION_TRANSPORT_URL=
NOVA_NOTIFICATION_TOPIC=billing_notifications
NOVA_NOTIFICATION_EXCHANGE=nova
NOVA_NOTIFICATION_QUEUE=billing-nova-consumer
NOVA_NOTIFICATION_CA_CERT=/run/secrets/rabbit-ca.pem
```

Supply the private broker URL through environment/secret configuration. Use the actual exchange, vhost, broker and certificate mount. Supported URL schemes are amqp, amqps, and a single-host rabbit alias. Full oslo.messaging multi-host/query syntax is not implemented. TLS requires certificate validation; plaintext AMQP should remain within the deployment's protected network. Broker credentials and raw notification bodies are not exposed by diagnostics.

The consumer declares its own durable, non-auto-delete queue and binds topic `billing_notifications.*` to the existing topic exchange. Queue names must begin with billing followed by a dash, underscore or dot. Use a unique queue for each independent cloud consumer; replicas for one cloud share it. Never configure another component's queue. Grant only the permissions needed to declare/read the billing queue and bind it to the existing exchange. No Nova RPC publishing permission is needed.

[Kombu's consumer API](https://docs.celeryq.dev/projects/kombu/en/stable/userguide/consumers.html) provides JSON-only decoding, manual acknowledgement and prefetch=1. The loop checks heartbeats and reconnects after failures. Broker exception text is not logged.

## Payload and timestamps

The adapter accepts an oslo.message JSON envelope or decoded notification body, supports major-version-1 Nova objects, unwraps nova_object.data, and reads uuid, tenant_id, state/state_update and optional nested flavor dimensions. Missing allocations reuse the known inventory snapshot. A changed flavor never inherits missing numeric fields from the previous flavor. Unknown root size remains a quality issue.

Nova stopped maps to SHUTOFF, rescued to RESCUE, building to BUILD and resized to VERIFY_RESIZE. Unknown states are non-billable. The adapter processes instance.update and completed instance actions ending in .end. It never guesses state from an action name; start/error messages do not establish completed transitions.

Envelope timestamp is the transition time. Oslo's offsetless timestamps are interpreted as UTC. Receipt time is stored separately. Missing/malformed/future timestamps are quarantined, not replaced with receipt time. Lifecycle valid_to uses event time; closed_at uses receipt time, so incremental metering sees delayed closures.

The message ID is hashed to a bounded event key; without one, a canonical envelope digest is used. Deduplication and lifecycle updates commit atomically before acknowledgement. Redelivery cannot create another transition. Database/lock failures retry; malformed events are recorded safely and acknowledged to avoid poison-message loops.

## Out-of-order safety and limits

Events older than the latest accepted observation, at/before the current period start, or inconsistent with the known old state are quarantined. Unknown instances request initial REST discovery. Events after a closed/deleted resource require reconciliation. A later poll corrects the present; it cannot invent a missed historical timestamp.

Closed lifecycle, usage and financial rows are never silently rewritten. Quarantined history remains visible for operator investigation; automatic retrospective replay is not implemented. Exact-second billing therefore applies to accepted trustworthy ordered events. Polling recovery and ambiguous delayed events retain the limits of their evidence. Broker delay, polling lock duration and metering work affect dashboard latency.

Closed Cinder periods are materialized only once attached-VM history is sufficiently observed through their interval. Unmaterialized closed volumes are retried on subsequent runs. Many non-billable/unresolved closed volumes can increase these retry scans; this favors correctness in the existing monolith.

## Migration and startup

Back up PostgreSQL using the existing README backup procedure. Additive Alembic revision 0005 creates processed_notifications and makes observation sync_run_id nullable for event-sourced observations. Existing polling observations retain their IDs. Lifecycle, usage, prices, charges and invoices are preserved. Downgrade refuses to discard a populated notification audit table.

```sh
docker compose build billing-app
docker compose run --rm billing-app alembic upgrade head
docker compose up -d billing-app
```

Configure the environment and Nova topic before enabling consumption. Keep polling enabled. Rates are not reseeded automatically. Existing sync -> meter -> rate callbacks remain; accepted notifications also request metering/rating.

## Diagnostics and validation

- GET /api/v1/diagnostics/openstack: polling health, notification connection and persisted issue count.
- GET /api/v1/diagnostics/notifications: paginated event/receipt times, normalized fields, status and issue code.
- Internal summaries flag incomplete quality when notifications are enabled but disconnected or quarantined issues exist.
- Data Quality/reconciliation details and existing VM lifecycle/usage/cost trace remain available.

Tests cover the exact 4.75-hour golden total of 726,750 VND, ten-hour SHUTOFF zero, one-hour ACTIVE plus nine-hour SHUTOFF, exact seconds, resize/delete, unknown states, duplicate/old/malformed events, queue isolation and secret suppression. Native PostgreSQL tests cover migration preservation, timestamp updates, model alignment and advisory locking.

For live acceptance: supply private broker/API credentials, inspect a sanitized deployed payload and routing, verify independent subscriptions, toggle a test VM through ACTIVE/SHUTOFF/ACTIVE, compare event/ledger timestamps and manual Decimal totals, then test restart/redelivery and REST fallback after a controlled notification outage. Do not disrupt other queues. No live Nova/RabbitMQ endpoint was supplied; deployment integration remains pending.


## Engineering validation record

2026-09-17: pre-change SQLite regression passed 113 tests (PostgreSQL excluded). The combined updated suite passed 142 tests, including eight native PostgreSQL tests, in 122.23 seconds with six dependency deprecation warnings. Ruff and JavaScript syntax checks passed. After the final logging/TLS/flavor metadata refinements, all 21 focused notification tests passed again in 20.39 seconds.

The PostgreSQL notification test upgrades a populated 0004 ledger to 0005, checks existing period preservation, applies an exact-second state transition, verifies deduplication and advisory locking, and compares Alembic schema to ORM metadata. The messaging subscription test uses Kombu's memory transport, not live RabbitMQ. No Docker restart, live Nova deployment configuration or real broker latency claim is made.

Changed areas: notification adapter/processor/consumer and inbox model; additive migration 0005; lifecycle receipt-time support; default policy/version and Cinder ACTIVE intersections; safe diagnostics and small dashboard labels; dependency/configuration documentation and tests. Existing frameworks, ORM, polling scheduler, price books and commercial modules are preserved.
