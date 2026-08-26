from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass

from google.cloud import aiplatform, bigquery


@dataclass(frozen=True)
class Config:
    project: str
    location: str
    source_table: str
    destination_table: str
    endpoint_name: str


FEATURE_COLUMNS = [
    "age",
    "income",
    "debt",
    "utilization",
    "late_payments",
    "account_age_months",
]


def read_feature_rows(client: bigquery.Client, table: str, limit: int = 500) -> list[dict]:
    query = f"""
    SELECT
      application_id,
      {', '.join(FEATURE_COLUMNS)}
    FROM `{table}`
    WHERE application_id IS NOT NULL
    ORDER BY application_id
    LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
    )
    return [dict(row.items()) for row in client.query(query, job_config=job_config).result()]


def to_instances(rows: Iterable[dict]) -> tuple[list[str], list[dict]]:
    ids: list[str] = []
    instances: list[dict] = []
    for row in rows:
        ids.append(str(row["application_id"]))
        instances.append({feature: float(row[feature]) for feature in FEATURE_COLUMNS})
    return ids, instances


def score_endpoint(config: Config, instances: list[dict]) -> list[float]:
    aiplatform.init(project=config.project, location=config.location)
    endpoint = aiplatform.Endpoint(config.endpoint_name)
    response = endpoint.predict(instances=instances)

    probabilities: list[float] = []
    for prediction in response.predictions:
        if isinstance(prediction, dict):
            value = prediction.get("default_probability", prediction.get("score"))
        elif isinstance(prediction, (list, tuple)):
            value = prediction[-1]
        else:
            value = prediction
        probabilities.append(float(value))
    return probabilities


def decision_route(probability: float) -> str:
    if probability < 0.08:
        return "auto_approve"
    if probability >= 0.65:
        return "auto_decline"
    return "human_review"


def write_scores(
    client: bigquery.Client,
    destination_table: str,
    application_ids: list[str],
    probabilities: list[float],
) -> None:
    rows = [
        {
            "application_id": application_id,
            "default_probability": probability,
            "decision_route": decision_route(probability),
        }
        for application_id, probability in zip(application_ids, probabilities, strict=True)
    ]
    errors = client.insert_rows_json(destination_table, rows)
    if errors:
        raise RuntimeError(f"BigQuery insert failed: {errors}")


def run(config: Config, limit: int = 500) -> int:
    bq = bigquery.Client(project=config.project)
    source_rows = read_feature_rows(bq, config.source_table, limit=limit)
    if not source_rows:
        print("No rows found for scoring")
        return 0

    application_ids, instances = to_instances(source_rows)
    probabilities = score_endpoint(config, instances)
    write_scores(bq, config.destination_table, application_ids, probabilities)

    review_count = sum(decision_route(p) == "human_review" for p in probabilities)
    print(f"scored={len(probabilities)} human_review={review_count}")
    return len(probabilities)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BigQuery -> Vertex AI banking scoring example")
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="europe-west4")
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--destination-table", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = Config(
        project=args.project,
        location=args.location,
        source_table=args.source_table,
        destination_table=args.destination_table,
        endpoint_name=args.endpoint,
    )
    run(config, limit=args.limit)


if __name__ == "__main__":
    main()
