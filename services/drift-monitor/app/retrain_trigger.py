"""Retraining trigger: feature rebuild → model train → hot-reload."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import boto3
import mlflow
import structlog
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

MLFLOW_TRACKING_URI  = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5050")
MODEL_NAME           = os.getenv("MODEL_NAME", "fraud-risk-model")
FRAUD_MODEL_API_URL  = os.getenv("FRAUD_MODEL_API_URL", "http://localhost:8004")
RELOAD_SECRET        = os.getenv("RELOAD_SECRET", "")
S3_ENDPOINT_URL      = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS_KEY     = os.getenv("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY     = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
S3_BUCKET_PROCESSED  = os.getenv("S3_BUCKET_PROCESSED", "payshield-processed-data")

BUILD_FEATURES_SCRIPT = "/app/ml/feature_engineering/build_features.py"
TRAIN_SCRIPT          = "/app/ml/training/train.py"

log = structlog.get_logger()


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )


def _current_production_version() -> int | None:
    """Return the current Production model version number, or None if absent."""
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    try:
        client = mlflow.tracking.MlflowClient()
        versions = client.get_latest_versions(MODEL_NAME, stages=["Production"])
        return int(versions[0].version) if versions else None
    except Exception as exc:
        log.warning("mlflow_version_check_failed", error=str(exc))
        return None


def _run(cmd: list[str], step: str) -> subprocess.CompletedProcess:
    log.info(f"{step}_start", cmd=" ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )


def trigger_retraining(drift_event: dict[str, Any]) -> bool:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log.info("retraining_start", drift_share=drift_event.get("drift_share"), timestamp=timestamp)

    previous_version = _current_production_version()
    log.info("current_production_version", version=previous_version)

    # ── Step 1: feature engineering ──────────────────────────────────────────

    result = _run([sys.executable, BUILD_FEATURES_SCRIPT], "build_features")
    if result.returncode != 0:
        log.error("build_features_failed",
                  returncode=result.returncode,
                  stdout=result.stdout[-2000:],
                  stderr=result.stderr[-2000:])
        return False
    log.info("build_features_complete")

    # ── Step 2: model training ────────────────────────────────────────────────

    dataset_version = f"retrain_{timestamp}"
    result = _run(
        [sys.executable, TRAIN_SCRIPT, "--dataset-version", dataset_version],
        "train",
    )
    if result.returncode != 0:
        log.error("train_failed",
                  returncode=result.returncode,
                  stdout=result.stdout[-2000:],
                  stderr=result.stderr[-2000:])
        return False
    log.info("train_complete", dataset_version=dataset_version)

    # ── Step 3: verify a new Production model was promoted ───────────────────

    new_version = _current_production_version()
    if new_version is None or (previous_version is not None and new_version <= previous_version):
        log.warning("model_not_promoted",
                    previous=previous_version,
                    new=new_version,
                    reason="promotion criteria not met")
        return False
    log.info("new_model_promoted", previous=previous_version, new=new_version)

    # ── Step 4: hot-reload fraud-model-api ───────────────────────────────────

    try:
        import httpx  # available via fraud-model-api's transitive deps

        resp = httpx.post(
            f"{FRAUD_MODEL_API_URL}/model/reload",
            headers={"X-Reload-Key": RELOAD_SECRET},
            timeout=30.0,
        )
        if resp.status_code == 200:
            log.info("model_hot_reloaded", version=new_version)
            print(f"✅ Model hot-reloaded (v{new_version})")
        else:
            log.warning("reload_non_200", status=resp.status_code, body=resp.text)
    except Exception as exc:
        log.warning("reload_request_failed", error=str(exc))

    # ── Step 5: promote current features → new reference baseline ────────────

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current_key   = f"inference/live_features_{today}.parquet"
    reference_key = "features/reference.parquet"

    s3 = _s3()
    try:
        obj = s3.get_object(Bucket=S3_BUCKET_PROCESSED, Key=current_key)
        data = obj["Body"].read()
        s3.put_object(
            Bucket=S3_BUCKET_PROCESSED,
            Key=reference_key,
            Body=data,
            ContentType="application/octet-stream",
        )
        log.info("reference_baseline_updated", source=current_key, dest=reference_key)
    except ClientError as exc:
        log.warning("baseline_update_failed", error=str(exc))

    return True
