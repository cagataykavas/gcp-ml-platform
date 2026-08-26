from __future__ import annotations

import argparse
from dataclasses import dataclass

from google.cloud import bigquery


@dataclass(frozen=True)
class MonitorConfig:
    project: str
    prediction_table: str
    outcome_table: str
    output_table: str
    lookback_days: int = 30


MONITORING_QUERY = """
WITH scored AS (
  SELECT
    application_id,
    TIMESTAMP(score_time) AS score_time,
    CAST(default_probability AS FLOAT64) AS score,
    decision_route,
    model_version
  FROM `{prediction_table}`
  WHERE DATE(score_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
),

labeled AS (
  SELECT
    application_id,
    CAST(default_label AS INT64) AS default_label,
    TIMESTAMP(label_time) AS label_time
  FROM `{outcome_table}`
  WHERE default_label IS NOT NULL
),

joined AS (
  SELECT
    s.*,
    l.default_label,
    l.label_time,
    NTILE(10) OVER (ORDER BY s.score) AS score_decile
  FROM scored AS s
  LEFT JOIN labeled AS l USING (application_id)
),

aggregate AS (
  SELECT
    DATE(score_time) AS score_date,
    model_version,
    COUNT(*) AS scored_rows,
    AVG(score) AS mean_score,
    STDDEV_POP(score) AS std_score,
    AVG(CASE WHEN decision_route = 'human_review' THEN 1 ELSE 0 END) AS review_rate,
    AVG(CASE WHEN decision_route = 'auto_approve' THEN 1 ELSE 0 END) AS approval_rate,
    AVG(CASE WHEN decision_route = 'auto_decline' THEN 1 ELSE 0 END) AS decline_rate,
    COUNTIF(default_label IS NOT NULL) AS labeled_rows,
    AVG(CAST(default_label AS FLOAT64)) AS observed_default_rate,
    AVG(IF(default_label IS NULL, NULL, POW(score - default_label, 2))) AS brier_score
  FROM joined
  GROUP BY score_date, model_version
)

SELECT *
FROM aggregate
ORDER BY score_date DESC, model_version
"""


CALIBRATION_QUERY = """
WITH joined AS (
  SELECT
    p.model_version,
    CAST(p.default_probability AS FLOAT64) AS score,
    CAST(o.default_label AS INT64) AS y,
    NTILE(10) OVER (
      PARTITION BY p.model_version
      ORDER BY CAST(p.default_probability AS FLOAT64)
    ) AS score_bin
  FROM `{prediction_table}` AS p
  JOIN `{outcome_table}` AS o USING (application_id)
  WHERE DATE(p.score_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
    AND o.default_label IS NOT NULL
)

SELECT
  model_version,
  score_bin,
  COUNT(*) AS rows,
  AVG(score) AS mean_predicted_probability,
  AVG(y) AS observed_default_rate,
  ABS(AVG(score) - AVG(y)) AS calibration_gap
FROM joined
GROUP BY model_version, score_bin
ORDER BY model_version, score_bin
"""


def run_query(client: bigquery.Client, sql: str, config: MonitorConfig):
    rendered = sql.format(
        prediction_table=config.prediction_table,
        outcome_table=config.outcome_table,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("lookback_days", "INT64", config.lookback_days),
        ]
    )
    return client.query(rendered, job_config=job_config).result()


def write_daily_monitoring(client: bigquery.Client, config: MonitorConfig) -> int:
    query = MONITORING_QUERY.format(
        prediction_table=config.prediction_table,
        outcome_table=config.outcome_table,
    )
    job_config = bigquery.QueryJobConfig(
        destination=config.output_table,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        query_parameters=[
            bigquery.ScalarQueryParameter("lookback_days", "INT64", config.lookback_days),
        ],
    )
    job = client.query(query, job_config=job_config)
    result = job.result()
    print(f"monitoring table refreshed: {config.output_table}")
    return int(result.total_rows or 0)


def print_calibration(client: bigquery.Client, config: MonitorConfig) -> None:
    rows = run_query(client, CALIBRATION_QUERY, config)
    print("model_version\tbin\trows\tpredicted\tobserved\tgap")
    for row in rows:
        print(
            f"{row.model_version}\t{row.score_bin}\t{row.rows}\t"
            f"{row.mean_predicted_probability:.4f}\t{row.observed_default_rate:.4f}\t"
            f"{row.calibration_gap:.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="BigQuery monitoring example for banking-risk predictions")
    parser.add_argument("--project", required=True)
    parser.add_argument("--prediction-table", required=True)
    parser.add_argument("--outcome-table", required=True)
    parser.add_argument("--output-table", required=True)
    parser.add_argument("--lookback-days", type=int, default=30)
    args = parser.parse_args()

    config = MonitorConfig(
        project=args.project,
        prediction_table=args.prediction_table,
        outcome_table=args.outcome_table,
        output_table=args.output_table,
        lookback_days=args.lookback_days,
    )
    client = bigquery.Client(project=config.project)
    write_daily_monitoring(client, config)
    print_calibration(client, config)


if __name__ == "__main__":
    main()
