"""A persisted Spotify cooldown is shared by all request paths for an app."""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import create_engine

from backend.adapters.spotify_playback_adapter import SpotifyPlaybackAdapter
from backend.adapters.spotify_search_adapter import SpotifySearchAdapter
from backend.adapters.spotify_utils import request_token
from backend.api.auth.dependencies import get_current_user
from backend.api.playback.dependencies import get_playback_control_service
from backend.core import spotify_rate_limit
from backend.core.encryption import encrypt_data
from backend.core.exceptions import SpotifyRateLimitedException
from backend.db.models.enum import StreamingPlatforms
from backend.db.models.platform_connection import PlatformConnection
from backend.db.models.spotify_api_cooldown import SpotifyAPICooldown
from backend.db.models.user import User
from backend.main import app
from backend.repositories.platform_connection_repo import PlatformConnectionRepo
from backend.services.platform_connection_service import PlatformConnectionService
from backend.services.spotify_oauth_service import SpotifyOAuthService


@pytest.fixture(params=["sqlite", "postgresql"])
def cooldown_store(request, tmp_path, monkeypatch):
    """Use separate connections to exercise real storage and concurrent updates."""
    base = None
    schema = None
    if request.param == "sqlite":
        engine = create_engine(
            f"sqlite:///{tmp_path / 'spotify-cooldown.db'}",
            connect_args={"timeout": 20, "check_same_thread": False},
        )
    else:
        url = os.environ.get("CROWDROOM_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("CROWDROOM_TEST_POSTGRES_URL not set")
        schema = f"cooldown_{uuid4().hex}"
        base = create_engine(url)
        with base.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    SpotifyAPICooldown.__table__.create(engine)
    monkeypatch.setattr(spotify_rate_limit, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()
    if base is not None:
        with base.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.dispose()


def test_cooldown_scope_persists_across_new_checks_without_storing_client_id(
    cooldown_store,
):
    before = datetime.now(timezone.utc)
    deadline = spotify_rate_limit.record_rate_limit("test-client-a", "30")
    assert (
        before + timedelta(seconds=29)
        <= deadline
        <= datetime.now(timezone.utc) + timedelta(seconds=31)
    )
    assert spotify_rate_limit.get_deadline("test-client-a") == deadline
    assert spotify_rate_limit.get_deadline("test-client-b") is None
    with pytest.raises(SpotifyRateLimitedException):
        spotify_rate_limit.assert_ready("test-client-a")
    with cooldown_store.connect() as connection:
        rows = connection.execute(select(SpotifyAPICooldown.__table__)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["scope"] != "test-client-a"
        assert len(rows[0]["scope"]) == 64
    # All state lives in storage rather than an adapter instance or process cache.
    assert spotify_rate_limit.get_deadline("test-client-a") == deadline


@pytest.mark.parametrize("header", [None, "", "not-a-duration", "-30", "NaN", "inf"])
def test_malformed_retry_after_has_a_finite_safe_wait(cooldown_store, header):
    before = datetime.now(timezone.utc)
    deadline = spotify_rate_limit.record_rate_limit("malformed-client", header)
    assert before < deadline <= datetime.now(timezone.utc) + timedelta(minutes=5)
    assert deadline.tzinfo is not None


def test_http_date_retry_after_and_existing_longer_wait_are_preserved(cooldown_store):
    expected = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=2)
    deadline = spotify_rate_limit.record_rate_limit(
        "date-client", format_datetime(expected, usegmt=True)
    )
    assert deadline == expected
    assert spotify_rate_limit.record_rate_limit("date-client", "1") == expected
    assert spotify_rate_limit.get_deadline("date-client") == expected


def test_expired_deadline_allows_requests_again(cooldown_store):
    spotify_rate_limit.record_rate_limit("expired-client", "30")
    with cooldown_store.begin() as connection:
        connection.execute(
            SpotifyAPICooldown.__table__.update().values(
                retry_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
    assert spotify_rate_limit.get_deadline("expired-client") is None
    spotify_rate_limit.assert_ready("expired-client")


def test_reopening_storage_keeps_the_same_cooldown(cooldown_store, monkeypatch):
    """Replacing the engine, as after a restart, cannot erase an active wait."""
    deadline = spotify_rate_limit.record_rate_limit("persistent-client", "60")
    url = cooldown_store.url
    if cooldown_store.dialect.name == "postgresql":
        with cooldown_store.connect() as connection:
            schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    cooldown_store.dispose()
    reopened = create_engine(url)
    monkeypatch.setattr(spotify_rate_limit, "_get_engine", lambda: reopened)
    try:
        assert spotify_rate_limit.get_deadline("persistent-client") == deadline
        with pytest.raises(SpotifyRateLimitedException):
            spotify_rate_limit.assert_ready("persistent-client")
    finally:
        reopened.dispose()


def test_parallel_waits_cannot_shorten_the_latest_deadline(cooldown_store):
    barrier = threading.Barrier(4)
    results, errors = [], []

    def record(seconds):
        try:
            barrier.wait(timeout=10)
            results.append(
                spotify_rate_limit.record_rate_limit("shared-client", str(seconds))
            )
        except Exception as error:
            errors.append(error)

    threads = [
        threading.Thread(target=record, args=(seconds,)) for seconds in (1, 15, 90, 2)
    ]
    before = datetime.now(timezone.utc)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert not errors
    assert len(results) == 4
    assert spotify_rate_limit.get_deadline("shared-client") >= before + timedelta(
        seconds=89
    )


def test_http_429_response_records_wait_and_blocks_another_request_path(cooldown_store):
    response = httpx.Response(
        429,
        headers={"Retry-After": "90"},
        request=httpx.Request("GET", "https://api.spotify.com/v1/me/player"),
    )
    first = AsyncMock(return_value=response)
    other = AsyncMock(return_value=httpx.Response(204))

    async def run():
        with pytest.raises(SpotifyRateLimitedException):
            await spotify_rate_limit.spotify_request(first, client_id="http-client")
        with pytest.raises(SpotifyRateLimitedException):
            await spotify_rate_limit.spotify_request(other, client_id="http-client")
        other.assert_not_awaited()
        allowed = await spotify_rate_limit.spotify_request(
            other, client_id="another-app"
        )
        assert allowed.status_code == 204

    anyio.run(run)
    first.assert_awaited_once()
    other.assert_awaited_once()


def test_all_adapter_and_token_paths_stop_before_network_during_shared_cooldown(
    isolated_spotify_cooldown, monkeypatch
):
    """Fresh adapters, cached search tokens and refreshes all share the app wait."""
    spotify_rate_limit.record_rate_limit("blocked-app", "60")
    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    playback = SpotifyPlaybackAdapter("test-access-token", client_id="blocked-app")
    search = SpotifySearchAdapter({"client_id": "blocked-app", "client_secret": "test"})
    search._token = "already-cached-token"
    search._token_expiry = time.time() + 600
    repository = MagicMock(spec=PlatformConnectionRepo)
    service = PlatformConnectionService(repository)
    connection = PlatformConnection(
        user_id=uuid4(),
        platform=StreamingPlatforms.SPOTIFY,
        credentials_encrypted=encrypt_data(
            {"client_id": "blocked-app", "client_secret": "test"}
        ),
        refresh_token_encrypted=encrypt_data(
            {"refresh_token": "keep-existing-refresh-token"}
        ),
    )
    original_refresh = connection.refresh_token_encrypted

    async def run():
        operations = [
            playback.get_devices,
            SpotifyPlaybackAdapter(
                "another-user-token", client_id="blocked-app"
            ).get_devices,
            playback.get_current_playback,
            lambda: playback.play("spotify:track:test", "phone"),
            lambda: playback.pause("phone"),
            playback.skip,
            lambda: search.search("song"),
            lambda: request_token(
                {"client_id": "blocked-app", "client_secret": "test"}
            ),
            lambda: SpotifyOAuthService(service)._request_tokens(
                "code", "verifier", "blocked-app", "test"
            ),
            lambda: service._refresh_access_token(connection),
        ]
        for operation in operations:
            with pytest.raises(SpotifyRateLimitedException):
                await operation()

    anyio.run(run)
    for method in (client.get, client.post, client.put):
        method.assert_not_awaited()
    assert connection.refresh_token_encrypted == original_refresh


@pytest.mark.parametrize(
    "path,method,payload",
    [
        ("/playback/play", "post", {}),
        ("/playback/pause", "post", None),
        ("/playback/skip", "post", None),
        ("/playback/devices", "get", None),
    ],
)
def test_control_routes_keep_rate_limit_code_and_retry_headers(path, method, payload):
    actor = User(username="host", room_id=uuid4())
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    service = AsyncMock()
    error = SpotifyRateLimitedException(deadline)
    service.play.side_effect = error
    service.pause.side_effect = error
    service.skip.side_effect = error
    service.get_devices.side_effect = error
    before = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_playback_control_service] = lambda: service
    try:
        response = (
            getattr(TestClient(app), method)(f"/api/v1{path}", json=payload)
            if method == "post"
            else TestClient(app).get(f"/api/v1{path}")
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(before)
    assert response.status_code == 429
    data = response.json()
    assert data["code"] == "SPOTIFY_RATE_LIMITED"
    assert data["retry_at"]
    assert 0 < data["retry_after"] <= 60
    assert int(response.headers["Retry-After"]) > 0
