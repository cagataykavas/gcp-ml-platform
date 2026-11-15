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
aggregation semantics, sustained duration and missing-data behavior. A separate
path-filtered workflow executes the same contract on pull requests that change
infrastructure.

## Limitations

This policy measures server-error proportion, not customer-visible availability.
It has no minimum-traffic condition, so one error in a very small request sample
can still breach the ratio. Notification routing, multi-window burn-rate alerting,
synthetic probes and deployment rollback remain deployment-specific concerns.
