"""Guest nicknames never grant another identity or reset persistent request limits."""

# ruff: noqa: F811

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlmodel import Session

import backend.api.auth.router as auth_routes
import backend.core.rate_limit as limits
from backend.api.users.router import router as users_router
from backend.core.exceptions import ForbiddenException, TooManyRequestsException
from backend.db.database import get_session
from backend.db.models import User
from backend.tests.test_public_user_ids import identity_engine  # noqa: F401


@pytest.fixture
def guest_api(identity_engine, monkeypatch):
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")

    def session():
        with Session(identity_engine) as db:
            yield db

    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(auth_routes.settings, "AUTH_MODE", "ONLINE")
    monkeypatch.setattr(limits, "request_limiter", limits.RequestRateLimiter())

    @app.exception_handler(ForbiddenException)
    async def forbidden(_request, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(TooManyRequestsException)
    async def limited(_request, exc):
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    with TestClient(app) as client:
        yield app, client, identity_engine


def test_duplicate_nickname_never_authenticates_registered_or_other_guest(guest_api):
    app, first, _ = guest_api
    assert (
        first.post(
            "/api/v1/auth/register",
            json={
                "username": "Alice",
                "email": "alice@example.com",
                "password": "secret123",
            },
        ).status_code
        == 201
    )
    owner = first.get("/api/v1/auth/me").json()
    assert (
        first.post("/api/v1/auth/guest", json={"username": "Alice"}).status_code == 200
    )
    guest = first.get("/api/v1/auth/me").json()
    assert guest["is_guest"] and guest["id"] != owner["id"]
    assert guest["username"] == "Alice"
    with TestClient(app) as second:
        assert (
            second.post("/api/v1/auth/guest", json={"username": "Alice"}).status_code
            == 200
        )
        other = second.get("/api/v1/auth/me").json()
        assert other["id"] not in {owner["id"], guest["id"]}
        assert other["username"] == "Alice"


def test_logout_reentry_rename_and_refresh_keep_identity_and_deadlines(guest_api):
    _, client, engine = guest_api
    assert (
        client.post("/api/v1/auth/guest", json={"username": "Initial"}).status_code
        == 200
    )
    original = client.get("/api/v1/auth/me").json()
    from uuid import UUID

    joined = datetime.now(timezone.utc) - timedelta(minutes=10)
    last_request = datetime.now(timezone.utc) - timedelta(minutes=3)
    with Session(engine) as db:
        guest = db.get(User, UUID(original["id"]))
        guest.guest_first_joined_at = joined
        guest.guest_last_requested_at = last_request
        db.add(guest)
        db.commit()
    deadline = client.get("/api/v1/auth/me").json()["guest_request_available_at"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert auth_routes.GUEST_IDENTITY_COOKIE_NAME in client.cookies
    assert (
        client.post("/api/v1/auth/guest", json={"username": "Renamed"}).status_code
        == 200
    )
    restored = client.get("/api/v1/auth/me").json()
    assert restored["id"] == original["id"]
    assert restored["public_id"] == original["public_id"]
    assert restored["username"] == "Renamed"
    assert restored["guest_request_available_at"] == deadline
    changed = client.patch(
        f"/api/v1/users/{original['id']}", json={"username": "Profile rename"}
    )
    assert changed.status_code == 200
    assert changed.json()["username"] == "Profile rename"
    assert changed.json()["guest_request_available_at"] == deadline


def test_identity_cookie_is_not_an_access_token_or_an_account_upgrade(guest_api):
    _, client, _ = guest_api
    response = client.post("/api/v1/auth/guest", json={"username": "Guest"})
    assert response.status_code == 200
    me = client.get("/api/v1/auth/me").json()
    for cookie in response.headers.get_list("set-cookie"):
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    for endpoint, body in (
        ("set-password", {"password": "secret123"}),
        ("complete-profile", {"email": "guest@example.com", "password": "secret123"}),
        ("spotify/start", {}),
    ):
        assert client.post(f"/api/v1/auth/{endpoint}", json=body).status_code == 403
    assert (
        client.patch(
            f"/api/v1/users/{me['id']}", json={"email": "guest@example.com"}
        ).status_code
        == 403
    )
    identity = client.cookies[auth_routes.GUEST_IDENTITY_COOKIE_NAME]
    client.cookies.clear()
    client.cookies.set(auth_routes.settings.AUTH_COOKIE_NAME, identity)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_guest_source_limit_applies_to_creation_not_session_reuse(guest_api):
    app, first, _ = guest_api
    assert (
        first.post("/api/v1/auth/guest", json={"username": "Guest"}).status_code == 200
    )
    original = first.get("/api/v1/auth/me").json()["id"]
    for _ in range(15):
        assert (
            first.post("/api/v1/auth/guest", json={"username": "Guest"}).status_code
            == 200
        )
    assert first.get("/api/v1/auth/me").json()["id"] == original
    with TestClient(app) as other:
        for _ in range(9):
            other.cookies.clear()
            assert (
                other.post("/api/v1/auth/guest", json={"username": "Guest"}).status_code
                == 200
            )
        other.cookies.clear()
        assert (
            other.post("/api/v1/auth/guest", json={"username": "Guest"}).status_code
            == 429
        )


@pytest.mark.parametrize("nickname", ["   ", "line\nbreak", "control\x00"])
def test_blank_or_control_character_nickname_is_rejected(guest_api, nickname):
    _, client, _ = guest_api
    assert (
        client.post("/api/v1/auth/guest", json={"username": nickname}).status_code
        == 422
    )


def test_local_passwordless_entry_cannot_claim_legacy_or_explicit_guest(
    guest_api, monkeypatch
):
    _, client, engine = guest_api
    monkeypatch.setattr(auth_routes.settings, "AUTH_MODE", "LOCAL")
    with Session(engine) as db:
        legacy = User(username="legacy-no-password")
        db.add(legacy)
        db.commit()
        legacy_id = str(legacy.id)
    assert (
        client.post(
            "/api/v1/auth/local-login", json={"username": "legacy-no-password"}
        ).status_code
        == 200
    )
    guest = client.get("/api/v1/auth/me").json()
    assert guest["is_guest"] and guest["id"] != legacy_id
    client.post("/api/v1/auth/logout")
    assert (
        client.post(
            "/api/v1/auth/local-login", json={"username": "Renamed"}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me").json()["id"] == guest["id"]
    assert (
        client.post(
            "/api/v1/auth/local-login",
            json={"username": "legacy-no-password", "password": "invented-password"},
        ).status_code
        == 401
    )


def test_local_registered_username_still_requires_password(guest_api, monkeypatch):
    _, client, _ = guest_api
    assert (
        client.post(
            "/api/v1/auth/register",
            json={
                "username": "Protected",
                "email": "protected@example.com",
                "password": "secret123",
            },
        ).status_code
        == 201
    )
    monkeypatch.setattr(auth_routes.settings, "AUTH_MODE", "LOCAL")
    assert (
        client.post("/api/v1/auth/local-login", json={"username": "   "}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/auth/local-login", json={"username": "Protected"}
        ).status_code
        == 401
    )
