terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  type        = string
  description = "GCP project used by the public ML platform reference deployment."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "Primary regional location for serving and storage resources."
}

variable "container_image" {
  type        = string
  description = "Immutable container image URI for the inference service."
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_services = toset([
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-ml-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 15
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = "ml-inference"
  description   = "Container images for the public GCP ML reference platform"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "prediction_events" {
  name = "prediction-events"

  message_retention_duration = "86600s"

  depends_on = [google_project_service.required]
}

resource "google_bigquery_dataset" "analytics" {
  dataset_id                 = "ml_analytics"
  friendly_name              = "ML Analytics and Monitoring"
  description                = "Synthetic prediction, drift and delayed-label monitoring data"
  location                   = var.region
  delete_contents_on_destroy = false

  labels = {
    workload = "ml-platform"
    data     = "synthetic"
  }

  depends_on = [google_project_service.required]
}

resource "google_service_account" "runtime" {
  account_id   = "ml-inference-runtime"
  display_name = "ML Inference Runtime"
  description  = "Runtime identity for the public Cloud Run ML reference service"
}

resource "google_storage_bucket_iam_member" "runtime_model_reader" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_pubsub_topic_iam_member" "runtime_prediction_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.prediction_events.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_bigquery_dataset_iam_member" "runtime_monitoring_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.analytics.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "inference" {
  name                = "ml-inference"
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "MODEL_BUCKET"
        value = google_storage_bucket.artifacts.name
      }

      env {
        name  = "PREDICTION_TOPIC"
        value = google_pubsub_topic.prediction_events.name
      }

      env {
        name  = "MONITORING_DATASET"
        value = google_bigquery_dataset.analytics.dataset_id
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_storage_bucket_iam_member.runtime_model_reader,
    google_pubsub_topic_iam_member.runtime_prediction_publisher,
    google_bigquery_dataset_iam_member.runtime_monitoring_writer,
  ]
}

output "artifact_bucket" {
  value       = google_storage_bucket.artifacts.name
  description = "Versioned model-artifact bucket."
}

output "artifact_repository" {
  value       = google_artifact_registry_repository.containers.name
  description = "Artifact Registry repository for inference images."
}

output "cloud_run_uri" {
  value       = google_cloud_run_v2_service.inference.uri
  description = "Cloud Run inference-service URI. Authentication is deployment-policy specific."
}

output "prediction_topic" {
  value       = google_pubsub_topic.prediction_events.name
  description = "Pub/Sub topic for asynchronous prediction telemetry."
}

output "monitoring_dataset" {
  value       = google_bigquery_dataset.analytics.dataset_id
  description = "BigQuery dataset used by synthetic monitoring examples."
}
