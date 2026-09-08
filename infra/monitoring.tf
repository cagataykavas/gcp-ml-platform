resource "google_monitoring_alert_policy" "cloud_run_5xx" {
  display_name = "ml-inference Cloud Run 5xx"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "Cloud Run 5xx request rate"

    condition_threshold {
      filter = <<-EOT
        resource.type = "cloud_run_revision"
        AND metric.type = "run.googleapis.com/request_count"
        AND metric.label."response_code_class" = "5xx"
        AND resource.label."service_name" = "${google_cloud_run_v2_service.inference.name}"
      EOT

      comparison      = "COMPARISON_GT"
      threshold_value = 0.05
      duration        = "120s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  documentation {
    content   = "Cloud Run is returning sustained server errors. Inspect revision logs, rollout health, and downstream dependencies."
    mime_type = "text/markdown"
  }

  user_labels = {
    workload = "ml-platform"
    signal   = "availability"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "cloud_run_latency" {
  display_name = "ml-inference Cloud Run p95 latency"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "Cloud Run p95 request latency"

    condition_threshold {
      filter = <<-EOT
        resource.type = "cloud_run_revision"
        AND metric.type = "run.googleapis.com/request_latencies"
        AND resource.label."service_name" = "${google_cloud_run_v2_service.inference.name}"
      EOT

      comparison      = "COMPARISON_GT"
      threshold_value = 1000
      duration        = "180s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }

  documentation {
    content   = "Cloud Run p95 request latency is above one second. Check cold starts, model load time, CPU saturation, and downstream calls."
    mime_type = "text/markdown"
  }

  user_labels = {
    workload = "ml-platform"
    signal   = "latency"
  }

  depends_on = [google_project_service.required]
}
