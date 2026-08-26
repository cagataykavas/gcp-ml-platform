from __future__ import annotations

import argparse
from pathlib import Path

from google.cloud import aiplatform
from kfp import compiler, dsl
from kfp.dsl import Dataset, Input, Metrics, Model, Output


@dsl.component(
    base_image="python:3.12-slim",
    packages_to_install=["google-cloud-bigquery==3.25.0", "pandas==2.2.2", "pyarrow==17.0.0"],
)
def extract_training_data(
    project: str,
    source_table: str,
    dataset: Output[Dataset],
    max_rows: int = 200000,
) -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    query = f"""
    SELECT *
    FROM `{source_table}`
    WHERE default_label IS NOT NULL
    ORDER BY event_time
    LIMIT @max_rows
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("max_rows", "INT64", max_rows)]
    )
    frame = client.query(query, job_config=config).to_dataframe()
    frame.to_parquet(dataset.path, index=False)
    dataset.metadata["rows"] = len(frame)
    dataset.metadata["source_table"] = source_table


@dsl.component(
    base_image="python:3.12-slim",
    packages_to_install=[
        "pandas==2.2.2",
        "pyarrow==17.0.0",
        "scikit-learn==1.5.1",
        "joblib==1.4.2",
    ],
)
def train_model(
    dataset: Input[Dataset],
    model: Output[Model],
    metrics: Output[Metrics],
) -> None:
    import json

    import joblib
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    frame = pd.read_parquet(dataset.path).sort_values("event_time")
    target = "default_label"
    ignore = {target, "event_time", "application_id"}
    features = [column for column in frame.columns if column not in ignore]

    split = int(len(frame) * 0.80)
    train = frame.iloc[:split]
    valid = frame.iloc[split:]

    numeric = [column for column in features if pd.api.types.is_numeric_dtype(train[column])]
    categorical = [column for column in features if column not in numeric]

    preprocess = ColumnTransformer(
        [
            ("numeric", StandardScaler(), numeric),
            ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical),
        ],
        remainder="drop",
    )
    estimator = Pipeline(
        [
            ("preprocess", preprocess),
            (
                "model",
                HistGradientBoostingClassifier(
                    learning_rate=0.05,
                    max_iter=250,
                    max_leaf_nodes=31,
                    l2_regularization=0.5,
                    random_state=42,
                ),
            ),
        ]
    )
    estimator.fit(train[features], train[target])
    probability = estimator.predict_proba(valid[features])[:, 1]

    report = {
        "roc_auc": float(roc_auc_score(valid[target], probability)),
        "average_precision": float(average_precision_score(valid[target], probability)),
        "brier_score": float(brier_score_loss(valid[target], probability)),
        "validation_rows": len(valid),
        "event_rate": float(valid[target].mean()),
    }

    Path(model.path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"estimator": estimator, "features": features, "metrics": report}, model.path)
    Path(model.path + ".metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    for name, value in report.items():
        if isinstance(value, (int, float)):
            metrics.log_metric(name, float(value))
    model.metadata.update(report)


@dsl.component(base_image="python:3.12-slim")
def quality_gate(
    metrics: Input[Metrics],
    min_roc_auc: float = 0.72,
    min_average_precision: float = 0.20,
    max_brier_score: float = 0.18,
) -> str:
    values = metrics.metadata
    failures: list[str] = []

    if float(values.get("roc_auc", 0.0)) < min_roc_auc:
        failures.append("roc_auc")
    if float(values.get("average_precision", 0.0)) < min_average_precision:
        failures.append("average_precision")
    if float(values.get("brier_score", 1.0)) > max_brier_score:
        failures.append("brier_score")

    if failures:
        raise RuntimeError(f"quality gate failed: {', '.join(failures)}")
    return "approved"


@dsl.component(
    base_image="python:3.12-slim",
    packages_to_install=["google-cloud-aiplatform==1.66.0"],
)
def register_model(
    project: str,
    location: str,
    model_display_name: str,
    artifact_uri: str,
    serving_image_uri: str,
    gate_status: str,
) -> str:
    from google.cloud import aiplatform

    if gate_status != "approved":
        raise RuntimeError("model cannot be registered before quality-gate approval")

    aiplatform.init(project=project, location=location)
    model = aiplatform.Model.upload(
        display_name=model_display_name,
        artifact_uri=artifact_uri,
        serving_container_image_uri=serving_image_uri,
        labels={"domain": "banking-risk", "stage": "candidate"},
        sync=True,
    )
    return model.resource_name


@dsl.component(
    base_image="python:3.12-slim",
    packages_to_install=["google-cloud-aiplatform==1.66.0"],
)
def deploy_candidate(
    project: str,
    location: str,
    model_resource_name: str,
    endpoint_display_name: str,
    min_replicas: int = 1,
    max_replicas: int = 3,
) -> str:
    from google.cloud import aiplatform

    aiplatform.init(project=project, location=location)
    model = aiplatform.Model(model_resource_name)
    endpoint = aiplatform.Endpoint.create(display_name=endpoint_display_name, sync=True)
    model.deploy(
        endpoint=endpoint,
        machine_type="n1-standard-2",
        min_replica_count=min_replicas,
        max_replica_count=max_replicas,
        traffic_percentage=100,
        sync=True,
    )
    return endpoint.resource_name


@dsl.pipeline(name="banking-risk-ml-platform")
def banking_pipeline(
    project: str,
    location: str,
    source_table: str,
    model_display_name: str,
    model_artifact_uri: str,
    serving_image_uri: str,
    endpoint_display_name: str,
):
    extract = extract_training_data(project=project, source_table=source_table)
    train = train_model(dataset=extract.outputs["dataset"])
    gate = quality_gate(metrics=train.outputs["metrics"])

    register = register_model(
        project=project,
        location=location,
        model_display_name=model_display_name,
        artifact_uri=model_artifact_uri,
        serving_image_uri=serving_image_uri,
        gate_status=gate.output,
    )

    deploy_candidate(
        project=project,
        location=location,
        model_resource_name=register.output,
        endpoint_display_name=endpoint_display_name,
    )


def compile_pipeline(output_path: str) -> None:
    compiler.Compiler().compile(
        pipeline_func=banking_pipeline,
        package_path=output_path,
    )


def submit_pipeline(
    *,
    project: str,
    location: str,
    template_path: str,
    pipeline_root: str,
    parameters: dict,
) -> None:
    aiplatform.init(project=project, location=location)
    job = aiplatform.PipelineJob(
        display_name="banking-risk-training",
        template_path=template_path,
        pipeline_root=pipeline_root,
        parameter_values=parameters,
        enable_caching=True,
    )
    job.submit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile", default="banking_vertex_pipeline.json")
    parser.add_argument("--project")
    parser.add_argument("--location", default="europe-west4")
    parser.add_argument("--pipeline-root")
    parser.add_argument("--source-table")
    parser.add_argument("--model-artifact-uri")
    parser.add_argument("--serving-image-uri")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()

    compile_pipeline(args.compile)
    print(f"compiled pipeline: {args.compile}")

    if args.submit:
        required = [
            args.project,
            args.pipeline_root,
            args.source_table,
            args.model_artifact_uri,
            args.serving_image_uri,
        ]
        if not all(required):
            raise SystemExit(
                "submission requires project, pipeline-root, source-table, "
                "model-artifact-uri and serving-image-uri"
            )
        submit_pipeline(
            project=args.project,
            location=args.location,
            template_path=args.compile,
            pipeline_root=args.pipeline_root,
            parameters={
                "project": args.project,
                "location": args.location,
                "source_table": args.source_table,
                "model_display_name": "banking-risk-model",
                "model_artifact_uri": args.model_artifact_uri,
                "serving_image_uri": args.serving_image_uri,
                "endpoint_display_name": "banking-risk-endpoint",
            },
        )


if __name__ == "__main__":
    main()
