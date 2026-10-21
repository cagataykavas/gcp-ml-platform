import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.verify_staged_revision import (
    MAX_EVIDENCE_BYTES,
    VerificationError,
    load_evidence,
    verify_staged_revision,
)

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/stage-cloud-run.yml"
WORKFLOW_DIRECTORY = Path(__file__).parents[1] / ".github/workflows"
TERRAFORM = Path(__file__).parents[1] / "infra/main.tf"
EXPECTED_REVISION = "ml-inference-run-39550883056-1"
EXPECTED_IMAGE = (
    "europe-west1-docker.pkg.dev/model-prod/ml-inference/inference@sha256:" + "a" * 64
)


def revision_evidence() -> dict:
    return {
        "metadata": {"name": EXPECTED_REVISION},
        "spec": {"containers": [{"image": EXPECTED_IMAGE}]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def service_evidence() -> dict:
    return {
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": EXPECTED_REVISION,
            "traffic": [{"revisionName": "ml-inference-production", "percent": 100}],
        }
    }


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
        self.assertIn('--revision-suffix="$revision_suffix"', self.workflow)
        self.assertIn('created_revision" != "$expected_revision"', self.workflow)
        self.assertIn("scripts/verify_staged_revision.py", self.workflow)
        self.assertIn('--expected-revision "$STAGED_REVISION"', self.workflow)
        self.assertIn('json.load(open(sys.argv[1]))["verification_id"]', self.workflow)

    def test_prevents_overlapping_release_attempts(self) -> None:
        self.assertIn("group: cloud-run-production-stage", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_terraform_does_not_revert_workflow_owned_image_revisions(self) -> None:
        terraform = TERRAFORM.read_text(encoding="utf-8")
        self.assertIn("ignore_changes = [template[0].containers[0].image]", terraform)


class StagedRevisionVerificationTests(unittest.TestCase):
    def test_accepts_exact_ready_digest_with_no_candidate_traffic(self) -> None:
        report = verify_staged_revision(
            revision_evidence(),
            service_evidence(),
            expected_revision=EXPECTED_REVISION,
            expected_image=EXPECTED_IMAGE,
        )

        self.assertTrue(report["accepted"])
        self.assertEqual(report["traffic_percent"], 0)
        self.assertRegex(report["verification_id"], r"^[0-9a-f]{64}$")

    def test_verification_is_deterministic(self) -> None:
        first = verify_staged_revision(
            revision_evidence(),
            service_evidence(),
            expected_revision=EXPECTED_REVISION,
            expected_image=EXPECTED_IMAGE,
        )
        second = verify_staged_revision(
            revision_evidence(),
            service_evidence(),
            expected_revision=EXPECTED_REVISION,
            expected_image=EXPECTED_IMAGE,
        )

        self.assertEqual(first, second)

    def test_rejects_revision_identity_image_and_readiness_drift(self) -> None:
        cases = []
        wrong_name = revision_evidence()
        wrong_name["metadata"]["name"] = "ml-inference-other"
        cases.append(wrong_name)
        wrong_image = revision_evidence()
        wrong_image["spec"]["containers"][0]["image"] = EXPECTED_IMAGE[:-1] + "b"
        cases.append(wrong_image)
        unready = revision_evidence()
        unready["status"]["conditions"][0]["status"] = "False"
        cases.append(unready)

        for evidence in cases:
            with self.subTest(evidence=evidence), self.assertRaises(VerificationError):
                verify_staged_revision(
                    evidence,
                    service_evidence(),
                    expected_revision=EXPECTED_REVISION,
                    expected_image=EXPECTED_IMAGE,
                )

    def test_rejects_created_or_ready_revision_races(self) -> None:
        for field in ("latestCreatedRevisionName", "latestReadyRevisionName"):
            service = service_evidence()
            service["status"][field] = "ml-inference-other"
            with self.subTest(field=field), self.assertRaises(VerificationError):
                verify_staged_revision(
                    revision_evidence(),
                    service,
                    expected_revision=EXPECTED_REVISION,
                    expected_image=EXPECTED_IMAGE,
                )

    def test_rejects_revision_and_latest_traffic_targets(self) -> None:
        traffic_cases = [
            [{"revisionName": EXPECTED_REVISION, "percent": 1}],
            [{"revisionName": EXPECTED_REVISION, "tag": "candidate", "percent": 0}],
            [{"latestRevision": True, "percent": 100}],
        ]
        for traffic in traffic_cases:
            service = service_evidence()
            service["status"]["traffic"] = traffic
            with self.subTest(traffic=traffic), self.assertRaises(VerificationError):
                verify_staged_revision(
                    revision_evidence(),
                    service,
                    expected_revision=EXPECTED_REVISION,
                    expected_image=EXPECTED_IMAGE,
                )

    def test_rejects_invalid_release_intent(self) -> None:
        cases = [
            ("UPPERCASE", EXPECTED_IMAGE),
            (EXPECTED_REVISION, EXPECTED_IMAGE.replace("@sha256:", ":latest-")),
        ]
        for revision, image in cases:
            with (
                self.subTest(revision=revision, image=image),
                self.assertRaises(VerificationError),
            ):
                verify_staged_revision(
                    revision_evidence(),
                    service_evidence(),
                    expected_revision=revision,
                    expected_image=image,
                )

    def test_loader_rejects_duplicate_nonfinite_and_oversized_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "duplicate.json": '{"status":{},"status":{}}',
                "nonfinite.json": '{"value":NaN}',
                "oversized.json": "x" * (MAX_EVIDENCE_BYTES + 1),
            }
            for name, content in cases.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(VerificationError):
                    load_evidence(path)

    def test_loader_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "evidence.json"
            link.symlink_to(target)

            with self.assertRaises(VerificationError):
                load_evidence(link)

    def test_cli_has_distinct_accept_and_reject_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision_path = root / "revision.json"
            service_path = root / "service.json"
            revision_path.write_text(json.dumps(revision_evidence()), encoding="utf-8")
            service_path.write_text(json.dumps(service_evidence()), encoding="utf-8")
            command = [
                sys.executable,
                "scripts/verify_staged_revision.py",
                "--revision",
                str(revision_path),
                "--service",
                str(service_path),
                "--expected-revision",
                EXPECTED_REVISION,
                "--expected-image",
                EXPECTED_IMAGE,
            ]

            accepted = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            rejected_service = service_evidence()
            rejected_service["status"]["traffic"] = [
                {"revisionName": EXPECTED_REVISION, "percent": 1}
            ]
            service_path.write_text(json.dumps(rejected_service), encoding="utf-8")
            rejected = subprocess.run(
                command, capture_output=True, text=True, check=False
            )

            self.assertEqual(accepted.returncode, 0)
            self.assertTrue(json.loads(accepted.stdout)["accepted"])
            self.assertEqual(rejected.returncode, 3)
            self.assertFalse(json.loads(rejected.stderr)["accepted"])


if __name__ == "__main__":
    unittest.main()
