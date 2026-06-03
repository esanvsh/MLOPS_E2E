"""PayShield AI — Auth Service."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import bcrypt
import boto3
from boto3.dynamodb.conditions import Key
import jwt
from fastapi import FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import prometheus_client
from pydantic import BaseModel, EmailStr
import redis.asyncio as aioredis
import structlog
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

DYNAMODB_ENDPOINT: str = os.getenv("DYNAMODB_ENDPOINT", "http://localhost:8000")
DYNAMODB_REGION: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DYNAMODB_TABLE_USERS: str = os.getenv("DYNAMODB_TABLE_USERS", "payshield-users-dev")
AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "local")
AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "local")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRY_MINUTES: int = int(os.getenv("JWT_EXPIRY_MINUTES", "60"))

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

REQUEST_COUNT = prometheus_client.Counter(
    "auth_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_DURATION = prometheus_client.Histogram(
    "auth_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)
LOGIN_ATTEMPTS = prometheus_client.Counter(
    "auth_login_attempts_total",
    "Login attempts by result",
    ["result"],
)

# ── Module-level clients (initialised in lifespan) ────────────────────────────

_redis: aioredis.Redis | None = None
_table: Any = None


def _build_table() -> Any:
    kwargs: dict[str, Any] = {
        "region_name": DYNAMODB_REGION,
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    }
    if DYNAMODB_ENDPOINT:
        kwargs["endpoint_url"] = DYNAMODB_ENDPOINT
    return boto3.resource("dynamodb", **kwargs).Table(DYNAMODB_TABLE_USERS)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _table, _redis

    _table = _build_table()
    try:
        await asyncio.to_thread(_table.load)
        log.info("dynamodb_ready", table=DYNAMODB_TABLE_USERS, endpoint=DYNAMODB_ENDPOINT)
    except Exception as exc:
        log.error("dynamodb_startup_failed", error=str(exc))
        raise

    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await _redis.ping()
        log.info("redis_ready", url=REDIS_URL)
    except Exception as exc:
        log.error("redis_startup_failed", error=str(exc))
        raise

    yield

    await _redis.aclose()
    log.info("shutdown_complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PayShield Auth Service", version="1.0.0", lifespan=lifespan)

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

    REQUEST_COUNT.labels(
        method=request.method,
        path=request.url.path,
        status=str(response.status_code),
    ).inc()
    REQUEST_DURATION.labels(method=request.method, path=request.url.path).observe(elapsed)
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

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    role: str = "CUSTOMER"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _encode_jwt(user_id: str, email: str, role: str) -> tuple[str, str]:
    token_id = uuid.uuid4().hex
    payload = {
        "user_id": user_id,
        "email": email,
        "role": role,
        "token_id": token_id,
        "exp": datetime.utcnow() + timedelta(minutes=JWT_EXPIRY_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), token_id


def _decode_jwt(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def _enforce_rate_limit(ip: str) -> None:
    key = f"rate_limit_login:{ip}"
    count = await _redis.incr(key)
    if count == 1:
        await _redis.expire(key, 60)
    if count > 5:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in a minute.",
        )


async def _find_user_by_email(email: str) -> dict[str, Any] | None:
    result = await asyncio.to_thread(
        _table.query,
        IndexName="email-index",
        KeyConditionExpression=Key("email").eq(email),
    )
    items = result.get("Items", [])
    return items[0] if items else None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/register", status_code=201)
async def register(body: RegisterRequest):
    if await _find_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = "usr_" + uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()

    await asyncio.to_thread(
        _table.put_item,
        Item={
            "user_id": user_id,
            "email": body.email,
            "password_hash": _hash_password(body.password),
            "role": body.role,
            "status": "ACTIVE",
            "created_at": now,
            "last_login": None,
        },
    )

    log.info("user_registered", user_id=user_id, email=body.email, role=body.role)
    return {"user_id": user_id, "message": "Registration successful"}


@app.post("/login")
async def login(body: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(ip)

    user = await _find_user_by_email(body.email)
    if not user or not _verify_password(body.password, user["password_hash"]):
        LOGIN_ATTEMPTS.labels(result="failure").inc()
        log.warning("login_failed", email=body.email, ip=ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token, token_id = _encode_jwt(user["user_id"], user["email"], user["role"])
    session_key = f"session:{user['user_id']}:{token_id}"
    await _redis.setex(session_key, JWT_EXPIRY_MINUTES * 60, "active")

    now = datetime.utcnow().isoformat()
    await asyncio.to_thread(
        _table.update_item,
        Key={"user_id": user["user_id"]},
        UpdateExpression="SET last_login = :ts",
        ExpressionAttributeValues={":ts": now},
    )

    LOGIN_ATTEMPTS.labels(result="success").inc()
    log.info("login_success", user_id=user["user_id"], ip=ip)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user["user_id"],
        "role": user["role"],
        "email": user["email"],
    }


@app.post("/validate")
async def validate_token(token: str = Query(...)):
    payload = _decode_jwt(token)
    session_key = f"session:{payload['user_id']}:{payload['token_id']}"
    if not await _redis.exists(session_key):
        raise HTTPException(status_code=401, detail="Session expired or logged out")
    return {
        "valid": True,
        "user_id": payload["user_id"],
        "email": payload["email"],
        "role": payload["role"],
    }


@app.post("/logout")
async def logout(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    payload = _decode_jwt(authorization.removeprefix("Bearer "))
    session_key = f"session:{payload['user_id']}:{payload['token_id']}"
    await _redis.delete(session_key)
    log.info("logout", user_id=payload["user_id"])
    return {"message": "Logged out successfully"}


@app.get("/health")
async def health():
    now = datetime.utcnow().isoformat()

    try:
        await asyncio.to_thread(_table.meta.client.list_tables)
        db_status = "healthy"
    except Exception as exc:
        log.error("health_dynamodb_failed", error=str(exc))
        db_status = "unhealthy"

    try:
        await _redis.ping()
        redis_status = "healthy"
    except Exception as exc:
        log.error("health_redis_failed", error=str(exc))
        redis_status = "unhealthy"

    overall = "healthy" if db_status == "healthy" and redis_status == "healthy" else "unhealthy"
    return JSONResponse(
        content={"status": overall, "dynamodb": db_status, "redis": redis_status, "timestamp": now},
        status_code=200 if overall == "healthy" else 503,
    )


@app.get("/metrics")
async def metrics():
    return Response(
        content=prometheus_client.generate_latest(),
        media_type=prometheus_client.CONTENT_TYPE_LATEST,
    )
