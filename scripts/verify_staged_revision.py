from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path

MAX_EVIDENCE_BYTES = 1024 * 1024
REVISION_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
IMAGE_RE = re.compile(
    r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]{4,28}/"
    r"[a-z0-9][a-z0-9._-]{0,127}/[a-z0-9][a-z0-9._-]{0,127}"
    r"@sha256:[0-9a-f]{64}$"
)


class VerificationError(ValueError):
    """Raised when Cloud Run evidence does not prove a safe staged revision."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("evidence contains duplicate JSON fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise VerificationError(f"evidence contains non-finite constant {value}")


def load_evidence(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise VerificationError("evidence cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VerificationError("evidence must be a regular non-symbolic file")
    if not 1 <= metadata.st_size <= MAX_EVIDENCE_BYTES:
        raise VerificationError("evidence size is outside the accepted range")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_pairs,
                parse_constant=_reject_constant,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VerificationError("evidence is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise VerificationError("evidence root must be an object")
    return value


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise VerificationError(f"{path} must be an object")
    return value


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise VerificationError(f"{path} must be an array")
    return value


def verify_staged_revision(
    revision: dict[str, object],
    service: dict[str, object],
    *,
    expected_revision: str,
    expected_image: str,
) -> dict[str, object]:
    if not REVISION_RE.fullmatch(expected_revision):
        raise VerificationError("expected revision name is invalid")
    if not IMAGE_RE.fullmatch(expected_image):
        raise VerificationError(
            "expected image must be a digest-pinned Artifact Registry URI"
        )

    metadata = _object(revision.get("metadata"), "revision.metadata")
    if metadata.get("name") != expected_revision:
        raise VerificationError("revision identity does not match release intent")
    status = _object(revision.get("status"), "revision.status")
    conditions = _list(status.get("conditions"), "revision.status.conditions")
    ready_conditions = [
        condition
        for condition in conditions
        if isinstance(condition, dict) and condition.get("type") == "Ready"
    ]
    if len(ready_conditions) != 1 or ready_conditions[0].get("status") != "True":
        raise VerificationError("revision is not ready")

    spec = _object(revision.get("spec"), "revision.spec")
    containers = _list(spec.get("containers"), "revision.spec.containers")
    if len(containers) != 1:
        raise VerificationError(
            "revision must contain exactly one application container"
        )
    container = _object(containers[0], "revision.spec.containers[0]")
    if container.get("image") != expected_image:
        raise VerificationError("revision image does not match release intent")

    service_status = _object(service.get("status"), "service.status")
    if service_status.get("latestCreatedRevisionName") != expected_revision:
        raise VerificationError("a different revision was created during verification")
    if service_status.get("latestReadyRevisionName") != expected_revision:
        raise VerificationError("staged revision is not the latest ready revision")
    traffic = _list(service_status.get("traffic", []), "service.status.traffic")
    for index, target_value in enumerate(traffic):
        target = _object(target_value, f"service.status.traffic[{index}]")
        if target.get("revisionName") == expected_revision or target.get(
            "latestRevision"
        ):
            raise VerificationError(
                "staged revision is reachable through service traffic"
            )
        percent = target.get("percent", 0)
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise VerificationError("traffic percent must be numeric")
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise VerificationError("traffic percent is outside [0, 100]")

    evidence = {
        "schema_version": "cloud-run-stage-verification/v1",
        "accepted": True,
        "revision": expected_revision,
        "image_digest": expected_image.rsplit("@", 1)[1],
        "traffic_percent": 0,
    }
    canonical = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    evidence["verification_id"] = hashlib.sha256(canonical.encode()).hexdigest()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify one exact zero-traffic Cloud Run staging revision."
    )
    parser.add_argument("--revision", type=Path, required=True)
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-image", required=True)
    args = parser.parse_args()
    try:
        result = verify_staged_revision(
            load_evidence(args.revision),
            load_evidence(args.service),
            expected_revision=args.expected_revision,
            expected_image=args.expected_image,
        )
    except VerificationError as error:
        print(json.dumps({"accepted": False, "error": str(error)}), file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return os.EX_OK


if __name__ == "__main__":
    raise SystemExit(main())
