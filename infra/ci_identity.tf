variable "github_repository" {
  type        = string
  default     = "cagataykavas/gcp-ml-platform"
  description = "GitHub owner/repository allowed to request deployment credentials."

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be an owner/repository pair."
  }
}

variable "github_repository_id" {
  type        = string
  default     = "1330974142"
  description = "Immutable GitHub repository ID used to prevent name-reuse attacks."

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.github_repository_id))
    error_message = "github_repository_id must be a positive numeric GitHub ID."
  }
}

variable "github_repository_owner_id" {
  type        = string
  default     = "104207794"
  description = "Immutable GitHub owner ID used to prevent owner-name reuse attacks."

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must be a positive numeric GitHub ID."
  }
}

variable "github_deploy_branch" {
  type        = string
  default     = "main"
  description = "Only this branch may exchange GitHub OIDC tokens for deployment credentials."

  validation {
    condition = (
      length(var.github_deploy_branch) > 0 &&
      length(var.github_deploy_branch) <= 200 &&
      can(regex("^[A-Za-z0-9._/-]+$", var.github_deploy_branch)) &&
      !strcontains(var.github_deploy_branch, "..")
    )
    error_message = "github_deploy_branch must be a bounded Git branch name."
  }
}

locals {
  github_deploy_workflow = "${var.github_repository}/.github/workflows/stage-cloud-run.yml@refs/heads/${var.github_deploy_branch}"

  github_oidc_attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
    "assertion.ref == 'refs/heads/${var.github_deploy_branch}'",
    "assertion.ref_type == 'branch'",
    "assertion.event_name == 'workflow_dispatch'",
    "assertion.workflow_ref == '${local.github_deploy_workflow}'",
    "assertion.environment == 'production'",
  ])

  github_repository_principal = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
  deployer_member             = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_project_service" "ci_identity" {
  for_each = toset([
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-deployments"
  display_name              = "GitHub deployments"
  description               = "Short-lived identity pool for the governed Cloud Run release workflow"
  disabled                  = false

  depends_on = [google_project_service.ci_identity]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "gcp-ml-platform"
  display_name                       = "gcp-ml-platform release"
  description                        = "Repository, workflow, branch, event and environment-scoped GitHub OIDC"

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.environment"         = "assertion.environment"
    "attribute.event_name"          = "assertion.event_name"
    "attribute.ref"                 = "assertion.ref"
    "attribute.ref_type"            = "assertion.ref_type"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.workflow_ref"        = "assertion.workflow_ref"
  }

  attribute_condition = local.github_oidc_attribute_condition

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com/"
  }
}

resource "google_service_account" "github_deployer" {
  account_id   = "github-cloud-run-deployer"
  display_name = "GitHub Cloud Run deployer"
  description  = "Keyless, short-lived identity for staging digest-pinned Cloud Run revisions"
}

resource "google_service_account_iam_member" "github_identity_impersonation" {
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.github_repository_principal
}

resource "google_project_iam_member" "github_cloud_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = local.deployer_member
}

resource "google_artifact_registry_repository_iam_member" "github_image_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.containers.location
  repository = google_artifact_registry_repository.containers.repository_id
  role       = "roles/artifactregistry.reader"
  member     = local.deployer_member
}

resource "google_service_account_iam_member" "github_runtime_user" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member
}

output "github_workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "Set this as the GCP_WORKLOAD_IDENTITY_PROVIDER GitHub repository variable."
}

output "github_deployer_service_account" {
  value       = google_service_account.github_deployer.email
  description = "Set this as the GCP_DEPLOYER_SERVICE_ACCOUNT GitHub repository variable."
}
