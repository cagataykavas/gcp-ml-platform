mock_provider "google" {}

override_resource {
  target = google_service_account.github_deployer
  values = {
    email = "github-cloud-run-deployer@portfolio-test-123.iam.gserviceaccount.com"
    name  = "projects/portfolio-test-123/serviceAccounts/github-cloud-run-deployer@portfolio-test-123.iam.gserviceaccount.com"
  }
}

override_resource {
  target = google_service_account.runtime
  values = {
    email = "ml-inference-runtime@portfolio-test-123.iam.gserviceaccount.com"
    name  = "projects/portfolio-test-123/serviceAccounts/ml-inference-runtime@portfolio-test-123.iam.gserviceaccount.com"
  }
}

variables {
  project_id      = "portfolio-test-123"
  region          = "europe-west1"
  container_image = "europe-west1-docker.pkg.dev/portfolio-test-123/ml-inference/inference@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

run "keyless_cloud_run_release_contract" {
  command = apply

  assert {
    condition = (
      google_iam_workload_identity_pool_provider.github.workload_identity_pool_provider_id == "ml-platform"
    )
    error_message = "The provider ID must remain valid and must not use Google's reserved gcp- prefix."
  }

  assert {
    condition = google_iam_workload_identity_pool_provider.github.oidc[0].issuer_uri == (
      "https://token.actions.githubusercontent.com/"
    )
    error_message = "The provider must trust only the GitHub Actions OIDC issuer."
  }

  assert {
    condition = google_iam_workload_identity_pool_provider.github.attribute_mapping["google.subject"] == (
      "assertion.sub"
    )
    error_message = "The OIDC subject must remain mapped to google.subject."
  }

  assert {
    condition = google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.repository_id"] == (
      "assertion.repository_id"
    )
    error_message = "The immutable repository ID claim must be mapped."
  }

  assert {
    condition = google_iam_workload_identity_pool_provider.github.attribute_mapping["attribute.repository_owner_id"] == (
      "assertion.repository_owner_id"
    )
    error_message = "The immutable owner ID claim must be mapped."
  }

  assert {
    condition = alltrue([
      strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "repository_id == '1330974142'"),
      strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "repository_owner_id == '104207794'"),
      strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "ref == 'refs/heads/main'"),
      strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "event_name == 'workflow_dispatch'"),
      strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "workflow_ref == 'cagataykavas/gcp-ml-platform/.github/workflows/stage-cloud-run.yml@refs/heads/main'"),
      strcontains(google_iam_workload_identity_pool_provider.github.attribute_condition, "environment == 'production'"),
    ])
    error_message = "OIDC admission must bind immutable identity, branch, event, workflow and environment."
  }

  assert {
    condition     = google_service_account_iam_member.github_identity_impersonation.role == "roles/iam.workloadIdentityUser"
    error_message = "The federated principal may only impersonate through Workload Identity User."
  }

  assert {
    condition = endswith(
      google_service_account_iam_member.github_identity_impersonation.member,
      "/attribute.repository_id/1330974142"
    )
    error_message = "Service-account impersonation must be scoped to the immutable repository ID."
  }

  assert {
    condition     = google_project_iam_member.github_cloud_run_developer.role == "roles/run.developer"
    error_message = "The deployer must not receive Cloud Run Admin."
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.github_image_reader.role == "roles/artifactregistry.reader"
    error_message = "The staging workflow only needs to read pre-published image digests."
  }

  assert {
    condition     = google_service_account_iam_member.github_runtime_user.role == "roles/iam.serviceAccountUser"
    error_message = "The deployer needs only actAs access on the dedicated runtime identity."
  }

  assert {
    condition = toset(keys(google_project_service.ci_identity)) == toset([
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "sts.googleapis.com",
    ])
    error_message = "Service-account federation requires IAM, IAM Credentials and STS APIs."
  }
}
