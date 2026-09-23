from __future__ import annotations

import json
import math
import unittest
from datetime import UTC, datetime

from gcpml.event_admission import (
    AdmissionError,
    EventTimePolicy,
    build_dead_letter,
    normalize_transaction,
)

OBSERVED_AT = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)


def payload(**updates: object) -> bytes:
    values: dict[str, object] = {
        "transaction_id": "tx-1",
        "customer_id": "customer-1",
        "event_time": "2026-09-23T07:59:00Z",
        "amount": 125.5,
        "country": "de",
        "merchant_id": "merchant-1",
    }
    values.update(updates)
    return json.dumps(values, separators=(",", ":")).encode()


class EventAdmissionTests(unittest.TestCase):
    def assert_rejected(
        self,
        message: bytes,
        code: str,
        *,
        policy: EventTimePolicy | None = None,
    ) -> AdmissionError:
        with self.assertRaises(AdmissionError) as caught:
            normalize_transaction(message, observed_at=OBSERVED_AT, policy=policy)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_valid_payload_is_normalized_for_event_time_windowing(self) -> None:
        result = normalize_transaction(payload(), observed_at=OBSERVED_AT)

        self.assertEqual(result["country"], "DE")
        self.assertEqual(result["is_cross_border"], 1)
        self.assertEqual(result["event_time"], "2026-09-23T07:59:00+00:00")
        self.assertEqual(result["event_timestamp"], 1790150340.0)

    def test_duplicate_json_field_is_rejected(self) -> None:
        message = (
            b'{"transaction_id":"tx-1","transaction_id":"tx-2",'
            b'"customer_id":"c","event_time":"2026-09-23T07:59:00Z",'
            b'"amount":1,"country":"TR","merchant_id":"m"}'
        )
        self.assert_rejected(message, "duplicate_json_field")

    def test_missing_required_field_is_rejected(self) -> None:
        values = json.loads(payload())
        del values["customer_id"]
        self.assert_rejected(json.dumps(values).encode(), "missing_required_fields")

    def test_blank_identifier_is_rejected(self) -> None:
        self.assert_rejected(payload(customer_id="  "), "invalid_identifier")

    def test_boolean_amount_is_not_coerced_to_one(self) -> None:
        self.assert_rejected(payload(amount=True), "invalid_amount")

    def test_non_finite_amount_is_rejected(self) -> None:
        self.assert_rejected(payload(amount=math.inf), "non_finite_amount")

    def test_negative_amount_is_rejected(self) -> None:
        self.assert_rejected(payload(amount=-0.01), "negative_amount")

    def test_invalid_country_is_rejected(self) -> None:
        self.assert_rejected(payload(country="TUR"), "invalid_country")

    def test_timezone_naive_event_time_is_rejected(self) -> None:
        self.assert_rejected(
            payload(event_time="2026-09-23T07:59:00"), "timezone_required"
        )

    def test_event_beyond_lateness_budget_is_rejected(self) -> None:
        policy = EventTimePolicy(max_lateness_seconds=60)
        self.assert_rejected(
            payload(event_time="2026-09-23T07:58:59Z"),
            "event_too_late",
            policy=policy,
        )

    def test_future_skew_rejection_is_marked_retryable(self) -> None:
        error = self.assert_rejected(
            payload(event_time="2026-09-23T08:02:01Z"),
            "event_time_in_future",
        )
        self.assertTrue(error.retryable)

    def test_payload_size_is_bounded_before_json_parsing(self) -> None:
        policy = EventTimePolicy(max_payload_bytes=10)
        self.assert_rejected(payload(), "payload_too_large", policy=policy)

    def test_dead_letter_is_bounded_and_content_addressed(self) -> None:
        message = b"not-json-and-long"
        error = AdmissionError("invalid_json", "payload is not valid JSON")

        record = build_dead_letter(
            message, error, observed_at=OBSERVED_AT, max_excerpt_bytes=8
        )

        self.assertEqual(record.payload, "not-json")
        self.assertEqual(
            record.payload_sha256,
            "a4afb5547fd9f67c0c70e169e6aa291c41032e5aae73da9232ae02fc5d53fe2c",
        )
        self.assertTrue(record.payload_truncated)
        self.assertEqual(record.reason_code, "invalid_json")
        self.assertFalse(record.retryable)

    def test_invalid_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_payload_bytes must be positive"):
            EventTimePolicy(max_payload_bytes=0)


if __name__ == "__main__":
    unittest.main()
