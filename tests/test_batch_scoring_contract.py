from __future__ import annotations

import math
import unittest

from platform_contracts.batch_scoring import (
    FEATURE_COLUMNS,
    build_bigquery_insert_batch,
    parse_vertex_probabilities,
    prepare_instances,
)


def feature_row(
    application_id: object = "app-1", **changes: object
) -> dict[str, object]:
    row: dict[str, object] = {
        "application_id": application_id,
        "age": 35,
        "income": 60_000,
        "debt": 12_000,
        "utilization": 0.4,
        "late_payments": 1,
        "account_age_months": 48,
    }
    row.update(changes)
    return row


class PrepareInstancesTests(unittest.TestCase):
    def test_prepares_exact_feature_contract_without_mutating_rows(self) -> None:
        row = feature_row(extra="not sent")
        original = row.copy()

        batch = prepare_instances([row])

        self.assertEqual(batch.application_ids, ("app-1",))
        self.assertEqual(tuple(batch.instances[0]), FEATURE_COLUMNS)
        self.assertEqual(row, original)

    def test_rejects_duplicate_ids_missing_features_and_nonfinite_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            prepare_instances([feature_row(), feature_row()])
        missing = feature_row()
        del missing["income"]
        with self.assertRaisesRegex(ValueError, "missing feature income"):
            prepare_instances([missing])
        with self.assertRaisesRegex(ValueError, "finite"):
            prepare_instances([feature_row(income=math.nan)])

    def test_rejects_boolean_features_and_oversized_batches(self) -> None:
        with self.assertRaisesRegex(TypeError, "JSON number"):
            prepare_instances([feature_row(age=True)])
        with self.assertRaisesRegex(ValueError, "exceeds"):
            prepare_instances(
                [feature_row(f"app-{index}") for index in range(3)], max_rows=2
            )


class VertexResponseTests(unittest.TestCase):
    def test_accepts_supported_vertex_response_shapes(self) -> None:
        parsed = parse_vertex_probabilities(
            [{"default_probability": 0.1}, {"score": 0.2}, [0.7, 0.3], 0.4],
            expected_count=4,
        )
        self.assertEqual(parsed, (0.1, 0.2, 0.3, 0.4))

    def test_rejects_cardinality_shape_and_probability_failures(self) -> None:
        with self.assertRaisesRegex(ValueError, "count"):
            parse_vertex_probabilities([0.1], expected_count=2)
        with self.assertRaisesRegex(ValueError, "supported"):
            parse_vertex_probabilities([{"other": 0.1}], expected_count=1)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            parse_vertex_probabilities([[]], expected_count=1)
        with self.assertRaisesRegex(ValueError, "finite"):
            parse_vertex_probabilities([math.inf], expected_count=1)
        with self.assertRaisesRegex(ValueError, r"\[0, 1\]"):
            parse_vertex_probabilities([1.1], expected_count=1)


class BigQueryInsertTests(unittest.TestCase):
    def test_retry_ids_are_deterministic_and_bind_run_model_and_application(
        self,
    ) -> None:
        first = build_bigquery_insert_batch(
            ["app-1", "app-2"],
            [0.02, 0.80],
            scoring_run_id="run-42",
            model_revision="model-v7",
        )
        retry = build_bigquery_insert_batch(
            ["app-1", "app-2"],
            [0.02, 0.80],
            scoring_run_id="run-42",
            model_revision="model-v7",
        )
        other_model = build_bigquery_insert_batch(
            ["app-1", "app-2"],
            [0.02, 0.80],
            scoring_run_id="run-42",
            model_revision="model-v8",
        )

        self.assertEqual(first, retry)
        self.assertNotEqual(first.row_ids, other_model.row_ids)
        self.assertEqual(
            [row["decision_route"] for row in first.rows],
            ["auto_approve", "auto_decline"],
        )
        self.assertNotIn("run-42", str(first.rows))
        self.assertTrue(all(len(row_id) == 64 for row_id in first.row_ids))

    def test_rejects_misaligned_or_duplicate_insert_evidence(self) -> None:
        kwargs = {"scoring_run_id": "run-1", "model_revision": "model-v1"}
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            build_bigquery_insert_batch(["app-1"], [0.1, 0.2], **kwargs)
        with self.assertRaisesRegex(ValueError, "unique"):
            build_bigquery_insert_batch(["app-1", "app-1"], [0.1, 0.2], **kwargs)


if __name__ == "__main__":
    unittest.main()
