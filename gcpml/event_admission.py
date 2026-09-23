"""Fail-closed Pub/Sub transaction admission for event-time pipelines."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


class AdmissionError(ValueError):
    """Expected contract rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class EventTimePolicy:
    """Bound the evidence accepted into event-time feature windows."""

    max_payload_bytes: int = 256 * 1024
    max_lateness_seconds: int = 6 * 60 * 60
    max_future_skew_seconds: int = 2 * 60
    max_identifier_length: int = 128

    def __post_init__(self) -> None:
        if self.max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        if self.max_lateness_seconds < 0:
            raise ValueError("max_lateness_seconds must be non-negative")
        if self.max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must be non-negative")
        if self.max_identifier_length <= 0:
            raise ValueError("max_identifier_length must be positive")


@dataclass(frozen=True)
class DeadLetterRecord:
    """Bounded DLQ evidence that is safe to serialize to BigQuery."""

    payload: str
    payload_sha256: str
    payload_truncated: bool
    reason_code: str
    retryable: bool
    error: str
    observed_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdmissionError("timezone_required", f"{field} must include a timezone")
    return value.astimezone(UTC)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdmissionError("duplicate_json_field", f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _decode_payload(message: bytes, policy: EventTimePolicy) -> dict[str, Any]:
    if not isinstance(message, bytes):
        raise AdmissionError("invalid_payload_type", "payload must be bytes")
    if len(message) > policy.max_payload_bytes:
        raise AdmissionError(
            "payload_too_large",
            f"payload exceeds {policy.max_payload_bytes} bytes",
        )
    try:
        decoded = message.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdmissionError("invalid_utf8", "payload is not valid UTF-8") from exc
    try:
        payload = json.loads(decoded, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AdmissionError("invalid_json", "payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AdmissionError("invalid_json_shape", "payload must be a JSON object")
    return payload


def _identifier(payload: dict[str, Any], field: str, policy: EventTimePolicy) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise AdmissionError("invalid_identifier", f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise AdmissionError("invalid_identifier", f"{field} must not be blank")
    if len(normalized) > policy.max_identifier_length:
        raise AdmissionError(
            "identifier_too_long",
            f"{field} exceeds {policy.max_identifier_length} characters",
        )
    return normalized


def normalize_transaction(
    message: bytes,
    *,
    observed_at: datetime,
    policy: EventTimePolicy | None = None,
) -> dict[str, object]:
    """Validate and normalize a transaction or raise :class:`AdmissionError`.

    ``observed_at`` is the trusted Pub/Sub source timestamp. The function is
    dependency-free so the exact contract can be tested without a Beam runner.
    """

    policy = policy or EventTimePolicy()
    observed_at = _utc(observed_at, field="observed_at")
    payload = _decode_payload(message, policy)

    required = {
        "transaction_id",
        "customer_id",
        "event_time",
        "amount",
        "country",
        "merchant_id",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise AdmissionError("missing_required_fields", f"missing fields: {missing}")

    transaction_id = _identifier(payload, "transaction_id", policy)
    customer_id = _identifier(payload, "customer_id", policy)
    merchant_id = _identifier(payload, "merchant_id", policy)

    amount_value = payload["amount"]
    if isinstance(amount_value, bool):
        raise AdmissionError("invalid_amount", "amount must be numeric, not boolean")
    try:
        amount = float(amount_value)
    except (TypeError, ValueError) as exc:
        raise AdmissionError("invalid_amount", "amount must be numeric") from exc
    if not math.isfinite(amount):
        raise AdmissionError("non_finite_amount", "amount must be finite")
    if amount < 0:
        raise AdmissionError("negative_amount", "amount must be non-negative")

    country_value = payload["country"]
    if not isinstance(country_value, str):
        raise AdmissionError("invalid_country", "country must be a two-letter string")
    country = country_value.strip().upper()
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        raise AdmissionError(
            "invalid_country", "country must be a two-letter ASCII code"
        )

    try:
        event_time = datetime.fromisoformat(str(payload["event_time"]))
    except ValueError as exc:
        raise AdmissionError(
            "invalid_event_time", "event_time must be ISO-8601"
        ) from exc
    event_time = _utc(event_time, field="event_time")

    if event_time > observed_at + timedelta(seconds=policy.max_future_skew_seconds):
        raise AdmissionError(
            "event_time_in_future",
            "event_time exceeds the configured future-skew budget",
            retryable=True,
        )
    if event_time < observed_at - timedelta(seconds=policy.max_lateness_seconds):
        raise AdmissionError(
            "event_too_late",
            "event_time exceeds the configured lateness budget",
        )

    return {
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "merchant_id": merchant_id,
        "event_time": event_time.isoformat(),
        "amount": amount,
        "country": country,
        "is_cross_border": int(country != "TR"),
        "event_timestamp": event_time.timestamp(),
    }


def build_dead_letter(
    message: bytes,
    error: AdmissionError,
    *,
    observed_at: datetime,
    max_excerpt_bytes: int = 4096,
) -> DeadLetterRecord:
    """Create bounded, content-addressed evidence for a rejected payload."""

    if max_excerpt_bytes <= 0:
        raise ValueError("max_excerpt_bytes must be positive")
    observed_at = _utc(observed_at, field="observed_at")
    raw = message if isinstance(message, bytes) else repr(message).encode("utf-8")
    excerpt = raw[:max_excerpt_bytes]
    return DeadLetterRecord(
        payload=excerpt.decode("utf-8", errors="replace"),
        payload_sha256=hashlib.sha256(raw).hexdigest(),
        payload_truncated=len(raw) > max_excerpt_bytes,
        reason_code=error.code,
        retryable=error.retryable,
        error=str(error),
        observed_at=observed_at.isoformat(),
    )
