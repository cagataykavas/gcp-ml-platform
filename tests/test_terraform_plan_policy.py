import unittest

from tools.terraform_plan_policy import evaluate_plan


def change(
    address: str,
    resource_type: str,
    after: dict[str, object] | None,
    actions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "address": address,
        "type": resource_type,
        "change": {"actions": actions or ["create"], "after": after},
    }


class TerraformPlanPolicyTest(unittest.TestCase):
    def test_hardened_plan_passes(self) -> None:
        plan = {
            "resource_changes": [
                change(
                    "google_storage_bucket.artifacts",
                    "google_storage_bucket",
                    {
                        "public_access_prevention": "enforced",
                        "uniform_bucket_level_access": True,
                        "force_destroy": False,
                    },
                ),
                change(
                    "google_cloud_run_v2_service.inference",
                    "google_cloud_run_v2_service",
                    {
                        "template": [
                            {
                                "containers": [
                                    {
                                        "image": (
                                            "europe-west1-docker.pkg.dev/project/ml/"
                                            "inference@sha256:" + "a" * 64
                                        )
                                    }
                                ]
                            }
                        ]
                    },
                ),
                change(
                    "google_project_service.required",
                    "google_project_service",
                    {"disable_on_destroy": False},
                ),
            ]
        }

        report = evaluate_plan(plan)

        self.assertTrue(report.passed)
        self.assertEqual(report.resources_checked, 3)
        self.assertEqual(report.findings, ())

    def test_security_regressions_are_reported_together(self) -> None:
        plan = {
            "resource_changes": [
                change(
                    "google_storage_bucket.artifacts",
                    "google_storage_bucket",
                    {
                        "public_access_prevention": "inherited",
                        "uniform_bucket_level_access": False,
                        "force_destroy": True,
                    },
                ),
                change(
                    "google_cloud_run_service_iam_member.public",
                    "google_cloud_run_service_iam_member",
                    {"member": "allUsers", "role": "roles/run.invoker"},
                ),
                change(
                    "google_cloud_run_v2_service.inference",
                    "google_cloud_run_v2_service",
                    {"template": [{"containers": [{"image": "registry/inference:latest"}]}]},
                ),
                change(
                    "google_bigquery_dataset.analytics",
                    "google_bigquery_dataset",
                    {"delete_contents_on_destroy": True},
                ),
                change(
                    "google_project_service.required",
                    "google_project_service",
                    {"disable_on_destroy": True},
                ),
            ]
        }

        report = evaluate_plan(plan)
        rules = {finding.rule for finding in report.findings}

        self.assertFalse(report.passed)
        self.assertEqual(
            rules,
            {
                "storage_public_access_prevention",
                "storage_uniform_access",
                "storage_no_force_destroy",
                "no_public_iam",
                "immutable_container_image",
                "bigquery_no_delete_contents",
                "keep_required_apis_enabled",
            },
        )

    def test_delete_and_replacement_actions_fail_closed(self) -> None:
        plan = {
            "resource_changes": [
                change("google_pubsub_topic.events", "google_pubsub_topic", None, ["delete"]),
                change(
                    "google_service_account.runtime",
                    "google_service_account",
                    {"account_id": "replacement"},
                    ["delete", "create"],
                ),
            ]
        }

        report = evaluate_plan(plan)

        self.assertFalse(report.passed)
        self.assertEqual(
            [finding.rule for finding in report.findings],
            ["no_destructive_changes", "no_destructive_changes"],
        )

    def test_noop_resources_are_not_counted(self) -> None:
        plan = {
            "resource_changes": [
                change(
                    "google_storage_bucket.artifacts",
                    "google_storage_bucket",
                    {"public_access_prevention": "inherited"},
                    ["no-op"],
                )
            ]
        }

        report = evaluate_plan(plan)

        self.assertTrue(report.passed)
        self.assertEqual(report.resources_checked, 0)

    def test_public_members_list_is_blocked(self) -> None:
        plan = {
            "resource_changes": [
                change(
                    "google_storage_bucket_iam_binding.readers",
                    "google_storage_bucket_iam_binding",
                    {
                        "members": ["group:ml@example.com", "allAuthenticatedUsers"],
                        "role": "roles/storage.objectViewer",
                    },
                )
            ]
        }

        report = evaluate_plan(plan)

        self.assertEqual(report.findings[0].rule, "no_public_iam")

    def test_malformed_plan_is_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "resource_changes"):
            evaluate_plan({})
        with self.assertRaisesRegex(TypeError, "address and type"):
            evaluate_plan({"resource_changes": [{"change": {"actions": ["create"]}}]})


if __name__ == "__main__":
    unittest.main()
