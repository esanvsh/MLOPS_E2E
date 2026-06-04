"""PayShield AI — Feature Service."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from kafka import KafkaConsumer, KafkaProducer
import prometheus_client
import redis.asyncio as aioredis
import structlog
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

KAFKA_BOOTSTRAP_SERVERS: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_CONSUMER_GROUP_ID: str = os.getenv("KAFKA_CONSUMER_GROUP_ID", "feature-service-group")
KAFKA_TOPIC_RAW: str = "transactions.raw"
KAFKA_TOPIC_SCORED: str = "transactions.scored"
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
FRAUD_MODEL_API_URL: str = os.getenv("FRAUD_MODEL_API_URL", "http://localhost:8004")
TRANSACTION_SERVICE_URL: str = os.getenv("TRANSACTION_SERVICE_URL", "http://localhost:8003")
APP_ENV: str = os.getenv("APP_ENV", "development")

# ── Structured logging ────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger()

# ── Prometheus metrics ────────────────────────────────────────────────────────

MESSAGES_CONSUMED = prometheus_client.Counter(
    "feature_messages_consumed_total",
    "Total Kafka messages processed",
)
PROCESSING_DURATION = prometheus_client.Histogram(
    "feature_processing_duration_seconds",
    "End-to-end message processing duration in seconds",
)
MODEL_ERRORS = prometheus_client.Counter(
    "feature_model_errors_total",
    "Total fraud model call errors",
)
KAFKA_PRODUCE_ERRORS = prometheus_client.Counter(
    "feature_kafka_produce_errors_total",
    "Total Kafka produce errors for scored events",
)

# ── Module-level clients (initialised in lifespan) ────────────────────────────

_redis: aioredis.Redis | None = None
_consumer: KafkaConsumer | None = None
_producer: KafkaProducer | None = None
_http_client: httpx.AsyncClient | None = None
_consumer_task: asyncio.Task | None = None


def _build_consumer() -> KafkaConsumer:
    config: dict[str, Any] = {
        "group_id": KAFKA_CONSUMER_GROUP_ID,
        "auto_offset_reset": "latest",
        "enable_auto_commit": True,
        "value_deserializer": lambda v: json.loads(v.decode("utf-8")),
    }
    if APP_ENV == "development":
        config["security_protocol"] = "PLAINTEXT"
    return KafkaConsumer(KAFKA_TOPIC_RAW, bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS, **config)


def _build_producer() -> KafkaProducer:
    config: dict[str, Any] = {
        "bootstrap_servers": KAFKA_BOOTSTRAP_SERVERS,
        "acks": "all",
        "retries": 3,
        "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
    }
    if APP_ENV == "development":
        config["security_protocol"] = "PLAINTEXT"
    return KafkaProducer(**config)


# ── Feature engineering ───────────────────────────────────────────────────────

async def build_inference_features(
    event: dict[str, Any], redis_client: aioredis.Redis
) -> dict[str, Any]:
    user_id: str = event["user_id"]
    amount: float = float(event["amount"])
    merchant_category: str = event["merchant_category"]
    payment_method: str = event["payment_method"]
    device_id: str = event.get("device_id", "")
    event_time: str = event["event_time"]

    dt = datetime.fromisoformat(event_time)
    hour_of_day: int = dt.hour
    day_of_week: int = dt.weekday()

    amount_log: float = math.log1p(amount)
    is_round_amount: int = 1 if amount % 100 == 0 else 0
    is_late_night: int = 1 if (hour_of_day >= 23 or hour_of_day <= 4) else 0
    is_weekend: int = 1 if day_of_week >= 5 else 0
    is_high_risk_category: int = 1 if merchant_category in ("electronics", "travel") else 0

    payment_channel_encoded: int = {
        "credit_card": 0,
        "debit_card": 1,
        "upi": 2,
        "neft": 3,
        "imps": 4,
    }.get(payment_method, 0)

    vel_1h_key = f"vel:1h:{user_id}"
    user_txn_count_1h: int = await redis_client.incr(vel_1h_key)
    if user_txn_count_1h == 1:
        await redis_client.expire(vel_1h_key, 3600)

    vel_24h_key = f"vel:24h:{user_id}"
    user_txn_count_24h: int = await redis_client.incr(vel_24h_key)
    if user_txn_count_24h == 1:
        await redis_client.expire(vel_24h_key, 86400)

    is_new_device = 0
    if device_id:
        added = await redis_client.sadd(f"devices:{user_id}", device_id)
        is_new_device = 1 if added > 0 else 0

    amount_x_hour: float = amount_log * hour_of_day
    amount_x_velocity: float = amount_log * min(user_txn_count_24h, 20)

    return {
        "amount_log": amount_log,
        "is_round_amount": is_round_amount,
        "hour_of_day": hour_of_day,
        "day_of_week": day_of_week,
        "is_late_night": is_late_night,
        "is_weekend": is_weekend,
        "user_txn_count_1h": user_txn_count_1h,
        "user_txn_count_24h": user_txn_count_24h,
        "is_new_device": is_new_device,
        "is_high_risk_category": is_high_risk_category,
        "payment_channel_encoded": payment_channel_encoded,
        "addr_mismatch": 0,
        "email_domain_risk": 0,
        "amount_x_hour": amount_x_hour,
        "amount_x_velocity": amount_x_velocity,
    }


# ── Downstream calls ──────────────────────────────────────────────────────────

async def _call_fraud_model(
    transaction_id: str, event: dict[str, Any], features: dict[str, Any]
) -> dict[str, Any]:
    try:
        payload = {
            "transaction_id": transaction_id,
            "amount": float(event["amount"]),
            "merchant_category": event.get("merchant_category", "other"),
            "country": event.get("country", "IN"),
            "hour": features["hour_of_day"],
            "day_of_week": features["day_of_week"],
            "payment_method": event.get("payment_method", "upi"),
            "failed_attempts": event.get("failed_attempts", 0),
            "is_new_device": features["is_new_device"],
            "user_txn_count_24h": features["user_txn_count_24h"],
            "addr_mismatch": features["addr_mismatch"],
            "email_domain_risk": features["email_domain_risk"],
            "is_high_risk_category": features["is_high_risk_category"],
            "device_id": event.get("device_id", ""),
            "user_id": event.get("user_id", ""),
        }
        resp = await _http_client.post(
            f"{FRAUD_MODEL_API_URL}/predict",
            json=payload,
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "fraud_probability": data["fraud_probability"],
            "fraud_prediction": "FRAUD" if data["prediction"] == 1 else "GENUINE",
            "risk_level": data["risk_level"],
            "model_version": data["model_version"],
        }
    except Exception as exc:
        MODEL_ERRORS.inc()
        log.warning("fraud_model_unavailable", error=str(exc), transaction_id=transaction_id)
        return {
            "fraud_prediction": "UNKNOWN",
            "fraud_probability": 0.0,
            "risk_level": "UNKNOWN",
            "model_version": "unavailable",
        }


async def _patch_transaction(transaction_id: str, prediction: dict[str, Any]) -> None:
    try:
        resp = await _http_client.patch(
            f"{TRANSACTION_SERVICE_URL}/transactions/{transaction_id}/prediction",
            json=prediction,
            timeout=3.0,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.error("patch_transaction_failed", error=str(exc), transaction_id=transaction_id)


async def _publish_scored_event(
    event: dict[str, Any], prediction: dict[str, Any]
) -> None:
    if _producer is None:
        return
    scored_event = {
        "event_id": "evt_" + uuid.uuid4().hex[:8],
        "event_type": "TRANSACTION_SCORED",
        "transaction_id": event["transaction_id"],
        "user_id": event["user_id"],
        "fraud_probability": prediction["fraud_probability"],
        "fraud_prediction": prediction["fraud_prediction"],
        "risk_level": prediction["risk_level"],
        "model_version": prediction["model_version"],
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await asyncio.to_thread(_producer.send, KAFKA_TOPIC_SCORED, value=scored_event)
    except Exception as exc:
        KAFKA_PRODUCE_ERRORS.inc()
        log.warning(
            "kafka_scored_produce_failed",
            error=str(exc),
            transaction_id=event["transaction_id"],
        )


# ── Message processing ────────────────────────────────────────────────────────

async def _process_message(event: dict[str, Any]) -> None:
    transaction_id: str = event.get("transaction_id", "unknown")
    t0 = time.perf_counter()

    try:
        features = await build_inference_features(event, _redis)
    except Exception as exc:
        log.error("feature_build_failed", error=str(exc), transaction_id=transaction_id)
        return

    prediction = await _call_fraud_model(transaction_id, event, features)

    await _patch_transaction(transaction_id, prediction)
    await _publish_scored_event(event, prediction)

    elapsed = time.perf_counter() - t0
    MESSAGES_CONSUMED.inc()
    PROCESSING_DURATION.observe(elapsed)
    log.info(
        "message_processed",
        transaction_id=transaction_id,
        risk_level=prediction["risk_level"],
        fraud_probability=prediction["fraud_probability"],
        duration_ms=round(elapsed * 1000, 2),
    )


async def _consume_loop() -> None:
    log.info("consumer_loop_started", topic=KAFKA_TOPIC_RAW, group=KAFKA_CONSUMER_GROUP_ID)
    while True:
        try:
            records: dict = await asyncio.to_thread(_consumer.poll, 1000)
            for _tp, messages in records.items():
                for msg in messages:
                    try:
                        await _process_message(msg.value)
                    except Exception as exc:
                        log.error("message_processing_error", error=str(exc))
        except asyncio.CancelledError:
            log.info("consumer_loop_cancelled")
            break
        except Exception as exc:
            log.error("consumer_loop_error", error=str(exc))
            await asyncio.sleep(2)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _redis, _consumer, _producer, _http_client, _consumer_task

    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await _redis.ping()
        log.info("redis_ready", url=REDIS_URL)
    except Exception as exc:
        log.error("redis_startup_failed", error=str(exc))
        raise

    try:
        _consumer = await asyncio.to_thread(_build_consumer)
        log.info("kafka_consumer_ready", topic=KAFKA_TOPIC_RAW, group=KAFKA_CONSUMER_GROUP_ID)
    except Exception as exc:
        log.warning("kafka_consumer_startup_failed_continuing", error=str(exc))
        _consumer = None

    try:
        _producer = await asyncio.to_thread(_build_producer)
        log.info("kafka_producer_ready")
    except Exception as exc:
        log.warning("kafka_producer_startup_failed_continuing", error=str(exc))
        _producer = None

    _http_client = httpx.AsyncClient()

    if _consumer is not None:
        _consumer_task = asyncio.create_task(_consume_loop())
        log.info("consumer_task_started")

    yield

    if _consumer_task is not None:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass

    if _consumer is not None:
        await asyncio.to_thread(_consumer.close)
        log.info("kafka_consumer_closed")

    if _producer is not None:
        try:
            await asyncio.to_thread(_producer.flush)
            _producer.close()
            log.info("kafka_producer_closed")
        except Exception as exc:
            log.warning("kafka_producer_shutdown_error", error=str(exc))

    if _http_client is not None:
        await _http_client.aclose()

    await _redis.aclose()
    log.info("shutdown_complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PayShield Feature Service", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - t0
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(elapsed * 1000, 2),
    )
    return response


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    now = datetime.now(timezone.utc).isoformat()

    try:
        await _redis.ping()
        redis_status = "healthy"
    except Exception as exc:
        log.error("health_redis_failed", error=str(exc))
        redis_status = "unhealthy"

    kafka_consumer_status = "healthy" if _consumer is not None else "unavailable"
    kafka_producer_status = "healthy" if _producer is not None else "unavailable"
    consumer_task_running = (
        _consumer_task is not None and not _consumer_task.done()
    )

    overall = "healthy" if redis_status == "healthy" else "unhealthy"
    return JSONResponse(
        content={
            "status": overall,
            "redis": redis_status,
            "kafka_consumer": kafka_consumer_status,
            "kafka_producer": kafka_producer_status,
            "consumer_task": "running" if consumer_task_running else "stopped",
            "timestamp": now,
        },
        status_code=200 if overall == "healthy" else 503,
    )


@app.get("/metrics")
async def metrics():
    return Response(
        content=prometheus_client.generate_latest(),
        media_type=prometheus_client.CONTENT_TYPE_LATEST,
    )
