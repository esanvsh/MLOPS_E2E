"""PayShield AI — Drift Monitor Service."""

from __future__ import annotations

import asyncio
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import boto3
import prometheus_client
import structlog
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

S3_ENDPOINT_URL     = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS_KEY    = os.getenv("MINIO_ROOT_USER", "minioadmin")
MINIO_SECRET_KEY    = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
S3_BUCKET_PROCESSED = os.getenv("S3_BUCKET_PROCESSED", "payshield-processed-data")
S3_BUCKET_DRIFT     = os.getenv("S3_BUCKET_DRIFT", "payshield-drift-reports")
APP_ENV             = os.getenv("APP_ENV", "development")
DRIFT_CHECK_INTERVAL_HOURS = int(os.getenv("DRIFT_CHECK_INTERVAL_HOURS", "6"))

# ── Logging ───────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ── Prometheus metrics ────────────────────────────────────────────────────────

DRIFT_CHECKS_TOTAL = prometheus_client.Counter(
    "drift_checks_total", "Total drift checks run"
)
DRIFT_DETECTED_TOTAL = prometheus_client.Counter(
    "drift_detected_total", "Drift checks that exceeded the threshold"
)
LAST_DRIFT_SHARE = prometheus_client.Gauge(
    "last_drift_share", "Drift share from the most recent check (0–1)"
)
LAST_CHECK_TIMESTAMP = prometheus_client.Gauge(
    "last_drift_check_timestamp_seconds", "Unix timestamp of the last drift check"
)
RETRAIN_TRIGGERS_TOTAL = prometheus_client.Counter(
    "retrain_triggers_total", "Number of automatic retraining runs triggered"
)

# ── Shared state ──────────────────────────────────────────────────────────────

_last_check: dict[str, Any] = {}
_check_lock = asyncio.Lock()


# ── S3 helper ─────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )


# ── Drift check runner ────────────────────────────────────────────────────────

def _run_drift_check_sync() -> dict[str, Any]:
    """Run drift_check.main() in a thread; catch SystemExit and return result dict."""
    import sys
    from app.drift_check import main as drift_main

    exit_code = 0
    try:
        drift_main()
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0

    # drift_main writes /tmp/drift_metrics.json before exiting
    metrics: dict[str, Any] = {}
    try:
        with open("/tmp/drift_metrics.json") as fh:
            metrics = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    event: dict[str, Any] = {}
    try:
        with open("/tmp/drift_event.json") as fh:
            event = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return {"exit_code": exit_code, "metrics": metrics, "event": event}


async def _run_drift_check() -> None:
    async with _check_lock:
        log.info("drift_check_starting")
        DRIFT_CHECKS_TOTAL.inc()

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_drift_check_sync)

        metrics    = result.get("metrics", {})
        event      = result.get("event", {})
        exit_code  = result.get("exit_code", 0)
        drift_share = float(metrics.get("drift_share", 0.0))

        LAST_DRIFT_SHARE.set(drift_share)
        LAST_CHECK_TIMESTAMP.set(datetime.now(timezone.utc).timestamp())

        if exit_code == 2:
            DRIFT_DETECTED_TOTAL.inc()
            log.warning("drift_check_result", status="drift_detected", drift_share=drift_share)

            # Trigger retraining in the background (non-blocking)
            loop.run_in_executor(None, _trigger_retrain_sync, event)
        elif exit_code == 1:
            log.error("drift_check_result", status="data_quality_failure")
        else:
            log.info("drift_check_result", status="healthy", drift_share=drift_share)

        _last_check.update({
            "exit_code":   exit_code,
            "drift_share": drift_share,
            "should_retrain": bool(metrics.get("should_retrain", 0)),
            "drifted_feature_count": metrics.get("drifted_feature_count", 0),
            "total_feature_count":   metrics.get("total_feature_count", 0),
            "timestamp": metrics.get("timestamp", ""),
        })


def _trigger_retrain_sync(drift_event: dict) -> None:
    from app.retrain_trigger import trigger_retraining

    RETRAIN_TRIGGERS_TOTAL.inc()
    success = trigger_retraining(drift_event)
    log.info("retrain_trigger_complete", success=success)


# ── Background scheduler ──────────────────────────────────────────────────────

async def _drift_check_scheduler() -> None:
    interval = DRIFT_CHECK_INTERVAL_HOURS * 3600
    log.info("drift_scheduler_started", interval_hours=DRIFT_CHECK_INTERVAL_HOURS)
    while True:
        try:
            await _run_drift_check()
        except Exception as exc:
            log.error("drift_check_unhandled_error", error=str(exc))
        await asyncio.sleep(interval)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_drift_check_scheduler())
    log.info("drift_monitor_started", env=APP_ENV)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("drift_monitor_stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="PayShield Drift Monitor",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "drift-monitor",
        "last_check": _last_check.get("timestamp", "never"),
        "last_drift_share": _last_check.get("drift_share", None),
    }


@app.get("/metrics")
async def metrics():
    data = prometheus_client.generate_latest()
    return Response(content=data, media_type=prometheus_client.CONTENT_TYPE_LATEST)


@app.post("/trigger-check", status_code=202)
async def trigger_check():
    """Kick off a drift check immediately; returns 202 while it runs in the background."""
    if _check_lock.locked():
        return JSONResponse(
            status_code=409,
            content={"detail": "A drift check is already running"},
        )
    asyncio.create_task(_run_drift_check())
    return {"status": "accepted", "message": "Drift check started in background"}


@app.get("/last-report")
async def last_report():
    """Return the S3 URL of the most recent drift HTML report."""
    s3 = _s3()
    try:
        resp = s3.list_objects_v2(
            Bucket=S3_BUCKET_DRIFT,
            Prefix="reports/drift_",
        )
    except ClientError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    objects = resp.get("Contents", [])
    if not objects:
        return JSONResponse(status_code=404, content={"detail": "No drift reports found"})

    latest = max(objects, key=lambda o: o["LastModified"])
    key    = latest["Key"]
    url    = f"s3://{S3_BUCKET_DRIFT}/{key}"

    # Generate a presigned URL so it can be opened directly in a browser
    try:
        presigned = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_DRIFT, "Key": key},
            ExpiresIn=3600,
        )
    except Exception:
        presigned = None

    return {
        "s3_url":      url,
        "presigned_url": presigned,
        "last_modified": latest["LastModified"].isoformat(),
        "size_bytes":  latest["Size"],
    }
