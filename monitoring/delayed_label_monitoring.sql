-- BigQuery monitoring examples for a probability model whose labels mature later.
--
-- Assumed tables:
--   `PROJECT.risk.predictions`
--     application_id STRING
--     decision_time TIMESTAMP
--     model_version STRING
--     country STRING
--     default_probability FLOAT64
--
--   `PROJECT.risk.outcomes`
--     application_id STRING
--     observed_at TIMESTAMP
--     default_label INT64
--
-- Replace PROJECT with your GCP project before execution.

DECLARE as_of TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
DECLARE outcome_horizon_days INT64 DEFAULT 90;
DECLARE reporting_lag_days INT64 DEFAULT 7;

-- 1) Mature labels only. Recent unlabeled accounts must not be interpreted as negatives.
CREATE TEMP TABLE mature_scored AS
SELECT
  p.application_id,
  p.decision_time,
  p.model_version,
  p.country,
  p.default_probability,
  o.default_label,
  o.observed_at,
  o.default_label IS NULL AS missing_mature_label
FROM `PROJECT.risk.predictions` AS p
LEFT JOIN `PROJECT.risk.outcomes` AS o
  USING (application_id)
WHERE p.decision_time <= TIMESTAMP_SUB(
  as_of,
  INTERVAL outcome_horizon_days + reporting_lag_days DAY
);

-- 2) Data-quality monitor: labels that should be mature but are still missing.
SELECT
  model_version,
  COUNT(*) AS mature_prediction_count,
  COUNTIF(missing_mature_label) AS missing_mature_labels,
  SAFE_DIVIDE(COUNTIF(missing_mature_label), COUNT(*)) AS missing_mature_label_rate
FROM mature_scored
GROUP BY model_version
ORDER BY model_version;

-- 3) Monthly calibration / operating monitor.
-- Brier score is mean squared error of the probability forecast for a binary outcome.
SELECT
  DATE_TRUNC(DATE(decision_time), MONTH) AS cohort_month,
  model_version,
  country,
  COUNT(*) AS rows,
  AVG(default_label) AS observed_event_rate,
  AVG(default_probability) AS mean_predicted_probability,
  AVG(default_label) - AVG(default_probability) AS calibration_gap,
  AVG(POW(default_probability - default_label, 2)) AS brier_score,
  APPROX_QUANTILES(default_probability, 100)[OFFSET(50)] AS median_score,
  APPROX_QUANTILES(default_probability, 100)[OFFSET(95)] AS p95_score
FROM mature_scored
WHERE NOT missing_mature_label
GROUP BY cohort_month, model_version, country
HAVING COUNT(*) >= 100
ORDER BY cohort_month, model_version, country;

-- 4) Decile calibration / lift-style table.
WITH labeled AS (
  SELECT *
  FROM mature_scored
  WHERE NOT missing_mature_label
),
scored AS (
  SELECT
    *,
    NTILE(10) OVER (
      PARTITION BY model_version
      ORDER BY default_probability DESC
    ) AS risk_decile
  FROM labeled
),
base AS (
  SELECT
    model_version,
    AVG(default_label) AS portfolio_event_rate
  FROM labeled
  GROUP BY model_version
)
SELECT
  s.model_version,
  s.risk_decile,
  COUNT(*) AS rows,
  AVG(s.default_probability) AS mean_score,
  AVG(s.default_label) AS observed_event_rate,
  SAFE_DIVIDE(AVG(s.default_label), b.portfolio_event_rate) AS lift_vs_portfolio
FROM scored AS s
JOIN base AS b
  USING (model_version)
GROUP BY s.model_version, s.risk_decile, b.portfolio_event_rate
ORDER BY s.model_version, s.risk_decile;

-- 5) Decision-rate monitor, useful before labels mature.
-- This is an operational signal, NOT a substitute for outcome-based performance.
SELECT
  DATE(decision_time) AS decision_date,
  model_version,
  country,
  COUNT(*) AS rows,
  AVG(CAST(default_probability < 0.08 AS INT64)) AS candidate_auto_approve_rate,
  AVG(CAST(default_probability >= 0.65 AS INT64)) AS candidate_auto_decline_rate,
  AVG(CAST(default_probability >= 0.08 AND default_probability < 0.65 AS INT64))
    AS candidate_review_rate,
  AVG(default_probability) AS mean_score
FROM `PROJECT.risk.predictions`
WHERE decision_time >= TIMESTAMP_SUB(as_of, INTERVAL 30 DAY)
GROUP BY decision_date, model_version, country
ORDER BY decision_date DESC, model_version, country;
