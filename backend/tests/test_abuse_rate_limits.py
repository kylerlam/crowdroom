"""Signup and join abuse is bounded independently of room approval checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import Request, Response

import backend.core.rate_limit as limits
import backend.main as main
from backend.core.config import settings
from backend.core.exceptions import TooManyRequestsException
from backend.core.security import SecurityService
from backend.db.models.enum import TokenType


@pytest.fixture
def limiter(monkeypatch):
    """Isolate counters without disabling the real application's safeguards."""
    instance = limits.RequestRateLimiter()
    monkeypatch.setattr(limits, "request_limiter", instance)
    monkeypatch.setattr(main, "request_limiter", instance)
    return instance


def request(path, ip="192.0.2.1", token=None, forwarded=None):
    """Create an HTTP request with server-resolved IP and optional credentials."""
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "headers": headers,
            "client": (ip, 1234),
            "server": ("test", 80),
        }
    )


def call(request_obj):
    """Run the production middleware with an inert downstream handler."""

    async def run():
        return await main.limit_signup_and_room_entry(
            request_obj,
            AsyncMock(return_value=Response(status_code=204)),
        )

    return anyio.run(run)


def test_signup_limit_covers_both_creation_endpoints_and_ignores_spoofed_headers(
    limiter,
):
    """Changing creation URLs or an untrusted forwarding header cannot reset it."""
    for index in range(5):
        path = "/api/v1/auth/register" if index % 2 else "/api/v1/users/"
        assert call(request(path, forwarded=f"198.51.100.{index}")).status_code == 204
    result = call(request("/api/v1/auth/register", forwarded="198.51.100.99"))
    assert result.status_code == 429
    assert "retry-after" in result.headers
    assert call(request("/api/v1/auth/register", ip="192.0.2.2")).status_code == 204


def test_join_account_limit_survives_ip_and_invite_rotation(limiter):
    """One valid account cannot multiply its quota with new invite links or IPs."""
    token = SecurityService(settings).create_token(
        TokenType.ACCESS, {"sub": str(uuid4()), "ver": 0}
    )
    for index in range(12):
        assert (
            call(
                request(
                    f"/api/v1/rooms/invite/token-{index}/join",
                    ip=f"192.0.2.{index + 1}",
                    token=token,
                )
            ).status_code
            == 204
        )
    assert (
        call(
            request(f"/api/v1/rooms/{uuid4()}/join", ip="198.51.100.1", token=token)
        ).status_code
        == 429
    )


def test_join_source_limit_survives_invalid_or_rotated_tokens(limiter):
    """Invalid credentials never remove the independent source quota."""
    for index in range(120):
        assert (
            call(
                request(f"/api/v1/rooms/{uuid4()}/join", token=f"invalid-{index}")
            ).status_code
            == 204
        )
    assert call(request(f"/api/v1/rooms/{uuid4()}/join")).status_code == 429


def test_windows_expire_and_memory_is_bounded(limiter, monkeypatch):
    """A retry becomes possible after its window instead of permanently banning IPs."""
    clock = {"now": 100.0}
    monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    limiter.check("test", "one", 1, 60)
    clock["now"] = 159.0
    with pytest.raises(TooManyRequestsException):
        limiter.check("test", "one", 1, 60)
    clock["now"] = 160.0
    limiter.check("test", "one", 1, 60)
    for index in range(10001):
        limiter.check("test", str(index), 1, 60)
    assert len(limiter._windows) == 10000
