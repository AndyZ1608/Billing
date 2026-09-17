# Phase 4 design

Billing consumes immutable rated charge snapshots. It never calls OpenStack or resolves prices.

New tables: billing_cycles, invoices, invoice_lines, invoice_charge_links,
billing_adjustments, billing_audit_log and invoice_number_counters. Additive migration 0004.
Cycles capture the metering version used to select charge runs. Invoice identity is unique
per cloud/project/cycle/currency. Charge links record exact intersected periods, amounts,
and quantities. Cumulative rounded allocation of stored amounts conserves totals across
adjacent cycles. Zero-value lines are retained for complete traceability.

All financial mutations use the existing cloud advisory lock plus transactions. PostgreSQL
exclusion constraints prohibit overlapping finalized links to the same charge. Finalized
headers, lines, links and applied adjustments have database guards. Rating refuses to
supersede any charge with a locked link. Numbers use an atomic persistent counter.

Finalization validates charge selection freshness, rating completeness (through a rating
module service), currency, provisional policy, source reconciliation and duplicate billing.
Review precedes finalization. Cycles finalize only when all required invoices are finalized.
Original invoice totals remain immutable; applied signed adjustments are separate records.
Read APIs expose original and effective totals separately. Administration uses configured
operator/admin bearer tokens; no anonymous financial mutations, including development.
