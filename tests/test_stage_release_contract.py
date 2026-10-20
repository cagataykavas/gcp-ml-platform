import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/stage-cloud-run.yml"
WORKFLOW_DIRECTORY = Path(__file__).parents[1] / ".github/workflows"
TERRAFORM = Path(__file__).parents[1] / "infra/main.tf"

APPROVED_ACTIONS = {
    "actions/checkout": {
        "08eba0b27e820071cde6df949e0beb9ba4906955": "v4.3.0",
        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0": "v7.0.0",
    },
    "actions/setup-python": {
        "a26af69be951a213d495a4c3e4e4022e16d87065": "v5.6.0",
    },
    "google-github-actions/auth": {
        "7c6bc770dae815cd3e89ee6cdf493a5fab2cc093": "v3.0.0",
    },
    "google-github-actions/setup-gcloud": {
        "aa5489c8933f4cc7a4f7d45035b3b1440c9c10db": "v3.0.1",
    },
    "hashicorp/setup-terraform": {
        "b9cd54a3c349d3f38e8881555d616ced269862dd": "v3.1.2",
    },
}

ACTION_REFERENCE = re.compile(
    r"^\s*-?\s*uses:\s*(?P<action>[^@\s]+)@(?P<ref>[^\s#]+)"
    r"(?:\s+#\s*(?P<version>\S+))?\s*$"
)


class WorkflowSupplyChainTests(unittest.TestCase):
    def test_every_remote_action_uses_an_approved_commit(self) -> None:
        references: list[tuple[str, str, str, str | None]] = []
        workflows = sorted(
            [
                *WORKFLOW_DIRECTORY.glob("*.yml"),
                *WORKFLOW_DIRECTORY.glob("*.yaml"),
            ]
        )
        self.assertEqual(len(workflows), 3)
        for workflow in workflows:
            for line_number, line in enumerate(
                workflow.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = ACTION_REFERENCE.match(line)
                if not match or match.group("action").startswith(("./", "docker://")):
                    continue
                references.append(
                    (
                        str(workflow.relative_to(WORKFLOW_DIRECTORY.parent.parent)),
                        str(line_number),
                        match.group("action"),
                        match.group("ref"),
                    )
                )
                action = match.group("action")
                commit = match.group("ref")
                self.assertRegex(commit, r"^[0-9a-f]{40}$")
                self.assertIn(action, APPROVED_ACTIONS)
                self.assertIn(commit, APPROVED_ACTIONS[action])
                self.assertEqual(
                    match.group("version"), APPROVED_ACTIONS[action][commit]
                )

        self.assertEqual(len(references), 9)

    def test_approved_commits_are_full_lowercase_sha1_values(self) -> None:
        for commits in APPROVED_ACTIONS.values():
            for commit in commits:
                self.assertRegex(commit, r"^[0-9a-f]{40}$")


class StageReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_uses_manual_release_intent_and_production_environment(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("environment: production", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)

    def test_requests_only_oidc_and_read_permissions(self) -> None:
        permissions = re.search(
            r"(?ms)^permissions:\n(?P<body>(?:  [^\n]+\n)+)", self.workflow
        )
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
        self.assertIn(
            "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093",
            self.workflow,
        )
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
