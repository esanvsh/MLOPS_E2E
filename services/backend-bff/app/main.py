"""PayShield AI — Backend-for-Frontend (BFF).

Proxies auth, transaction, and fraud-model requests from the React
frontend, adding any cross-cutting concerns (rate limiting, tracing).
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import prometheus_client
from dotenv import load_dotenv

load_dotenv()

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8002")
TRANSACTION_SERVICE_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://localhost:8003")

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger()

REQUEST_COUNT = prometheus_client.Counter(
    "bff_requests_total", "Total BFF requests", ["method", "path", "status"]
)
REQUEST_DURATION = prometheus_client.Histogram(
    "bff_request_duration_seconds", "BFF request duration", ["method", "path"]
)

_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    _http = httpx.AsyncClient(timeout=30.0)
    log.info("bff_ready", auth_url=AUTH_SERVICE_URL, transaction_url=TRANSACTION_SERVICE_URL)
    yield
    await _http.aclose()
    log.info("bff_shutdown")


app = FastAPI(title="PayShield BFF", version="1.0.0", lifespan=lifespan)

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
        method=request.method, path=request.url.path, status=str(response.status_code)
    ).inc()
    REQUEST_DURATION.labels(method=request.method, path=request.url.path).observe(elapsed)
    log.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(elapsed * 1000, 2),
    )
    return response


# ── Proxy helpers ─────────────────────────────────────────────────────────────

async def _forward(upstream_resp: httpx.Response) -> Response:
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type", "application/json"),
    )


def _auth_headers(request: Request) -> dict:
    headers = {}
    if auth := request.headers.get("authorization"):
        headers["authorization"] = auth
    return headers


async def _validate_token(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth.removeprefix("Bearer ")
    try:
        r = await _http.post(
            f"{AUTH_SERVICE_URL}/validate",
            params={"token": token},
            timeout=5.0,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        log.error("auth_validate_error", error=str(exc))
        raise HTTPException(status_code=503, detail="Auth service unavailable")


def _txn_headers(user: dict) -> dict:
    return {
        "X-User-ID": user["user_id"],
        "X-User-Role": user.get("role", "CUSTOMER"),
    }


# ── Auth routes  (/api/auth/* canonical, legacy bare paths kept for compat) ───

async def _register(request: Request) -> Response:
    resp = await _http.post(f"{AUTH_SERVICE_URL}/register", json=await request.json())
    return await _forward(resp)

async def _login(request: Request) -> Response:
    resp = await _http.post(f"{AUTH_SERVICE_URL}/login", json=await request.json())
    return await _forward(resp)

async def _validate(request: Request) -> Response:
    resp = await _http.post(
        f"{AUTH_SERVICE_URL}/validate",
        params={"token": request.query_params.get("token", "")},
        headers=_auth_headers(request),
    )
    return await _forward(resp)

async def _logout(request: Request) -> Response:
    resp = await _http.post(f"{AUTH_SERVICE_URL}/logout", headers=_auth_headers(request))
    return await _forward(resp)


app.add_api_route("/api/auth/register", _register, methods=["POST"], status_code=201)
app.add_api_route("/api/auth/login",    _login,    methods=["POST"])
app.add_api_route("/api/auth/validate", _validate, methods=["POST"])
app.add_api_route("/api/auth/logout",   _logout,   methods=["POST"])

# Legacy bare paths — kept so existing curl examples and frontend still work
app.add_api_route("/register", _register, methods=["POST"], status_code=201)
app.add_api_route("/login",    _login,    methods=["POST"])
app.add_api_route("/validate", _validate, methods=["POST"])
app.add_api_route("/logout",   _logout,   methods=["POST"])


# ── Transaction routes ────────────────────────────────────────────────────────

@app.post("/api/transactions", status_code=201)
async def create_transaction(request: Request):
    user = await _validate_token(request)
    resp = await _http.post(
        f"{TRANSACTION_SERVICE_URL}/transactions",
        json=await request.json(),
        headers=_txn_headers(user),
    )
    return await _forward(resp)


@app.get("/api/transactions/fraud")
async def get_fraud_transactions(request: Request):
    user = await _validate_token(request)
    resp = await _http.get(
        f"{TRANSACTION_SERVICE_URL}/transactions/fraud",
        headers=_txn_headers(user),
    )
    return await _forward(resp)


@app.get("/api/transactions/{transaction_id}")
async def get_transaction(transaction_id: str, request: Request):
    user = await _validate_token(request)
    resp = await _http.get(
        f"{TRANSACTION_SERVICE_URL}/transactions/{transaction_id}",
        headers=_txn_headers(user),
    )
    return await _forward(resp)


@app.get("/api/transactions")
async def get_transactions(request: Request):
    user = await _validate_token(request)
    resp = await _http.get(
        f"{TRANSACTION_SERVICE_URL}/transactions",
        headers=_txn_headers(user),
    )
    return await _forward(resp)


@app.patch("/api/transactions/{transaction_id}/prediction")
async def update_prediction(transaction_id: str, request: Request):
    user = await _validate_token(request)
    resp = await _http.patch(
        f"{TRANSACTION_SERVICE_URL}/transactions/{transaction_id}/prediction",
        json=await request.json(),
        headers=_txn_headers(user),
    )
    return await _forward(resp)


# ── Health / Metrics ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    checks: dict[str, str] = {}
    overall = "healthy"

    for name, url in [
        ("auth_service", f"{AUTH_SERVICE_URL}/health"),
        ("transaction_service", f"{TRANSACTION_SERVICE_URL}/health"),
    ]:
        try:
            r = await _http.get(url, timeout=5.0)
            checks[name] = "healthy" if r.status_code == 200 else "unhealthy"
        except Exception:
            checks[name] = "unhealthy"

    if any(v == "unhealthy" for v in checks.values()):
        overall = "degraded"

    return Response(
        content=__import__("json").dumps(
            {"status": overall, "dependencies": checks, "timestamp": datetime.utcnow().isoformat()}
        ),
        status_code=200 if overall == "healthy" else 207,
        media_type="application/json",
    )


@app.get("/metrics")
async def metrics():
    return Response(
        content=prometheus_client.generate_latest(),
        media_type=prometheus_client.CONTENT_TYPE_LATEST,
    )
