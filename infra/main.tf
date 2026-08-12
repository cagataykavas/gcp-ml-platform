terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-ml-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = "ml-inference"
  format        = "DOCKER"
}

resource "google_cloud_run_v2_service" "inference" {
  name     = "ml-inference"
  location = var.region

  template {
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
    containers {
      image = var.container_image
      resources { limits = { cpu = "1", memory = "1Gi" } }
      env { name = "MODEL_BUCKET"; value = google_storage_bucket.artifacts.name }
    }
  }
}

resource "google_pubsub_topic" "prediction_events" {
  name = "prediction-events"
}

resource "google_bigquery_dataset" "analytics" {
  dataset_id = "ml_analytics"
  location   = var.region
}

variable "project_id" { type = string }
variable "region" { type = string; default = "europe-west1" }
variable "container_image" { type = string }

output "artifact_bucket" { value = google_storage_bucket.artifacts.name }
output "cloud_run_uri" { value = google_cloud_run_v2_service.inference.uri }
output "prediction_topic" { value = google_pubsub_topic.prediction_events.name }
