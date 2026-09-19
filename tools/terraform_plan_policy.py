"""Fail-closed policy gate for Terraform plan JSON."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PUBLIC_MEMBERS = {"allUsers", "allAuthenticatedUsers"}


@dataclass(frozen=True)
class Finding:
    address: str
    rule: str
    message: str


@dataclass(frozen=True)
class PlanReport:
    resources_checked: int
    passed: bool
    findings: tuple[Finding, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _public_members(after: dict[str, Any]) -> set[str]:
    values: list[object] = []
    if "member" in after:
        values.append(after["member"])
    members = after.get("members", [])
    if isinstance(members, list):
        values.extend(members)
    return {value for value in values if isinstance(value, str)} & PUBLIC_MEMBERS


def _container_images(after: dict[str, Any]) -> list[str]:
    images: list[str] = []
    templates = after.get("template", [])
    if isinstance(templates, dict):
        templates = [templates]
    if not isinstance(templates, list):
        return images
    for template in templates:
        if not isinstance(template, dict):
            continue
        containers = template.get("containers", [])
        if isinstance(containers, dict):
            containers = [containers]
        if not isinstance(containers, list):
            continue
        images.extend(
            container["image"]
            for container in containers
            if isinstance(container, dict) and isinstance(container.get("image"), str)
        )
    return images


def evaluate_plan(plan: dict[str, Any]) -> PlanReport:
    """Evaluate a decoded Terraform plan against rollout safety rules."""

    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        raise TypeError("plan must contain a resource_changes list")

    findings: list[Finding] = []
    checked = 0
    for resource in changes:
        if not isinstance(resource, dict):
            raise TypeError("resource_changes entries must be objects")
        address = resource.get("address")
        resource_type = resource.get("type")
        change = resource.get("change")
        if not isinstance(address, str) or not isinstance(resource_type, str):
            raise TypeError("each resource change requires string address and type")
        if not isinstance(change, dict) or not isinstance(change.get("actions"), list):
            raise TypeError(f"{address} requires a change.actions list")

        actions = change["actions"]
        if any(not isinstance(action, str) for action in actions):
            raise ValueError(f"{address} contains an invalid action")
        if actions == ["no-op"]:
            continue
        checked += 1

        if "delete" in actions:
            findings.append(
                Finding(
                    address,
                    "no_destructive_changes",
                    f"destructive action sequence is not allowed: {actions}",
                )
            )

        after = change.get("after")
        if after is None:
            continue
        if not isinstance(after, dict):
            raise TypeError(f"{address} change.after must be an object or null")

        if "_iam_" in resource_type:
            public = sorted(_public_members(after))
            if public:
                findings.append(
                    Finding(
                        address,
                        "no_public_iam",
                        f"public IAM principals are forbidden: {', '.join(public)}",
                    )
                )

        if resource_type == "google_storage_bucket":
            if after.get("public_access_prevention") != "enforced":
                findings.append(
                    Finding(
                        address,
                        "storage_public_access_prevention",
                        "Cloud Storage public access prevention must be enforced",
                    )
                )
            if after.get("uniform_bucket_level_access") is not True:
                findings.append(
                    Finding(
                        address,
                        "storage_uniform_access",
                        "Cloud Storage uniform bucket-level access must be enabled",
                    )
                )
            if after.get("force_destroy") is True:
                findings.append(
                    Finding(
                        address,
                        "storage_no_force_destroy",
                        "versioned artifact buckets cannot enable force_destroy",
                    )
                )

        if resource_type == "google_bigquery_dataset" and after.get(
            "delete_contents_on_destroy"
        ) is True:
            findings.append(
                Finding(
                    address,
                    "bigquery_no_delete_contents",
                    "monitoring datasets cannot delete contents on destroy",
                )
            )

        if resource_type == "google_project_service" and after.get("disable_on_destroy") is True:
            findings.append(
                Finding(
                    address,
                    "keep_required_apis_enabled",
                    "required APIs cannot be disabled during resource destruction",
                )
            )

        if resource_type == "google_cloud_run_v2_service":
            for image in _container_images(after):
                if image.endswith(":latest") or "@sha256:" not in image:
                    findings.append(
                        Finding(
                            address,
                            "immutable_container_image",
                            f"Cloud Run image must use a sha256 digest: {image}",
                        )
                    )

    return PlanReport(
        resources_checked=checked,
        passed=not findings,
        findings=tuple(findings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Path produced by terraform show -json")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    try:
        payload = json.loads(args.plan.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("plan root must be an object")
        report = evaluate_plan(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        parser.exit(2, f"invalid Terraform plan: {error}\n")

    rendered = json.dumps(report.as_dict(), indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
