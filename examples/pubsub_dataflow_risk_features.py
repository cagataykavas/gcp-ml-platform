from __future__ import annotations

import argparse
from dataclasses import dataclass

import apache_beam as beam
from apache_beam.options.pipeline_options import (
    PipelineOptions,
    SetupOptions,
    StandardOptions,
)
from apache_beam.transforms.window import SlidingWindows

from gcpml.event_admission import (
    AdmissionError,
    EventTimePolicy,
    build_dead_letter,
    normalize_transaction,
)


@dataclass(frozen=True)
class Config:
    project: str
    subscription: str
    output_table: str
    dead_letter_table: str
    region: str = "europe-west1"
    runner: str = "DataflowRunner"
    max_payload_bytes: int = 256 * 1024
    max_lateness_seconds: int = 6 * 60 * 60
    max_future_skew_seconds: int = 2 * 60


class ParseTransaction(beam.DoFn):
    """Parse/validate Pub/Sub JSON while keeping invalid payloads observable."""

    INVALID = "invalid"

    def __init__(self, policy: EventTimePolicy) -> None:
        self.policy = policy

    def process(self, message: bytes, timestamp=beam.DoFn.TimestampParam):
        observed_at = timestamp.to_utc_datetime(has_tz=True)
        try:
            normalized = normalize_transaction(
                message,
                observed_at=observed_at,
                policy=self.policy,
            )
            # Beam event timestamps drive windowing. The original Pub/Sub timestamp is
            # intentionally not substituted for the business event time.
            yield beam.window.TimestampedValue(
                normalized, normalized["event_timestamp"]
            )
        except AdmissionError as exc:
            yield beam.pvalue.TaggedOutput(
                self.INVALID,
                build_dead_letter(message, exc, observed_at=observed_at).to_dict(),
            )


class CustomerWindowFeatures(beam.CombineFn):
    def create_accumulator(self):
        return {
            "count": 0,
            "amount_sum": 0.0,
            "amount_max": 0.0,
            "cross_border_count": 0,
            "merchants": set(),
        }

    def add_input(self, accumulator, element):
        accumulator["count"] += 1
        accumulator["amount_sum"] += float(element["amount"])
        accumulator["amount_max"] = max(
            accumulator["amount_max"], float(element["amount"])
        )
        accumulator["cross_border_count"] += int(element["is_cross_border"])
        accumulator["merchants"].add(str(element["merchant_id"]))
        return accumulator

    def merge_accumulators(self, accumulators):
        merged = self.create_accumulator()
        for accumulator in accumulators:
            merged["count"] += accumulator["count"]
            merged["amount_sum"] += accumulator["amount_sum"]
            merged["amount_max"] = max(merged["amount_max"], accumulator["amount_max"])
            merged["cross_border_count"] += accumulator["cross_border_count"]
            merged["merchants"].update(accumulator["merchants"])
        return merged

    def extract_output(self, accumulator):
        return {
            "txn_count_5m": accumulator["count"],
            "amount_sum_5m": accumulator["amount_sum"],
            "amount_max_5m": accumulator["amount_max"],
            "cross_border_count_5m": accumulator["cross_border_count"],
            "merchant_diversity_5m": len(accumulator["merchants"]),
        }


class AttachWindow(beam.DoFn):
    def process(self, element, window=beam.DoFn.WindowParam):
        customer_id, features = element
        yield {
            "customer_id": customer_id,
            "window_start": window.start.to_utc_datetime().isoformat(),
            "window_end": window.end.to_utc_datetime().isoformat(),
            **features,
        }


def build_pipeline(pipeline: beam.Pipeline, config: Config) -> None:
    admission_policy = EventTimePolicy(
        max_payload_bytes=config.max_payload_bytes,
        max_lateness_seconds=config.max_lateness_seconds,
        max_future_skew_seconds=config.max_future_skew_seconds,
    )
    parsed = (
        pipeline
        | "Read PubSub" >> beam.io.ReadFromPubSub(subscription=config.subscription)
        | "Parse transaction"
        >> beam.ParDo(ParseTransaction(admission_policy)).with_outputs(
            ParseTransaction.INVALID,
            main="valid",
        )
    )

    _ = (
        parsed.valid
        | "Key by customer" >> beam.Map(lambda row: (row["customer_id"], row))
        | "Five minute sliding window"
        >> beam.WindowInto(SlidingWindows(size=300, period=60))
        | "Aggregate customer features" >> beam.CombinePerKey(CustomerWindowFeatures())
        | "Attach window boundaries" >> beam.ParDo(AttachWindow())
        | "Write feature rows"
        >> beam.io.WriteToBigQuery(
            config.output_table,
            schema=(
                "customer_id:STRING,window_start:TIMESTAMP,window_end:TIMESTAMP,"
                "txn_count_5m:INTEGER,amount_sum_5m:FLOAT,amount_max_5m:FLOAT,"
                "cross_border_count_5m:INTEGER,merchant_diversity_5m:INTEGER"
            ),
            write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND,
            create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
        )
    )

    _ = parsed.invalid | "Write invalid rows" >> beam.io.WriteToBigQuery(
        config.dead_letter_table,
        schema=(
            "payload:STRING,payload_sha256:STRING,payload_truncated:BOOLEAN,"
            "reason_code:STRING,retryable:BOOLEAN,error:STRING,observed_at:TIMESTAMP"
        ),
        write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND,
        create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
    )


def run(config: Config, extra_args: list[str] | None = None) -> None:
    options = PipelineOptions(
        extra_args or [],
        runner=config.runner,
        project=config.project,
        region=config.region,
        streaming=True,
        save_main_session=True,
    )
    options.view_as(StandardOptions).streaming = True
    options.view_as(SetupOptions).save_main_session = True

    with beam.Pipeline(options=options) as pipeline:
        build_pipeline(pipeline, config)


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Pub/Sub -> Dataflow risk feature stream"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--output-table", required=True)
    parser.add_argument("--dead-letter-table", required=True)
    parser.add_argument("--region", default="europe-west1")
    parser.add_argument("--runner", default="DataflowRunner")
    parser.add_argument("--max-payload-bytes", type=int, default=256 * 1024)
    parser.add_argument("--max-lateness-seconds", type=int, default=6 * 60 * 60)
    parser.add_argument("--max-future-skew-seconds", type=int, default=2 * 60)
    return parser.parse_known_args()


def main() -> None:
    args, beam_args = parse_args()
    run(
        Config(
            project=args.project,
            subscription=args.subscription,
            output_table=args.output_table,
            dead_letter_table=args.dead_letter_table,
            region=args.region,
            runner=args.runner,
            max_payload_bytes=args.max_payload_bytes,
            max_lateness_seconds=args.max_lateness_seconds,
            max_future_skew_seconds=args.max_future_skew_seconds,
        ),
        extra_args=beam_args,
    )


if __name__ == "__main__":
    main()
