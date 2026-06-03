"""Tests for auth-service endpoints.

Run from services/auth-service/:
    pytest app/tests/ -v
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import boto3
import fakeredis.aioredis
import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from app.main import JWT_ALGORITHM, JWT_SECRET, app

pytestmark = pytest.mark.asyncio


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _seed_dynamodb_table() -> None:
    """Create payshield-users-dev inside the active mock_aws context."""
    boto3.resource(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    ).create_table(
        TableName="payshield-users-dev",
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "email-index",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest_asyncio.fixture
async def client():
    """ASGI test client with moto DynamoDB and fakeredis injected directly.

    httpx.ASGITransport does not trigger the ASGI lifespan, so _table and
    _redis stay None unless we patch them ourselves before requests are made.
    """
    with mock_aws():
        _seed_dynamodb_table()
        ddb = boto3.resource(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        moto_table = ddb.Table("payshield-users-dev")
        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        with (
            patch("app.main._table", moto_table),
            patch("app.main._redis", fake_redis),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                yield ac


# ── Request helpers ───────────────────────────────────────────────────────────

async def _register(
    ac: AsyncClient,
    email: str,
    password: str,
    role: str = "CUSTOMER",
):
    return await ac.post(
        "/register",
        json={"email": email, "password": password, "role": role},
    )


async def _login(ac: AsyncClient, email: str, password: str):
    return await ac.post("/login", json={"email": email, "password": password})


async def _token_for(ac: AsyncClient, email: str, password: str) -> str:
    resp = await _login(ac, email, password)
    return resp.json()["access_token"]


# ── POST /register ────────────────────────────────────────────────────────────

class TestRegister:
    async def test_new_user_returns_201_with_user_id(self, client):
        resp = await _register(client, "alice@test.com", "pass123")
        assert resp.status_code == 201
        body = resp.json()
        assert body["user_id"].startswith("usr_")
        assert len(body["user_id"]) == 16  # "usr_" + 12 hex chars
        assert body["message"] == "Registration successful"

    async def test_duplicate_email_returns_409(self, client):
        await _register(client, "bob@test.com", "pass")
        resp = await _register(client, "bob@test.com", "different-pass")
        assert resp.status_code == 409
        assert "already registered" in resp.json()["detail"]

    async def test_invalid_email_returns_422(self, client):
        resp = await _register(client, "not-an-email", "pass")
        assert resp.status_code == 422

    async def test_missing_fields_returns_422(self, client):
        resp = await client.post("/register", json={"email": "x@test.com"})
        assert resp.status_code == 422

    async def test_custom_role_is_stored(self, client):
        await _register(client, "carol@test.com", "pass", role="ANALYST")
        token = await _token_for(client, "carol@test.com", "pass")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["role"] == "ANALYST"

    async def test_default_role_is_customer(self, client):
        await client.post("/register", json={"email": "dave@test.com", "password": "pass"})
        token = await _token_for(client, "dave@test.com", "pass")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["role"] == "CUSTOMER"


# ── POST /login ───────────────────────────────────────────────────────────────

class TestLogin:
    async def test_success_returns_all_fields(self, client):
        await _register(client, "eve@test.com", "mypass", role="ADMIN")
        resp = await _login(client, "eve@test.com", "mypass")
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["email"] == "eve@test.com"
        assert body["role"] == "ADMIN"
        assert body["user_id"].startswith("usr_")
        assert "access_token" in body

    async def test_jwt_contains_required_claims(self, client):
        await _register(client, "frank@test.com", "pass")
        token = await _token_for(client, "frank@test.com", "pass")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        for field in ("user_id", "email", "role", "token_id", "exp"):
            assert field in payload
        assert payload["email"] == "frank@test.com"

    async def test_wrong_password_returns_401(self, client):
        await _register(client, "grace@test.com", "correct")
        resp = await _login(client, "grace@test.com", "wrong")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"

    async def test_unknown_email_returns_401(self, client):
        resp = await _login(client, "nobody@test.com", "pass")
        assert resp.status_code == 401

    async def test_rate_limit_blocks_after_five_attempts(self, client):
        """Attempts 1–5 pass the rate-limit gate; attempt 6+ returns 429."""
        statuses = []
        for _ in range(7):
            resp = await client.post(
                "/login",
                json={"email": "spam@test.com", "password": "bad"},
            )
            statuses.append(resp.status_code)
        # First 5 hit auth check (user not found → 401); 6th+ blocked by rate limit
        assert all(s == 401 for s in statuses[:5])
        assert all(s == 429 for s in statuses[5:])


# ── POST /validate ────────────────────────────────────────────────────────────

class TestValidate:
    async def test_valid_token_returns_user_info(self, client):
        await _register(client, "heidi@test.com", "pass", role="ANALYST")
        token = await _token_for(client, "heidi@test.com", "pass")
        resp = await client.post(f"/validate?token={token}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["email"] == "heidi@test.com"
        assert body["role"] == "ANALYST"
        assert body["user_id"].startswith("usr_")

    async def test_malformed_token_returns_401(self, client):
        resp = await client.post("/validate?token=this.is.garbage")
        assert resp.status_code == 401

    async def test_token_signed_with_wrong_secret_returns_401(self, client):
        payload = {
            "user_id": "usr_abc123",
            "email": "x@test.com",
            "role": "CUSTOMER",
            "token_id": uuid.uuid4().hex,
            "exp": 9_999_999_999,
        }
        bad_token = jwt.encode(payload, "wrong-secret", algorithm=JWT_ALGORITHM)
        resp = await client.post(f"/validate?token={bad_token}")
        assert resp.status_code == 401

    async def test_valid_jwt_without_redis_session_returns_401(self, client):
        """A well-signed token whose session was never stored is still rejected."""
        payload = {
            "user_id": "usr_ghost000000",
            "email": "ghost@test.com",
            "role": "CUSTOMER",
            "token_id": uuid.uuid4().hex,
            "exp": 9_999_999_999,
        }
        orphan = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        resp = await client.post(f"/validate?token={orphan}")
        assert resp.status_code == 401
        assert "Session" in resp.json()["detail"]


# ── POST /logout ──────────────────────────────────────────────────────────────

class TestLogout:
    async def test_logout_returns_success_message(self, client):
        await _register(client, "ivan@test.com", "pass")
        token = await _token_for(client, "ivan@test.com", "pass")
        resp = await client.post("/logout", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "Logged out successfully"

    async def test_token_is_invalid_after_logout(self, client):
        await _register(client, "judy@test.com", "pass")
        token = await _token_for(client, "judy@test.com", "pass")
        await client.post("/logout", headers={"Authorization": f"Bearer {token}"})
        resp = await client.post(f"/validate?token={token}")
        assert resp.status_code == 401

    async def test_missing_bearer_prefix_returns_401(self, client):
        await _register(client, "kim@test.com", "pass")
        token = await _token_for(client, "kim@test.com", "pass")
        resp = await client.post("/logout", headers={"Authorization": token})
        assert resp.status_code == 401

    async def test_concurrent_sessions_are_independent(self, client):
        """Logging out session A must not invalidate session B."""
        await _register(client, "leo@test.com", "pass")
        token_a = await _token_for(client, "leo@test.com", "pass")
        token_b = await _token_for(client, "leo@test.com", "pass")
        # Logout session A
        await client.post("/logout", headers={"Authorization": f"Bearer {token_a}"})
        # Session B should still be valid
        resp = await client.post(f"/validate?token={token_b}")
        assert resp.status_code == 200
        assert resp.json()["valid"] is True


# ── GET /health ───────────────────────────────────────────────────────────────

class TestHealth:
    async def test_all_deps_healthy(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["dynamodb"] == "healthy"
        assert body["redis"] == "healthy"
        assert "timestamp" in body


# ── GET /metrics ──────────────────────────────────────────────────────────────

class TestMetrics:
    async def test_returns_prometheus_text_format(self, client):
        await _register(client, "mia@test.com", "pass")
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "auth_requests_total" in resp.text
        assert "auth_request_duration_seconds" in resp.text
        assert "auth_login_attempts_total" in resp.text
