mock_provider "google" {}

variables {
  project_id      = "portfolio-capacity-test"
  container_image = "europe-west1-docker.pkg.dev/portfolio-capacity-test/ml-inference/inference@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

run "cloud_run_capacity_contract" {
  command = plan

  assert {
    condition = (
      one(google_cloud_run_v2_service.inference.template).max_instance_request_concurrency == 8
    )
    error_message = "The one-vCPU inference container must have an explicit concurrency budget."
  }

  assert {
    condition = (
      one(google_cloud_run_v2_service.inference.template).timeout == "60s"
    )
    error_message = "Inference requests must have an explicit bounded timeout."
  }

  assert {
    condition = (
      one(one(google_cloud_run_v2_service.inference.template).scaling).min_instance_count == 0 &&
      one(one(google_cloud_run_v2_service.inference.template).scaling).max_instance_count == 10
    )
    error_message = "Cloud Run scaling must preserve the reviewed zero-to-ten instance envelope."
  }

  assert {
    condition = (
      one(one(one(google_cloud_run_v2_service.inference.template).containers).resources).limits["cpu"] == "1" &&
      one(one(one(google_cloud_run_v2_service.inference.template).containers).resources).limits["memory"] == "1Gi"
    )
    error_message = "Capacity evidence must stay bound to the reviewed one-vCPU, 1 GiB container."
  }
}

run "reject_inverted_scaling_envelope" {
  command = plan

  variables {
    cloud_run_min_instances = 4
    cloud_run_max_instances = 3
  }

  expect_failures = [google_cloud_run_v2_service.inference]
}

run "accept_reviewed_override" {
  command = plan

  variables {
    cloud_run_min_instances           = 2
    cloud_run_max_instances           = 20
    cloud_run_max_concurrency         = 4
    cloud_run_request_timeout_seconds = 30
  }

  assert {
    condition = (
      one(google_cloud_run_v2_service.inference.template).max_instance_request_concurrency == 4 &&
      one(google_cloud_run_v2_service.inference.template).timeout == "30s" &&
      one(one(google_cloud_run_v2_service.inference.template).scaling).min_instance_count == 2 &&
      one(one(google_cloud_run_v2_service.inference.template).scaling).max_instance_count == 20
    )
    error_message = "Reviewed capacity overrides must flow to the planned Cloud Run revision exactly."
  }
}
