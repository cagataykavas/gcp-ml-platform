from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FEATURE_COLUMNS = (
    "age",
    "income",
    "debt",
    "utilization",
    "late_payments",
    "account_age_months",
)
MAX_BATCH_ROWS = 500
MAX_IDENTIFIER_BYTES = 128


@dataclass(frozen=True)
class PreparedBatch:
    application_ids: tuple[str, ...]
    instances: tuple[dict[str, float], ...]


@dataclass(frozen=True)
class BigQueryInsertBatch:
    rows: tuple[dict[str, float | str], ...]
    row_ids: tuple[str, ...]


def _identifier(name: str, value: object) -> str:
    if isinstance(value, bool):
        raise TypeError(f"{name} must not be boolean")
    normalized = str(value)
    if not normalized or normalized != normalized.strip():
        raise ValueError(f"{name} must be non-empty and have no surrounding whitespace")
    if len(normalized.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise ValueError(f"{name} exceeds {MAX_IDENTIFIER_BYTES} UTF-8 bytes")
    if not normalized.isprintable():
        raise ValueError(f"{name} must contain only printable characters")
    return normalized


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, (bool, str, bytes)):
        raise TypeError(f"{name} must be a finite JSON number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite JSON number") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def prepare_instances(
    rows: Iterable[Mapping[str, Any]],
    *,
    max_rows: int = MAX_BATCH_ROWS,
) -> PreparedBatch:
    """Validate a bounded BigQuery feature batch before Vertex AI inference."""
    if not 1 <= max_rows <= MAX_BATCH_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_BATCH_ROWS}")
    materialized = list(rows)
    if not materialized:
        raise ValueError("at least one feature row is required")
    if len(materialized) > max_rows:
        raise ValueError("feature row count exceeds max_rows")

    application_ids: list[str] = []
    instances: list[dict[str, float]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(materialized):
        if not isinstance(row, Mapping):
            raise TypeError(f"row {index} must be a mapping")
        if "application_id" not in row:
            raise ValueError(f"row {index} is missing application_id")
        application_id = _identifier(
            f"row {index} application_id", row["application_id"]
        )
        if application_id in seen_ids:
            raise ValueError("application_id values must be unique within a batch")
        seen_ids.add(application_id)

        instance: dict[str, float] = {}
        for feature in FEATURE_COLUMNS:
            if feature not in row:
                raise ValueError(f"row {index} is missing feature {feature}")
            instance[feature] = _finite_number(
                f"row {index} feature {feature}", row[feature]
            )
        application_ids.append(application_id)
        instances.append(instance)

    return PreparedBatch(tuple(application_ids), tuple(instances))


def parse_vertex_probabilities(
    predictions: Sequence[object],
    *,
    expected_count: int,
) -> tuple[float, ...]:
    """Validate Vertex prediction shape and binary default probabilities."""
    if not 1 <= expected_count <= MAX_BATCH_ROWS:
        raise ValueError(f"expected_count must be between 1 and {MAX_BATCH_ROWS}")
    if isinstance(predictions, (str, bytes)) or not isinstance(predictions, Sequence):
        raise TypeError("predictions must be a sequence")
    if len(predictions) != expected_count:
        raise ValueError("Vertex prediction count does not match the request batch")

    probabilities: list[float] = []
    for index, prediction in enumerate(predictions):
        value: object
        if isinstance(prediction, Mapping):
            if "default_probability" in prediction:
                value = prediction["default_probability"]
            elif "score" in prediction:
                value = prediction["score"]
            else:
                raise ValueError(
                    f"prediction {index} has no supported probability field"
                )
        elif isinstance(prediction, (list, tuple)):
            if not prediction:
                raise ValueError(f"prediction {index} must not be empty")
            value = prediction[-1]
        else:
            value = prediction

        probability = _finite_number(f"prediction {index}", value)
        if not 0 <= probability <= 1:
            raise ValueError(f"prediction {index} must be in [0, 1]")
        probabilities.append(probability)
    return tuple(probabilities)


def decision_route(probability: float) -> str:
    if probability < 0.08:
        return "auto_approve"
    if probability >= 0.65:
        return "auto_decline"
    return "human_review"


def build_bigquery_insert_batch(
    application_ids: Sequence[object],
    probabilities: Sequence[object],
    *,
    scoring_run_id: object,
    model_revision: object,
) -> BigQueryInsertBatch:
    """Build privacy-reduced rows and deterministic BigQuery retry IDs.

    BigQuery streaming ``insertId`` deduplication is best-effort. The caller must
    reuse the same run and model revision on retries; exact-once requirements need
    the Storage Write API or a downstream uniqueness constraint.
    """
    run_id = _identifier("scoring_run_id", scoring_run_id)
    revision = _identifier("model_revision", model_revision)
    if not application_ids or len(application_ids) > MAX_BATCH_ROWS:
        raise ValueError(f"application_ids must contain 1 to {MAX_BATCH_ROWS} values")
    if len(application_ids) != len(probabilities):
        raise ValueError("application_ids and probabilities must have equal lengths")

    rows: list[dict[str, float | str]] = []
    row_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, (raw_id, raw_probability) in enumerate(
        zip(application_ids, probabilities, strict=True)
    ):
        application_id = _identifier(f"application_id {index}", raw_id)
        if application_id in seen_ids:
            raise ValueError("application_id values must be unique within a batch")
        seen_ids.add(application_id)
        probability = _finite_number(f"probability {index}", raw_probability)
        if not 0 <= probability <= 1:
            raise ValueError(f"probability {index} must be in [0, 1]")
        rows.append(
            {
                "application_id": application_id,
                "default_probability": probability,
                "decision_route": decision_route(probability),
            }
        )
        identity = f"batch-score-v1\0{run_id}\0{revision}\0{application_id}".encode()
        row_ids.append(hashlib.sha256(identity).hexdigest())
    return BigQueryInsertBatch(tuple(rows), tuple(row_ids))
