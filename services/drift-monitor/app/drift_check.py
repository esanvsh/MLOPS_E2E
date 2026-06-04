import matplotlib
matplotlib.use('Agg')  # Required — no display in Docker/WSL2

import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import boto3
import numpy as np
import pandas as pd
import structlog
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from evidently.metric_preset import DataDriftPreset, DataQualityPreset
from evidently.metrics import ColumnDriftMetric
from evidently.report import Report

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

S3_ENDPOINT_URL       = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS_KEY      = os.getenv("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY      = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
S3_BUCKET_PROCESSED   = os.getenv("S3_BUCKET_PROCESSED", "payshield-processed-data")
S3_BUCKET_DRIFT       = os.getenv("S3_BUCKET_DRIFT", "payshield-drift-reports")
KAFKA_BOOTSTRAP       = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
DRIFT_THRESHOLD_SHARE = float(os.getenv("DRIFT_THRESHOLD_SHARE", "0.30"))
MIN_CURRENT_ROWS      = int(os.getenv("MIN_CURRENT_ROWS", "100"))

KAFKA_TOPIC_DRIFT = "model.drift.detected"

REFERENCE_KEY = "features/reference.parquet"
TODAY         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
CURRENT_KEY   = f"inference/live_features_{TODAY}.parquet"

# 37 features matching training schema (ml/feature_engineering/build_features.py)
FEATURE_COLS = [
    "amount_log", "is_round_amount", "amount_bin",
    "hour_of_day", "day_of_week", "is_late_night", "is_weekend", "days_since_start",
    "user_txn_count_cumulative",
    "card4_encoded", "card6_encoded",
    "addr_mismatch",
    "email_domain_risk",
    "is_high_risk_category",
    "amount_x_hour", "amount_x_velocity", "new_device_x_late_night",
    "payment_channel_encoded", "city_tier", "upi_collect_request",
    "is_new_device", "is_new_merchant_for_user",
    "C1", "C2", "C6", "C11", "C13", "C14",
    "D1", "D4", "D10", "D15",
    "M4_encoded", "M6_encoded",
    "V_pca_1", "V_pca_2", "V_pca_3", "V_pca_4", "V_pca_5",
]

# ── Colours ───────────────────────────────────────────────────────────────────

RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# ── Logging ───────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )


def _ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)
        log.info("bucket_created", bucket=bucket)


def _read_parquet(client, bucket: str, key: str) -> pd.DataFrame:
    obj = client.get_object(Bucket=bucket, Key=key)
    return pd.read_parquet(io.BytesIO(obj["Body"].read()))


def _upload_bytes(client, bucket: str, key: str, data: bytes, content_type: str) -> None:
    _ensure_bucket(client, bucket)
    client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


# ── Synthetic current data ────────────────────────────────────────────────────

