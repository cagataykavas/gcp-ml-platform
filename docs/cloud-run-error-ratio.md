# Cloud Run 5xx ratio alert

The Cloud Run request-count metric is a delta counter split by response-code
class. Applying `ALIGN_RATE` to only the 5xx series measures errors per second;
it does **not** measure the fraction of requests that failed. An absolute error
arrival threshold behaves differently as traffic changes and can be both noisy
for a quiet service and insensitive for a busy one.

The `cloud_run_5xx` policy now computes:

```text
sum(5xx requests over 5 minutes) / sum(all requests over 5 minutes)
```

Both sides use `ALIGN_DELTA`, reduce response-code and revision series with
`REDUCE_SUM`, and retain only the Cloud Run service label. Matching alignment,
reduction and label sets are required for a meaningful Cloud Monitoring ratio.
The alert opens when the ratio remains above `0.05` for five minutes. Missing
traffic is treated as inactive rather than as an outage signal.

## Incident routing contract

Both Cloud Run serving policies require one or more existing Cloud Monitoring
notification-channel resource names. Terraform rejects an empty set, short IDs,
channel labels and malformed resource names before apply. It then binds the exact
same sorted channel set to the 5xx-ratio and p95-latency policies.

Open incidents re-notify every 30 minutes and automatically close after 30 minutes
without observed data. Re-notification keeps a sustained serving failure visible;
auto-close bounds stale incidents after a service stops emitting the underlying
metric. Neither setting is a substitute for escalation policy in the downstream
email, SMS, PagerDuty, Slack or webhook destination.

Channel resources are deliberately inputs rather than repository-managed objects.
Their endpoint configuration can contain organization-specific contact details or
secrets and should be provisioned in a separately governed operations stack. Supply
full names such as:

```hcl
monitoring_notification_channels = [
  "projects/my-platform/notificationChannels/1234567890",
]
```

## Credential-free verification

The native Terraform test uses a mocked Google provider, so it cannot create or
modify cloud resources and requires no GCP credentials:

```bash
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra fmt -check -diff -recursive
terraform -chdir=infra validate
terraform -chdir=infra test -test-directory=tests
```

The assertions pin the ratio threshold, numerator/denominator filters, matching
aggregation semantics, sustained duration, missing-data behavior, shared channel
routing, re-notification and stale-incident closure. Negative plans prove that an
empty or malformed routing set fails closed. A separate path-filtered workflow
executes the same contract on pull requests that change infrastructure.

## Limitations

This policy measures server-error proportion, not customer-visible availability.
It has no minimum-traffic condition, so one error in a very small request sample
can still breach the ratio. This configuration proves that incidents are routed,
not that a human received or acknowledged them. Multi-window burn-rate alerting,
synthetic probes, escalation and deployment rollback remain deployment-specific
concerns.
