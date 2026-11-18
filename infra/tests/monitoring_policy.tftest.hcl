mock_provider "google" {}

variables {
  project_id      = "portfolio-monitoring-test"
  container_image = "europe-west1-docker.pkg.dev/portfolio/ml/inference@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  monitoring_notification_channels = [
    "projects/portfolio-monitoring-test/notificationChannels/1234567890",
    "projects/portfolio-monitoring-test/notificationChannels/9876543210",
  ]
}

run "cloud_run_5xx_uses_request_ratio" {
  command = plan

  assert {
    condition = (
      one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).threshold_value == 0.05
    )
    error_message = "The Cloud Run 5xx ratio must alert above five percent."
  }

  assert {
    condition = (
      strcontains(
        one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).filter,
        "response_code_class"
      ) &&
      !strcontains(
        one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).denominator_filter,
        "response_code_class"
      )
    )
    error_message = "Only the ratio numerator may restrict response_code_class."
  }

  assert {
    condition = (
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).aggregations).alignment_period ==
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).denominator_aggregations).alignment_period &&
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).aggregations).per_series_aligner ==
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).denominator_aggregations).per_series_aligner &&
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).aggregations).cross_series_reducer ==
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).denominator_aggregations).cross_series_reducer &&
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).aggregations).group_by_fields ==
      one(one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).denominator_aggregations).group_by_fields
    )
    error_message = "Ratio numerator and denominator must have identical aggregation semantics."
  }

  assert {
    condition = (
      one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).duration == "300s" &&
      one(one(google_monitoring_alert_policy.cloud_run_5xx.conditions).condition_threshold).evaluation_missing_data == "EVALUATION_MISSING_DATA_INACTIVE"
    )
    error_message = "The ratio must be sustained for five minutes and stay inactive without traffic."
  }
}

run "cloud_run_alerts_route_and_renotify" {
  command = plan

  assert {
    condition = (
      google_monitoring_alert_policy.cloud_run_5xx.notification_channels ==
      google_monitoring_alert_policy.cloud_run_latency.notification_channels &&
      length(google_monitoring_alert_policy.cloud_run_5xx.notification_channels) == 2
    )
    error_message = "Availability and latency alerts must route to the same reviewed channel set."
  }

  assert {
    condition = (
      one(google_monitoring_alert_policy.cloud_run_5xx.alert_strategy).auto_close == "1800s" &&
      one(one(google_monitoring_alert_policy.cloud_run_5xx.alert_strategy).notification_channel_strategy).renotify_interval == "1800s" &&
      one(one(google_monitoring_alert_policy.cloud_run_5xx.alert_strategy).notification_channel_strategy).notification_channel_names == google_monitoring_alert_policy.cloud_run_5xx.notification_channels &&
      one(google_monitoring_alert_policy.cloud_run_latency.alert_strategy).auto_close == "1800s" &&
      one(one(google_monitoring_alert_policy.cloud_run_latency.alert_strategy).notification_channel_strategy).renotify_interval == "1800s" &&
      one(one(google_monitoring_alert_policy.cloud_run_latency.alert_strategy).notification_channel_strategy).notification_channel_names == google_monitoring_alert_policy.cloud_run_latency.notification_channels
    )
    error_message = "Each serving alert must re-notify its configured channels and close stale incidents on the reviewed interval."
  }
}

run "reject_empty_notification_routing" {
  command = plan

  variables {
    monitoring_notification_channels = []
  }

  expect_failures = [var.monitoring_notification_channels]
}

run "reject_short_or_malformed_channel_references" {
  command = plan

  variables {
    monitoring_notification_channels = [
      "notificationChannels/123",
      "projects/portfolio-monitoring-test/notificationChannels/email-primary",
    ]
  }

  expect_failures = [var.monitoring_notification_channels]
}
