"""One persistent cooldown shared by every Spotify request for the same app."""

import hashlib
import json
import logging
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import perf_counter

import anyio
import httpx
from sqlalchemy import select

from backend.core.config import settings
from backend.core.exceptions import SpotifyRateLimitedException
from backend.db.models.spotify_api_cooldown import SpotifyAPICooldown

logger = logging.getLogger("uvicorn.error.spotify")
_request_source = ContextVar("spotify_request_source", default="interactive")


@asynccontextmanager
async def spotify_request_source(source: str):
    """Attribute requests to their async task without changing playback behavior."""
    token = _request_source.set(source)
    try:
        yield
    finally:
        _request_source.reset(token)


def _log_request(response: httpx.Response, client_id, duration_ms, deadline=None):
    """Record request counts and quota reasons without URLs, tokens or bodies."""
    if not isinstance(response, httpx.Response):
        return
    try:
        request = response.request
        path = request.url.path
        if request.url.host == "accounts.spotify.com" and path == "/api/token":
            endpoint = path
        elif request.url.host == "api.spotify.com" and path in {
            "/v1/search",
            "/v1/me/player",
            "/v1/me/player/devices",
            "/v1/me/player/play",
            "/v1/me/player/pause",
            "/v1/me/player/next",
        }:
            endpoint = path
        elif request.url.host == "api.spotify.com" and path.startswith("/v1/tracks/"):
            endpoint = "/v1/tracks/{id}"
        else:
            endpoint = "other"
        method = request.method if request.method in {"GET", "POST", "PUT"} else "other"
    except RuntimeError:  # Detached test responses did not make an HTTP request.
        return
    reason = "unspecified"
    if response.status_code == 429:
        try:
            error = response.json().get("error")
        except ValueError, AttributeError:
            error = None
        if isinstance(error, dict) and error.get("reason") in (
            "QUOTA_EXCEEDED",
            "RATE_LIMITED",
        ):
            reason = error["reason"]
    event = {
        "event": "spotify_request",
        "at": datetime.now(timezone.utc).isoformat(),
        "app_scope": _scope(client_id)[:12],
        "source": (
            _request_source.get()
            if _request_source.get()
            in ("poll", "confirm", "search", "metadata", "interactive")
            else "other"
        ),
        "endpoint": endpoint,
        "method": method,
        "status": response.status_code,
        "reason": reason,
        "retry_at": deadline.isoformat() if deadline is not None else None,
        "retry_after_seconds": (
            max(1, math.ceil((deadline - datetime.now(timezone.utc)).total_seconds()))
            if deadline is not None
            else None
        ),
        "duration_ms": round(duration_ms, 1),
    }
    logger.log(
        logging.WARNING if response.status_code == 429 else logging.INFO,
        "spotify_request %s",
        json.dumps(event, sort_keys=True),
    )


def _get_engine():
    from backend.db.database import engine

    return engine


def _scope(client_id: str | None) -> str:
    app = (
        client_id
        if isinstance(client_id, str) and client_id
        else settings.SPOTIFY_CLIENT_ID
    )
    return hashlib.sha256((app or "default-spotify-app").encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def get_deadline(client_id: str | None = None) -> datetime | None:
    """Read a live cooldown without making a Spotify call or changing its expiry."""
    table = SpotifyAPICooldown.__table__
    with _get_engine().connect() as connection:
        deadline = connection.execute(
            select(table.c.retry_at).where(table.c.scope == _scope(client_id))
        ).scalar_one_or_none()
    if deadline is not None and _utc(deadline) > datetime.now(timezone.utc):
        return _utc(deadline)
    return None


def assert_ready(client_id: str | None = None) -> None:
    """Reject attempts locally while Spotify's retry deadline remains in force."""
    deadline = get_deadline(client_id)
    if deadline is not None:
        raise SpotifyRateLimitedException(deadline)


def record_rate_limit(client_id: str | None, retry_after: str | None) -> datetime:
    """Persist Retry-After, retaining a longer deadline reported concurrently."""
    now = datetime.now(timezone.utc)
    try:
        seconds = float(retry_after)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError
        deadline = now + timedelta(seconds=max(1, seconds))
    except TypeError, ValueError, OverflowError:
        try:
            deadline = max(
                now + timedelta(seconds=1), _utc(parsedate_to_datetime(retry_after))
            )
        except TypeError, ValueError, OverflowError:
            deadline = now + timedelta(seconds=5)
    engine = _get_engine()
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    table = SpotifyAPICooldown.__table__
    statement = insert(table).values(scope=_scope(client_id), retry_at=deadline)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.scope],
        set_={"retry_at": statement.excluded.retry_at},
        where=table.c.retry_at < statement.excluded.retry_at,
    )
    with engine.begin() as connection:
        connection.execute(statement)
    return get_deadline(client_id) or deadline


async def spotify_request(send, *args, client_id: str | None = None, **kwargs):
    """Guard a network operation and retain 429 before any caller handles errors."""
    await anyio.to_thread.run_sync(assert_ready, client_id)
    started = perf_counter()
    response = await send(*args, **kwargs)
    duration_ms = (perf_counter() - started) * 1000
    deadline = None
    if response.status_code == 429:
        deadline = await anyio.to_thread.run_sync(
            record_rate_limit, client_id, response.headers.get("Retry-After")
        )
    _log_request(response, client_id, duration_ms, deadline)
    if deadline is not None:
        raise SpotifyRateLimitedException(deadline)
    return response
