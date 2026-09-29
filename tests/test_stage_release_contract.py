import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/stage-cloud-run.yml"
TERRAFORM = Path(__file__).parents[1] / "infra/main.tf"


class StageReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_uses_manual_release_intent_and_production_environment(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("environment: production", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)

    def test_requests_only_oidc_and_read_permissions(self) -> None:
        permissions = re.search(r"(?ms)^permissions:\n(?P<body>(?:  [^\n]+\n)+)", self.workflow)
        self.assertIsNotNone(permissions)
        self.assertEqual(
            permissions.group("body").strip().splitlines(),
            ["contents: read", "  id-token: write"],
        )

    def test_rejects_tags_and_non_sha256_image_references(self) -> None:
        self.assertIn(r"^sha256:[0-9a-f]{64}$", self.workflow)
        self.assertIn("${IMAGE_NAME}@${IMAGE_DIGEST}", self.workflow)
        self.assertNotIn("${IMAGE_NAME}:${IMAGE_DIGEST}", self.workflow)

    def test_exchanges_oidc_without_a_static_service_account_key(self) -> None:
        self.assertIn("google-github-actions/auth@v3", self.workflow)
        self.assertIn("workload_identity_provider:", self.workflow)
        self.assertIn("service_account:", self.workflow)
        self.assertNotIn("credentials_json:", self.workflow)
        self.assertNotIn("secrets.", self.workflow)

    def test_stages_without_traffic_and_checks_the_created_revision(self) -> None:
        self.assertIn("--no-traffic", self.workflow)
        self.assertIn("status.latestCreatedRevisionName", self.workflow)
        self.assertIn("status.conditions[?type=Ready].status", self.workflow)
        self.assertIn('"$deployed_image" != "$image_uri"', self.workflow)

    def test_prevents_overlapping_release_attempts(self) -> None:
        self.assertIn("group: cloud-run-production-stage", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_terraform_does_not_revert_workflow_owned_image_revisions(self) -> None:
        terraform = TERRAFORM.read_text(encoding="utf-8")
        self.assertIn("ignore_changes = [template[0].containers[0].image]", terraform)


if __name__ == "__main__":
    unittest.main()
