import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .features import apply_preprocessing, build_feature_vector, classify_risk
from .metrics import (
    FRAUD_AMOUNT_SAVED,
    HIGH_RISK_TOTAL,
    MODEL_CONFIDENCE,
    MODEL_INFO,
    MODEL_LOADED,
    PREDICTIONS_TOTAL,
    PREDICTION_LATENCY,
    REDIS_ERRORS,
    REQUEST_COUNT,
    REQUEST_LATENCY,
)
from .model_loader import MODEL_LOADER, MODEL_STAGE
from .schemas import (
    FraudPredictionResponse,
    HealthResponse,
    ModelInfoResponse,
    TransactionFeatureInput,
)

logger = structlog.get_logger()

RELOAD_SECRET = os.getenv("RELOAD_SECRET", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

redis_client: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client

    loaded = MODEL_LOADER.load_model()
    MODEL_LOADED.set(1 if loaded else 0)
    if loaded:
        MODEL_INFO.info({
            "model_name": MODEL_LOADER.model_name,
            "version": MODEL_LOADER.model_version,
            "stage": MODEL_STAGE,
            "loaded_at": MODEL_LOADER.loaded_at,
            "feature_count": str(len(MODEL_LOADER.feature_names)),
        })
        logger.info(
            "model_ready",
            version=MODEL_LOADER.model_version,
            feature_count=len(MODEL_LOADER.feature_names),
        )
    else:
        logger.warning("model_not_loaded_on_startup")

    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("redis_connected", url=REDIS_URL)
    except Exception:
        logger.exception("redis_connection_failed", url=REDIS_URL)
        redis_client = None

    yield

    if redis_client:
        await redis_client.aclose()


app = FastAPI(title="PayShield Fraud Model API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    latency = time.time() - start
    endpoint = request.url.path
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=endpoint,
        status_code=str(response.status_code),
    ).inc()
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency)
    logger.info(
        "http_request",
        method=request.method,
        path=endpoint,
        status_code=response.status_code,
        latency_ms=round(latency * 1000, 2),
    )
    return response


@app.post("/predict", response_model=FraudPredictionResponse)
async def predict(input: TransactionFeatureInput):
    if not MODEL_LOADER.is_loaded():
        return JSONResponse(
            status_code=503,
            content={"error": "Model not loaded, retry in 30s"},
        )

    start = time.time()
    threshold = float(os.getenv("MODEL_THRESHOLD", "0.35"))

    X = build_feature_vector(input, MODEL_LOADER.feature_names, MODEL_LOADER.preprocessor)
    X = apply_preprocessing(X, MODEL_LOADER.preprocessor, MODEL_LOADER.feature_names)

    prob = float(MODEL_LOADER.model.predict_proba(X)[0][1])
    risk = classify_risk(prob)
    prediction = 1 if prob >= threshold else 0
    latency_ms = (time.time() - start) * 1000

    PREDICTIONS_TOTAL.labels(risk_level=risk).inc()
    PREDICTION_LATENCY.observe(latency_ms)
    MODEL_CONFIDENCE.observe(prob)

    if risk == "HIGH":
        HIGH_RISK_TOTAL.inc()
        FRAUD_AMOUNT_SAVED.inc(input.amount)

    if redis_client and input.transaction_id:
        try:
            await redis_client.setex(
                f"risk_cache:{input.transaction_id}",
                300,
                f"{prob:.6f}:{risk}",
            )
        except Exception:
            REDIS_ERRORS.inc()
            logger.warning("redis_cache_write_failed", transaction_id=input.transaction_id)

    logger.info(
        "fraud_prediction",
        transaction_id=input.transaction_id,
        fraud_probability=round(prob, 4),
        risk_level=risk,
        prediction=prediction,
        latency_ms=round(latency_ms, 2),
        model_version=MODEL_LOADER.model_version,
    )

    result = FraudPredictionResponse(
        transaction_id=input.transaction_id,
        prediction=prediction,
        fraud_probability=prob,
        risk_level=risk,
        model_version=MODEL_LOADER.model_version,
        threshold_used=threshold,
        latency_ms=round(latency_ms, 2),
    )

    response = JSONResponse(content=result.model_dump())
    response.headers["X-Model-Version"] = MODEL_LOADER.model_version
    response.headers["X-Prediction-Latency"] = f"{latency_ms:.2f}ms"
    return response


@app.get("/health", response_model=HealthResponse)
async def health():
    model_loaded = MODEL_LOADER.is_loaded()
    redis_connected = False

    if redis_client:
        try:
            await redis_client.ping()
            redis_connected = True
        except Exception:
            REDIS_ERRORS.inc()
            logger.warning("redis_ping_failed")

    response_body = HealthResponse(
        status="healthy" if model_loaded else "degraded",
        model_loaded=model_loaded,
        model_version=MODEL_LOADER.model_version,
        redis_connected=redis_connected,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    return JSONResponse(
        content=response_body.model_dump(),
        status_code=200 if model_loaded else 503,
    )


@app.get("/model/info", response_model=ModelInfoResponse)
async def model_info():
    if not MODEL_LOADER.is_loaded():
        raise HTTPException(status_code=503, detail="Model not loaded")
    return ModelInfoResponse(
        model_name=MODEL_LOADER.model_name,
        model_version=MODEL_LOADER.model_version,
        stage=MODEL_STAGE,
        loaded_at=MODEL_LOADER.loaded_at,
        feature_count=len(MODEL_LOADER.feature_names),
    )


@app.post("/model/reload")
async def reload_model(x_reload_key: str = Header(default="")):
    if not RELOAD_SECRET or x_reload_key != RELOAD_SECRET:
        raise HTTPException(status_code=403, detail="Invalid or missing reload key")

    success = MODEL_LOADER.reload_model()
    MODEL_LOADED.set(1 if success else 0)
    if success:
        MODEL_INFO.info({
            "model_name": MODEL_LOADER.model_name,
            "version": MODEL_LOADER.model_version,
            "stage": MODEL_STAGE,
            "loaded_at": MODEL_LOADER.loaded_at,
            "feature_count": str(len(MODEL_LOADER.feature_names)),
        })
    logger.info("model_reload_requested", success=success, version=MODEL_LOADER.model_version)
    return {"success": success, "model_version": MODEL_LOADER.model_version}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
