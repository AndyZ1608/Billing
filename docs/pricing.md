# Pricing catalog and administration

Phase 3 prices persisted Phase 2 usage. Products and monetary rates live in database tables, not calculation constants. The optional bootstrap values are explicitly TEST data.

## Model

| Entity | Purpose |
| --- | --- |
| `billing_products` | Unique code and one-to-one meter/unit mapping; name, description, compute/storage category and enabled flag |
| `price_books` | Reusable named policy with one currency, code and description |
| `price_book_versions` | Named effective period and DRAFT / ACTIVE / RETIRED status |
| `price_rules` | One product per version, nonnegative Decimal unit price and exact meter billing unit |
| `project_price_book_assignments` | Cloud/project and effective-dated book selection; null project means cloud default |
| `project_price_overrides` | Effective-dated project/product price, currency and reason |
| `pricing_audit_log` | Action, entity UUID/type, before/after snapshots, actor label and UTC timestamp |

The product's meter and unit must exactly match the Phase 2 registry. Price rules do not modify that registry. The seven existing meter mappings are supported; a future meter requires a collector/metering contract change and then a catalog product. A unique constraint prevents ambiguous mapping. Products/books are immutable identities in this implementation; create a new book for another policy/currency. Disabled records do not resolve prices.

Currencies currently accepted are VND and USD. Each book has exactly one. There is no currency conversion. Supporting another currency requires adding its currency/rounding validation and corresponding schema constraint migration.

## Effective dates and activation

All periods use `[effective_from, effective_to)`. End may be null for open-ended. APIs require an ISO timestamp with timezone offset; storage and API output use UTC. Naive timestamps and zero/negative ranges are rejected.

Workflow: create DRAFT version → add rules → activate → optionally retire. Only ACTIVE versions resolve prices. Activation requires at least one enabled rule and no overlapping ACTIVE version in that book. An intentionally incomplete book can activate; missing meter rules remain UNRATED and visible. Gaps are not filled using the preceding or following version.

Rules are append-only and can only be added to a DRAFT. There is no rule update/delete API. Active versions' rules and effective bounds cannot be changed. For a mistaken draft, create a replacement draft with a new label. For a correction to active prices, retire the old version, create a replacement draft with the intended effective bounds, add rules and activate it. Retired versions remain available for charge audit but do not participate in subsequent resolution.

For a planned change to an open-ended active version, retire it and replace it with two ACTIVE versions having adjacent bounds: the old price until the change instant, and the new price from that instant. Existing RATED charges remain stable. Use explicit force re-rating when existing charges should reflect a correction. Missing-price retries can fill UNRATED portions only.

Assignments and overrides reject overlap for the same cloud/project (plus product for overrides). Null-project default assignments also reject overlap. Bounds are immutable; administrative `retire` marks the configuration inactive for resolution, without deleting it. Correct it by creating replacement interval rows, then explicitly re-rate affected usage if needed. Retirement is a configuration correction, not a new effective end timestamp. Set bounded intervals in advance whenever the end is known.

## Resolution precedence

1. Effective project/product override.
2. Effective project price book assignment.
3. Effective cloud default assignment (`project_id: null`).

A selected project book with no active version or missing rule does **not** silently fall back to the default. It produces PRICE_GAP or NO_PRICE_RULE. If there is no project assignment at an instant, the cloud default can apply. If no book is selected, an applicable override can still supply its own currency and rate. If a selected book exists, an override must use that book's currency; mismatch becomes UNRATED/CURRENCY_MISMATCH. A missing/disabled product is NO_PRODUCT. Zero unit price is a valid RATED price, never a missing-price indicator.

The resolver collects relevant assignment, override and ACTIVE-version boundaries, resolves each segment, and combines adjacent segments with identical pricing references. This avoids splitting on an unrelated default-book change while a project override/book supplies the actual price.

## Administrative access and audit

Cost reads are separate from pricing writes and rating-run operations. All write routes require:

```text
Content-Type: application/json
X-Pricing-Admin: true
X-Audit-Actor: operator-name
Authorization: Bearer <configured token>   # only when PRICING_ADMIN_TOKEN is set
```

The UI has explicit administrator controls and holds a token only in the current page input; it does not persist it in browser storage. Header/actor labels are not verified identity. With no token configured, this remains the existing trusted-loopback POC. Keep Compose bound to loopback or deploy behind authenticated HTTPS. The optional token protects financial write routes; it is not a general application login and does not protect existing inventory/metering APIs.

