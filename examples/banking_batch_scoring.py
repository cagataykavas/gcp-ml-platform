from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass

from google.cloud import aiplatform, bigquery

from platform_contracts.batch_scoring import (
    FEATURE_COLUMNS,
    build_bigquery_insert_batch,
    decision_route,
    parse_vertex_probabilities,
    prepare_instances,
)


@dataclass(frozen=True)
class Config:
    project: str
    location: str
    source_table: str
    destination_table: str
    endpoint_name: str
    model_revision: str


def read_feature_rows(
    client: bigquery.Client, table: str, limit: int = 500
) -> list[dict]:
    query = f"""
    SELECT
      application_id,
      {", ".join(FEATURE_COLUMNS)}
    FROM `{table}`
    WHERE application_id IS NOT NULL
    ORDER BY application_id
    LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
    )
    return [
        dict(row.items()) for row in client.query(query, job_config=job_config).result()
    ]


def to_instances(rows: Iterable[dict]) -> tuple[list[str], list[dict]]:
    batch = prepare_instances(rows)
    return list(batch.application_ids), list(batch.instances)


def score_endpoint(config: Config, instances: list[dict]) -> list[float]:
    aiplatform.init(project=config.project, location=config.location)
    endpoint = aiplatform.Endpoint(config.endpoint_name)
    response = endpoint.predict(instances=instances)

    return list(
        parse_vertex_probabilities(response.predictions, expected_count=len(instances))
    )


def write_scores(
    client: bigquery.Client,
    destination_table: str,
    application_ids: list[str],
    probabilities: list[float],
    *,
    scoring_run_id: str,
    model_revision: str,
) -> None:
    batch = build_bigquery_insert_batch(
        application_ids,
        probabilities,
        scoring_run_id=scoring_run_id,
        model_revision=model_revision,
    )
    errors = client.insert_rows_json(
        destination_table,
        list(batch.rows),
        row_ids=list(batch.row_ids),
    )
    if errors:
        raise RuntimeError(f"BigQuery insert failed for {len(errors)} row(s)")


def run(config: Config, *, scoring_run_id: str, limit: int = 500) -> int:
    bq = bigquery.Client(project=config.project)
    source_rows = read_feature_rows(bq, config.source_table, limit=limit)
    if not source_rows:
        print("No rows found for scoring")
        return 0

    application_ids, instances = to_instances(source_rows)
    probabilities = score_endpoint(config, instances)
    write_scores(
        bq,
        config.destination_table,
        application_ids,
        probabilities,
        scoring_run_id=scoring_run_id,
        model_revision=config.model_revision,
    )

    review_count = sum(
        decision_route(probability) == "human_review" for probability in probabilities
    )
    print(f"scored={len(probabilities)} human_review={review_count}")
    return len(probabilities)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BigQuery -> Vertex AI banking scoring example"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="europe-west4")
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--destination-table", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--run-id", required=True)
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
        model_revision=args.model_revision,
    )
    run(config, scoring_run_id=args.run_id, limit=args.limit)


if __name__ == "__main__":
    main()