def _make_synthetic_current(reference: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Sample reference and add Gaussian noise to numeric columns for testing."""
    sample = reference[cols].sample(min(500, len(reference)), random_state=99).copy()
    numeric = sample.select_dtypes(include=[np.number]).columns
    noise = np.random.default_rng(99).normal(0, 0.3, size=(len(sample), len(numeric)))
    sample[numeric] = sample[numeric].values + noise
    return sample


# ── Kafka publish ─────────────────────────────────────────────────────────────

def _publish_drift_event(event: dict) -> None:
    try:
        from kafka import KafkaProducer  # type: ignore

        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode(),
            request_timeout_ms=5_000,
            retries=1,
        )
        producer.send(KAFKA_TOPIC_DRIFT, value=event)
        producer.flush(timeout=5)
        producer.close()
        log.info("drift_event_published", topic=KAFKA_TOPIC_DRIFT)
    except Exception as exc:
        log.warning("kafka_publish_skipped", error=str(exc))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    s3 = _s3()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # ── 1. Load data ──────────────────────────────────────────────────────────

    log.info("loading_reference", bucket=S3_BUCKET_PROCESSED, key=REFERENCE_KEY)
    try:
        reference = _read_parquet(s3, S3_BUCKET_PROCESSED, REFERENCE_KEY)
    except ClientError as exc:
        log.error("reference_load_failed", error=str(exc))
        sys.exit(1)

    available_cols = [c for c in FEATURE_COLS if c in reference.columns]
    missing_cols   = [c for c in FEATURE_COLS if c not in reference.columns]
    if missing_cols:
        log.warning("reference_missing_cols", missing=missing_cols)

    synthetic = False
    log.info("loading_current", bucket=S3_BUCKET_PROCESSED, key=CURRENT_KEY)
    try:
        current_raw = _read_parquet(s3, S3_BUCKET_PROCESSED, CURRENT_KEY)
        current = current_raw[[c for c in available_cols if c in current_raw.columns]]
    except ClientError:
        log.warning("current_not_found", key=CURRENT_KEY, action="generating_synthetic")
        synthetic = True
        current = _make_synthetic_current(reference, available_cols)
        buf = io.BytesIO()
        current.to_parquet(buf, index=False)
        _upload_bytes(s3, S3_BUCKET_PROCESSED, CURRENT_KEY, buf.getvalue(), "application/octet-stream")

    if len(current) < MIN_CURRENT_ROWS:
        log.info("insufficient_current_rows", rows=len(current), min=MIN_CURRENT_ROWS)
        print(f"{YELLOW}⚠ Not enough current rows ({len(current)} < {MIN_CURRENT_ROWS}), skipping.{RESET}")
        sys.exit(0)

    ref_df = reference[available_cols].copy()
    cur_df = current[[c for c in available_cols if c in current.columns]].copy()

    log.info("data_loaded",
             reference_rows=len(ref_df),
             current_rows=len(cur_df),
             feature_count=len(available_cols),
             synthetic=synthetic)

    # ── 2. Data quality check ─────────────────────────────────────────────────

    log.info("running_data_quality_check")
    quality_report = Report(metrics=[DataQualityPreset()])
    quality_report.run(reference_data=ref_df, current_data=cur_df)
    quality_dict = quality_report.as_dict()

    quality_metrics = quality_dict.get("metrics", [{}])[0].get("result", {})
    current_nulls   = quality_metrics.get("current", {}).get("number_of_missing_values", 0)
    current_cols    = quality_metrics.get("current", {}).get("number_of_columns", len(available_cols))
    null_rate       = current_nulls / max(len(cur_df) * current_cols, 1)

    if null_rate > 0.5:
        log.error("critical_data_quality", null_rate=null_rate)
        print(f"{RED}{BOLD}✗ Critical data quality issue: null_rate={null_rate:.1%}{RESET}")
        sys.exit(1)

    log.info("data_quality_ok", null_rate=f"{null_rate:.2%}")

    # ── 3. Data drift check ───────────────────────────────────────────────────

    log.info("running_drift_check")
    drift_report = Report(metrics=[DataDriftPreset()])
    drift_report.run(reference_data=ref_df, current_data=cur_df)
    drift_dict   = drift_report.as_dict()

    # DataDriftPreset emits two metrics:
    #   metrics[0] = DatasetDriftMetric  → summary stats, no drift_by_columns
    #   metrics[1] = DataDriftTable      → per-column drift_by_columns
    summary_result = drift_dict["metrics"][0]["result"]
    table_result   = drift_dict["metrics"][1]["result"]

    drift_share   = summary_result.get("share_of_drifted_columns", 0.0)
    dataset_drift = summary_result.get("dataset_drift", False)
    drift_by_col  = table_result.get("drift_by_columns", {})

    drifted_features = [
        {"feature": col, "score": info.get("drift_score", 0.0), "method": info.get("stattest_name", "")}
        for col, info in drift_by_col.items()
        if info.get("drift_detected", False)
    ]
    drifted_features.sort(key=lambda x: x["score"], reverse=True)

    log.info("drift_result",
             drift_share=drift_share,
             dataset_drift=dataset_drift,
             drifted_count=len(drifted_features))

    # Save HTML report to MinIO
    report_key = f"reports/drift_{timestamp}.html"
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
            drift_report.save_html(tmp.name)
            tmp.seek(0) if hasattr(tmp, "seek") else None
        with open(tmp.name, "rb") as fh:
            _upload_bytes(s3, S3_BUCKET_DRIFT, report_key, fh.read(), "text/html")
        log.info("report_saved", bucket=S3_BUCKET_DRIFT, key=report_key)
    except Exception as exc:
        log.warning("report_save_failed", error=str(exc))

    # ── 4. Prediction drift ───────────────────────────────────────────────────

    prediction_drift: dict = {}
    if "fraud_probability" in reference.columns and "fraud_probability" in current.columns:
        pred_report = Report(metrics=[ColumnDriftMetric(column_name="fraud_probability")])
        pred_report.run(
            reference_data=reference[["fraud_probability"]],
            current_data=current[["fraud_probability"]],
        )
        pred_result = pred_report.as_dict()["metrics"][0]["result"]
        prediction_drift = {
            "drift_detected": pred_result.get("drift_detected", False),
            "drift_score":    pred_result.get("drift_score", 0.0),
            "stattest":       pred_result.get("stattest_name", ""),
        }
        log.info("prediction_drift", **prediction_drift)
    else:
        log.info("prediction_drift_skipped", reason="fraud_probability column not present")

    # ── 5. Decision ───────────────────────────────────────────────────────────

    reasons: list[str] = []
    if drift_share > DRIFT_THRESHOLD_SHARE:
        reasons.append(f"drift_share {drift_share:.1%} > threshold {DRIFT_THRESHOLD_SHARE:.1%}")
    if prediction_drift.get("drift_detected"):
        reasons.append("fraud_probability distribution shifted")

    should_retrain = len(reasons) > 0

    # ── 6. Drift event ────────────────────────────────────────────────────────

    drift_event = {
        "event_type":       "drift_detected" if should_retrain else "drift_check_ok",
        "timestamp":        timestamp,
        "drift_share":      round(drift_share, 4),
        "dataset_drift":    dataset_drift,
        "drifted_features": drifted_features[:10],
        "prediction_drift": prediction_drift,
        "should_retrain":   should_retrain,
        "reasons":          reasons,
        "report_path":      f"s3://{S3_BUCKET_DRIFT}/{report_key}",
        "synthetic_data":   synthetic,
    }

    with open("/tmp/drift_event.json", "w") as fh:
        json.dump(drift_event, fh, indent=2)

    if should_retrain:
        _publish_drift_event(drift_event)

        print(f"\n{RED}{BOLD}🚨 DRIFT DETECTED — retraining recommended{RESET}")
        print(f"   drift_share      : {drift_share:.1%}  (threshold {DRIFT_THRESHOLD_SHARE:.1%})")
        print(f"   drifted features : {len(drifted_features)} / {len(available_cols)}")
        for f in drifted_features[:5]:
            print(f"     • {f['feature']:<30} score={f['score']:.4f}  ({f['method']})")
        if len(drifted_features) > 5:
            print(f"     … and {len(drifted_features) - 5} more")
        for r in reasons:
            print(f"   reason: {r}")
    else:
        print(f"\n{GREEN}{BOLD}✅ System healthy — no significant drift detected{RESET}")
        print(f"   drift_share: {drift_share:.1%}  (threshold {DRIFT_THRESHOLD_SHARE:.1%})")

    # ── 7 & 8. Metrics JSON for Prometheus scraping ───────────────────────────

    metrics = {
        "drift_share":              round(drift_share, 4),
        "drifted_feature_count":    len(drifted_features),
        "total_feature_count":      len(available_cols),
        "should_retrain":           int(should_retrain),
        "prediction_drift_score":   round(prediction_drift.get("drift_score", 0.0), 4),
        "current_row_count":        len(cur_df),
        "null_rate":                round(null_rate, 4),
        "timestamp":                timestamp,
    }
    with open("/tmp/drift_metrics.json", "w") as fh:
        json.dump(metrics, fh, indent=2)

    log.info("drift_check_complete", **{k: v for k, v in metrics.items() if k != "timestamp"})

    sys.exit(2 if should_retrain else 0)


if __name__ == "__main__":
    main()
