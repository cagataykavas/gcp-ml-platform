# Pub/Sub event-admission boundary

The Dataflow risk-feature example treats the Pub/Sub source timestamp as the
trusted observation time and the payload's `event_time` as business time. The
dependency-free `gcpml.event_admission` module validates the message before its
business timestamp can affect a Beam window.

The contract rejects duplicate JSON fields, missing or blank identifiers,
non-finite/negative amounts, invalid country codes, timezone-naive timestamps,
oversized payloads, events beyond the lateness budget, and events beyond the
future-clock-skew budget. Expected rejections become bounded BigQuery DLQ rows
with a stable reason code, retryability decision, SHA-256 identity, truncation
flag, and observation timestamp. Unexpected code failures are not converted
into ordinary bad data: they fail the pipeline for operator investigation.

When submitting to Dataflow, pass `--setup_file ./setup.py` through the
example's unknown/Beam arguments so workers receive the same versioned
`gcpml` contract module as the submitter. Pin the source revision used for a
job; do not depend on an ambient worker image containing this package.

## Operating guidance

- Calibrate lateness from measured producer-to-Pub/Sub delay, not from a demo default.
- Route `retryable=true` records through a bounded delayed retry path. Never redrive blindly.
- Deduplicate redrive attempts by `payload_sha256` plus the source message identity.
- Restrict access to raw payload excerpts; hashes are identities, not anonymization.
- Alert on reason-code rates and DLQ age, not only total DLQ volume.
- Keep schema changes versioned and evaluate producer/consumer compatibility separately.

## Trust boundary and limitations

Ingress admission prevents clearly invalid evidence from entering feature
windows, but it does not replace Pub/Sub schema governance, Dataflow watermark
monitoring, BigQuery deduplication, or a replay ledger. A message may pass the
per-event lateness check and still arrive behind a runner watermark shaped by
other partitions. Production pipelines should expose dropped-late-data metrics
and use an explicit correction/reconciliation path for finalized windows.
