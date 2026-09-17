# Phase 3 implementation plan

The Phase 1/2 monolith, SDK collector, lifecycle and metering tables remain intact. Rating reads only final `usage_records` and billing-domain configuration. No live resource allocations determine charges. Open/provisional usage is not persisted by Phase 2 and will not be rated in Phase 3; cost pages explicitly show final usage only.

Add catalog products (unique meter/unit mapping), books, draft/active/retired versions, one rule per product/version, effective-dated project assignments and overrides. A null-project assignment is the cloud default, also effective-dated. Reject overlaps; freeze active rules. Corrections retire old configuration and create replacement versions/assignments/overrides, with audit events. Historical charge snapshots stay unchanged until explicit administrative re-rating.

Resolver precedence: project override, effective project assignment, effective cloud default. A selected project book with a gap does not silently fall back to a default. Override currency must match the selected book when one exists. Split at all assignment, override and active version boundaries; zero is a valid price and missing prices become UNRATED segments.

Add rating runs and immutable financial charge snapshots. One active set of segments per usage across algorithm versions. Normal runs find new or UNRATED usage through indexed anti-joins; unchanged retry results are skipped; normal retries preserve every already RATED segment and fill only UNRATED gaps. Force requires a bounded range, replaces the complete canonical charge set for intersecting usage, marks old rows SUPERSEDED and references the replacing run. Never delete financial history. Query ranges clip charges for reporting.

Serialize pricing writes and rating with the existing cloud advisory lock key plus process gate. Rating processes usage in keyset batches, caches pricing within a run, and uses one savepoint per usage. A failed usage cannot leave half a replacement; completed batches survive a later failure and retry remains idempotent.

Use Decimal precision 50, exact microsecond durations, NUMERIC(30,12) quantities, NUMERIC(24,8) prices and NUMERIC(38,8) stored subtotals. Half-even rounding is centralized; API/UI retain decimal strings. Totals stay separated by currency. No FX, tax, discounts, monthly fees or invoices.

Pricing administration has separate write routes, an explicit admin capability header, optional configured admin token, and audit actor labels. Without a token this remains the existing trusted-loopback POC, not authentication/RBAC. UI clearly identifies admin operations and force replay. PostgreSQL adds overlap/immutability/source-bounds protection; SQLite remains a development option.

Migration 0003 is additive, with no pricing bootstrap during startup and no changes to Phase 1/2 data. Explicit demo/bootstrap creates TEST POC-VND prices idempotently. Validation includes prior regressions, golden splits, zero/missing price, overrides, currencies, replay/rollback, API/admin, upgrade preservation, native PostgreSQL and browser checks. Docker deployment will be attempted if available and otherwise explicitly reported unverified.
