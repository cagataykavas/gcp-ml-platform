# Keyless Cloud Run release boundary

The repository provisions a dedicated GitHub Actions trust path for staging a
digest-pinned Cloud Run revision. It replaces long-lived service-account key JSON with
short-lived credentials issued through Google Cloud Workload Identity Federation.

## Admission boundary

The provider accepts a GitHub OIDC token only when all of these claims match:

- immutable repository ID `1330974142`;
- immutable owner ID `104207794`;
- branch `main` with branch ref type;
- event `workflow_dispatch`;
- workflow `cagataykavas/gcp-ml-platform/.github/workflows/stage-cloud-run.yml` from `main`;
- GitHub environment `production`.

Numeric IDs prevent a deleted repository or renamed owner from being impersonated by
re-registering the old name. Workflow, event, branch, and environment binding prevents
an unrelated pull-request job or another workflow in the same repository from obtaining
the deployment identity.

The accepted principal may impersonate only the dedicated
`github-cloud-run-deployer` service account. That account receives:

- `roles/run.developer` for Cloud Run service/revision updates;
- `roles/artifactregistry.reader` on only the inference image repository; and
- `roles/iam.serviceAccountUser` on only the Cloud Run runtime service account.

It cannot publish images, administer Artifact Registry, act as arbitrary service
accounts, or receive Cloud Run Admin through this configuration.

## Bootstrap and repository configuration

The identity resources must be applied once by an existing trusted administrator; the
new identity cannot bootstrap its own trust. After `terraform apply`, configure these
GitHub repository variables:

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | Target Google Cloud project ID |
| `GCP_REGION` | Terraform region, normally `europe-west1` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Terraform output `github_workload_identity_provider` |
| `GCP_DEPLOYER_SERVICE_ACCOUNT` | Terraform output `github_deployer_service_account` |

Configure protection on the `production` GitHub environment: restrict deployment to
`main`, require appropriate reviewers, and prevent administrator bypass if that matches
the operating policy. The GCP attribute condition verifies the environment claim but
does not create or configure GitHub's approval rules.

Workload Identity Pool, provider, and IAM updates can take several minutes to propagate.
Do not work around propagation by adding a service-account key.

## Staging contract

Run the `stage-cloud-run` workflow manually with a digest such as:

```text
sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

The workflow constructs the complete Artifact Registry URI for
`ml-inference/inference`, rejects tags and malformed digests, exchanges its GitHub OIDC
token, and creates a Cloud Run revision with `--no-traffic`. It then verifies both
revision readiness and the exact deployed image reference before writing a bounded job
summary.

Terraform owns the service configuration but delegates the container image field to the
release workflow after the initial bootstrap. Its targeted `ignore_changes` rule prevents
a later infrastructure apply from silently restoring an older digest; all other service
configuration remains drift-managed by Terraform.

Staging is deliberately separate from promotion. A ready container has not yet proved
model quality, request correctness, latency, or safe database/message interactions.
Traffic should move only after authenticated smoke tests and the repository's monitoring
or release gates admit the candidate.

## Trust and operational limits

- GitHub Actions, the selected official actions, GitHub's OIDC issuer, Google STS, IAM
  Credentials, and the initial Terraform administrator remain trust dependencies.
- Major action tags are readable but mutable upstream references. A stricter deployment
  should pin reviewed action commit SHAs and use dependency update automation.
- `roles/run.developer` is project-scoped because it must create a revision for a service;
  use a custom role if the project contains unrelated Cloud Run workloads.
- The workflow assumes the image was already scanned, signed, and published. Digest
  pinning proves byte identity, not provenance or vulnerability status.
- `--no-traffic` avoids automatic production exposure, but a tagged or authenticated
  revision can still be invoked by principals that already possess invocation access.

The next increment is a signed provenance and vulnerability admission step followed by
an authenticated candidate smoke test and a separately authorized traffic-promotion
workflow.

## References

- [Google Cloud: Workload Identity Federation with deployment pipelines](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
- [Google Cloud: Cloud Run deployment permissions](https://cloud.google.com/run/docs/reference/iam/roles)
- [GitHub: OpenID Connect token claims](https://docs.github.com/actions/reference/security/oidc)
- [google-github-actions/auth: Workload Identity Federation](https://github.com/google-github-actions/auth#workload-identity-federation)
