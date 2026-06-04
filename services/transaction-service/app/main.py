"""PayShield AI — Transaction Service."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from kafka import KafkaProducer
import prometheus_client
from pydantic import BaseModel, ConfigDict, Field
import structlog
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

DYNAMODB_ENDPOINT: str = os.getenv("DYNAMODB_ENDPOINT", "http://localhost:8000")
DYNAMODB_REGION: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DYNAMODB_TABLE_TRANSACTIONS: str = os.getenv(
    "DYNAMODB_TABLE_TRANSACTIONS", "payshield-transactions-dev"
)
AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "local")
AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "local")
KAFKA_BOOTSTRAP_SERVERS: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC_TRANSACTIONS: str = os.getenv("KAFKA_TOPIC_TRANSACTIONS", "transactions.raw")
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

TRANSACTION_CREATED = prometheus_client.Counter(
    "transaction_created_total",
    "Total transactions created",
)
TRANSACTION_DURATION = prometheus_client.Histogram(
    "transaction_processing_duration_seconds",
    "Transaction processing duration in seconds",
)
KAFKA_ERRORS = prometheus_client.Counter(
    "kafka_produce_errors_total",
    "Total Kafka produce errors",
)
DYNAMODB_WRITE_ERRORS = prometheus_client.Counter(
    "dynamodb_write_errors_total",
    "Total DynamoDB write errors",
)

# ── Module-level clients (initialised in lifespan) ────────────────────────────

_table: Any = None
_kafka_producer: KafkaProducer | None = None


def _build_table() -> Any:
    kwargs: dict[str, Any] = {
        "region_name": DYNAMODB_REGION,
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    }
    if DYNAMODB_ENDPOINT:
        kwargs["endpoint_url"] = DYNAMODB_ENDPOINT
    return boto3.resource("dynamodb", **kwargs).Table(DYNAMODB_TABLE_TRANSACTIONS)


def _build_kafka_producer() -> KafkaProducer:
    config: dict[str, Any] = {
        "bootstrap_servers": KAFKA_BOOTSTRAP_SERVERS,
        "acks": "all",
        "retries": 3,
        "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
    }
    if APP_ENV == "development":
        config["security_protocol"] = "PLAINTEXT"
    return KafkaProducer(**config)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _table, _kafka_producer

    _table = _build_table()
    try:
        await asyncio.to_thread(_table.load)
        log.info("dynamodb_ready", table=DYNAMODB_TABLE_TRANSACTIONS, endpoint=DYNAMODB_ENDPOINT)
    except Exception as exc:
        log.error("dynamodb_startup_failed", error=str(exc))
        raise

    try:
        _kafka_producer = await asyncio.to_thread(_build_kafka_producer)
        log.info("kafka_ready", servers=KAFKA_BOOTSTRAP_SERVERS)
    except Exception as exc:
        log.warning("kafka_startup_failed_continuing", error=str(exc))
        _kafka_producer = None

    yield

    if _kafka_producer is not None:
        try:
            await asyncio.to_thread(_kafka_producer.flush)
            _kafka_producer.close()
            log.info("kafka_producer_closed")
        except Exception as exc:
            log.warning("kafka_shutdown_error", error=str(exc))

    log.info("shutdown_complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PayShield Transaction Service", version="1.0.0", lifespan=lifespan)

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
        ip=request.client.host if request.client else "unknown",
    )
    return response


# ── Models ────────────────────────────────────────────────────────────────────

class TransactionCreate(BaseModel):
    amount: float = Field(gt=0, lt=1_000_000)
    merchant_category: Literal[
        "electronics", "grocery", "travel", "entertainment", "food", "healthcare", "other"
    ]
    country: str = "IN"
    payment_method: Literal["credit_card", "debit_card", "upi", "neft", "imps"]
    device_id: str = ""
    ip_address: str = ""


class TransactionResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    transaction_id: str
    user_id: str
    amount: float
    merchant_category: str
    country: str
    payment_method: str
    status: str
    fraud_prediction: str
    risk_level: str
    fraud_probability: float | None
    model_version: str | None
    created_at: str


class PredictionUpdate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    fraud_prediction: str
    fraud_probability: float
    risk_level: str
    model_version: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _item_to_response(item: dict[str, Any]) -> TransactionResponse:
    raw_prob = item.get("fraud_probability")
    return TransactionResponse(
        transaction_id=item["transaction_id"],
        user_id=item["user_id"],
        amount=float(item["amount"]),
        merchant_category=item["merchant_category"],
        country=item["country"],
        payment_method=item["payment_method"],
        status=item["status"],
        fraud_prediction=item["fraud_prediction"],
        risk_level=item["risk_level"],
        fraud_probability=float(raw_prob) if raw_prob is not None else None,
        model_version=item.get("model_version"),
        created_at=item["created_at"],
    )


async def _publish_transaction_event(event: dict[str, Any]) -> None:
    if _kafka_producer is None:
        log.warning("kafka_unavailable_skipping_publish", event_id=event.get("event_id"))
        return
    try:
        await asyncio.to_thread(_kafka_producer.send, KAFKA_TOPIC_TRANSACTIONS, value=event)
    except Exception as exc:
        KAFKA_ERRORS.inc()
        log.warning("kafka_produce_failed", error=str(exc), event_id=event.get("event_id"))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/transactions", status_code=201, response_model=TransactionResponse)
async def create_transaction(
    body: TransactionCreate,
    x_user_id: str = Header(...),
    x_user_role: str = Header(default="CUSTOMER"),
):
    t0 = time.perf_counter()
    transaction_id = "txn_" + uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    item: dict[str, Any] = {
        "transaction_id": transaction_id,
        "user_id": x_user_id,
        "amount": Decimal(str(body.amount)),
        "merchant_category": body.merchant_category,
        "country": body.country,
        "payment_method": body.payment_method,
        "device_id": body.device_id,
        "ip_address": body.ip_address,
        "status": "PROCESSING",
        "fraud_prediction": "PENDING",
        "risk_level": "UNKNOWN",
        "created_at": now,
    }

    try:
        await asyncio.to_thread(_table.put_item, Item=item)
    except Exception as exc:
        DYNAMODB_WRITE_ERRORS.inc()
        log.error("dynamodb_write_failed", error=str(exc), transaction_id=transaction_id)
        raise HTTPException(status_code=503, detail="Failed to store transaction")

    event = {
        "event_id": "evt_" + uuid.uuid4().hex[:8],
        "event_type": "TRANSACTION_CREATED",
        "transaction_id": transaction_id,
        "user_id": x_user_id,
        "amount": body.amount,
        "merchant_category": body.merchant_category,
        "country": body.country,
        "payment_method": body.payment_method,
        "device_id": body.device_id,
        "event_time": now,
    }
    await _publish_transaction_event(event)

    TRANSACTION_CREATED.inc()
    TRANSACTION_DURATION.observe(time.perf_counter() - t0)
    log.info("transaction_created", transaction_id=transaction_id, user_id=x_user_id, amount=body.amount)

    return TransactionResponse(
        transaction_id=transaction_id,
        user_id=x_user_id,
        amount=body.amount,
        merchant_category=body.merchant_category,
        country=body.country,
        payment_method=body.payment_method,
        status="PROCESSING",
        fraud_prediction="PENDING",
        risk_level="UNKNOWN",
        fraud_probability=None,
        model_version=None,
        created_at=now,
    )


@app.get("/transactions", response_model=list[TransactionResponse])
async def list_transactions(x_user_id: str = Header(...)):
    try:
        result = await asyncio.to_thread(
            _table.query,
            IndexName="user-id-index",
            KeyConditionExpression=Key("user_id").eq(x_user_id),
            Limit=20,
            ScanIndexForward=False,
        )
    except Exception as exc:
        log.error("dynamodb_query_failed", error=str(exc), user_id=x_user_id)
        raise HTTPException(status_code=503, detail="Failed to retrieve transactions")

    return [_item_to_response(item) for item in result.get("Items", [])]


# NOTE: must be declared before /{transaction_id} so "fraud" is not treated as a path param
@app.get("/transactions/fraud", response_model=list[TransactionResponse])
async def list_fraud_transactions(
    x_user_id: str = Header(...),
    x_user_role: str = Header(default="CUSTOMER"),
):
    if x_user_role not in ("ADMIN", "ANALYST"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    items: list[dict[str, Any]] = []
    for risk_level in ("HIGH", "MEDIUM"):
        try:
            result = await asyncio.to_thread(
                _table.query,
                IndexName="risk-level-index",
                KeyConditionExpression=Key("risk_level").eq(risk_level),
                Limit=20,
                ScanIndexForward=False,
            )
            items.extend(result.get("Items", []))
        except Exception as exc:
            log.error("dynamodb_query_failed", error=str(exc), risk_level=risk_level)

    return [_item_to_response(item) for item in items]


@app.get("/transactions/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(transaction_id: str, x_user_id: str = Header(...)):
    try:
        result = await asyncio.to_thread(
            _table.get_item,
            Key={"transaction_id": transaction_id},
        )
    except Exception as exc:
        log.error("dynamodb_get_failed", error=str(exc), transaction_id=transaction_id)
        raise HTTPException(status_code=503, detail="Failed to retrieve transaction")

    item = result.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return _item_to_response(item)


@app.patch("/transactions/{transaction_id}/prediction", response_model=TransactionResponse)
async def update_prediction(transaction_id: str, body: PredictionUpdate):
    try:
        result = await asyncio.to_thread(
            _table.update_item,
            Key={"transaction_id": transaction_id},
            UpdateExpression=(
                "SET fraud_prediction = :fp, fraud_probability = :prob, "
                "risk_level = :rl, model_version = :mv, #st = :st"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":fp": body.fraud_prediction,
                ":prob": Decimal(str(body.fraud_probability)),
                ":rl": body.risk_level,
                ":mv": body.model_version,
                ":st": "SCORED",
            },
            ConditionExpression="attribute_exists(transaction_id)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status_code=404, detail="Transaction not found")
        DYNAMODB_WRITE_ERRORS.inc()
        log.error("dynamodb_update_failed", error=str(exc), transaction_id=transaction_id)
        raise HTTPException(status_code=503, detail="Failed to update prediction")

    log.info(
        "prediction_updated",
        transaction_id=transaction_id,
        risk_level=body.risk_level,
        fraud_probability=body.fraud_probability,
    )
    return _item_to_response(result["Attributes"])


@app.get("/health")
async def health():
    now = datetime.now(timezone.utc).isoformat()

    try:
        await asyncio.to_thread(_table.meta.client.list_tables)
        db_status = "healthy"
    except Exception as exc:
        log.error("health_dynamodb_failed", error=str(exc))
        db_status = "unhealthy"

    kafka_status = "healthy" if _kafka_producer is not None else "unavailable"

    overall = "healthy" if db_status == "healthy" else "unhealthy"
    return JSONResponse(
        content={
            "status": overall,
            "dynamodb": db_status,
            "kafka": kafka_status,
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