CLI administrators already have shell/database access and do not use the HTTP token. Pricing changes and force re-rating start are audited without storing token/header values. No SDK credentials or settings are added to audit snapshots. Rule creation is recorded as CREATE on `price_rules`; immutable rules are never edited in place.

PostgreSQL rejects active version/assignment/override overlaps even when API checks are bypassed. It also rejects changes/deletion of product/book identities, active pricing definitions and audit entries. SQLite provides the same application validation but is only a development/demo database; use PostgreSQL for exact financial persistence and database-level protections.

## API

Paths under `/api/v1/pricing`:

| Method | Path |
| --- | --- |
| GET / POST | `/products`, `/price-books` |
| GET | `/products/{id}`, `/price-books/{id}` |
| GET / POST | `/price-books/{id}/versions` |
| GET / POST | `/price-book-versions/{id}/rules` |
| POST | `/price-book-versions/{id}/activate`, `/price-book-versions/{id}/retire` |
| GET / POST | `/project-assignments`, `/project-overrides` |
| POST | `/project-assignments/{id}/retire`, `/project-overrides/{id}/retire` |
| GET | `/audit`, `/admin/config` |

Lists use `limit` and `offset` (maximum limit 500). Duplicates/constraint conflicts return 409; invalid input returns 422; missing administrator capability/token returns 403. Send fractional prices as JSON **strings**, not JSON floats. Negative, nonfinite, over-precision prices and unit mismatch are rejected. See `/docs` for generated schemas.

Example rule body:

```json
{"product_id":"PRODUCT_UUID","unit_price":"2000.00000000","billing_unit":"vCPU-hour"}
```

Example cloud default assignment:

```json
{"project_id":null,"price_book_id":"BOOK_UUID","effective_from":"2026-01-01T00:00:00Z","effective_to":null}
```

Example override:

```json
{"project_id":"PROJECT_UUID","product_id":"PRODUCT_UUID","unit_price":"1500","currency":"VND","effective_from":"2026-09-01T00:00:00Z","effective_to":"2026-10-01T00:00:00Z","reason":"Approved project rate correction"}
```

## Bootstrap workflow

1. Run Alembic upgrade and discover projects through existing sync.
2. Create products for the meters to price.
3. Create a book with its currency.
4. Create a draft with effective bounds.
5. Add price rules and review their units/values.
6. Activate the version; overlap validation runs here.
7. Create a cloud default or project-specific assignment; add overrides where needed.
8. Run metering to materialize closed usage, then rating.
9. Review UNRATED reasons and pricing gaps in Data Quality.
10. Inspect project costs and individual charge audit traces.

For **explicit development/bootstrap only**:

```bash
python -m app.cli pricing seed-demo
# Docker equivalent:
docker compose exec billing-app python -m app.cli pricing seed-demo
```

This creates POC-VND once, reuses existing meter products, and never overwrites an existing POC-VND book or default assignment. It does not repair or mutate an existing partial/custom book. Startup and migrations do not seed pricing.

| TEST meter | TEST VND price per meter unit |
| --- | ---: |
| compute.instance | 0 |
| compute.vcpu | 1000 |
| compute.ram | 200 |
| compute.root_disk | 10 |
| compute.ephemeral_disk | 10 |
| storage.volume | 0 |
| storage.volume_capacity | 20 |

These are demonstration values, not production rates. Pricing has no taxes, discounts, tiers, monthly flat fees, minimum charges or commitments. `minimum_quantity=0` and `rounding_mode=HALF_EVEN` reserve explicit rule fields without implementing more complex commercial formulas.


## Billed price corrections (Phase 4)

Retiring/replacing pricing does not alter existing charge or invoice snapshots. Force rerating
cannot supersede invoice-locked charges: the run records BILLED_CHARGE_CONFLICT. Correct the
financial outcome with an approved, applied [adjustment](adjustments.md); changing current
prices alone never rewrites an issued invoice. Unbilled charges retain Phase 3 rerating semantics.


## Internal billing operational view

See [Internal billing](internal-billing.md). The existing domain boundaries remain intact. Persisted final charges remain authoritative; the internal dashboard separately estimates open usage with the existing resolver. INTERNAL-VND is an explicit effective-dated bootstrap, never an automatic historical price rewrite.
