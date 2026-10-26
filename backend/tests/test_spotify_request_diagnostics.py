"""Attribute outbound Spotify requests without logging private request content."""

import asyncio
import json
import logging

import anyio
import httpx
import pytest

from backend.adapters.spotify_playback_adapter import SpotifyPlaybackAdapter
from backend.core.exceptions import SpotifyRateLimitedException
from backend.core.spotify_rate_limit import (
    get_deadline,
    spotify_request,
    spotify_request_source,
)


@pytest.fixture(autouse=True)
def capture_diagnostics(caplog, monkeypatch):
    """Isolate capture from logging configurations changed by migration tests."""
    logger = logging.getLogger("uvicorn.error.spotify")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(caplog.handler)
    yield
    logger.removeHandler(caplog.handler)


def events(caplog):
    return [
        json.loads(record.getMessage().split("spotify_request ", 1)[1])
        for record in caplog.records
        if record.name == "uvicorn.error.spotify"
        and record.getMessage().startswith("spotify_request ")
    ]


def test_diagnostics_redact_queries_credentials_track_ids_and_unknown_paths(caplog):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        ) as client:
            for url in (
                "https://api.spotify.com/v1/search?q=private-query",
                "https://api.spotify.com/v1/tracks/private-track-id",
                "https://accounts.spotify.com/api/token",
                "https://api.spotify.com/v1/private-user-id",
            ):
                await spotify_request(
                    client.post,
                    url,
                    client_id="private-client-id",
                    headers={"Authorization": "Bearer private-access-token"},
                    data={"client_secret": "private-client-secret"},
                )

    with caplog.at_level(logging.INFO, logger="uvicorn.error.spotify"):
        anyio.run(run)
    recorded = events(caplog)
    assert [event["endpoint"] for event in recorded] == [
        "/v1/search",
        "/v1/tracks/{id}",
        "/api/token",
        "other",
    ]
    assert all(event["method"] == "POST" for event in recorded)
    assert all(event["status"] == 200 for event in recorded)
    assert all(len(event["app_scope"]) == 12 for event in recorded)
    assert all(event["duration_ms"] >= 0 for event in recorded)
    assert "private-" not in caplog.text


@pytest.mark.parametrize(
    "body,expected_reason",
    [
        (
            {"error": {"reason": "QUOTA_EXCEEDED", "message": "private-message"}},
            "QUOTA_EXCEEDED",
        ),
        ({"error": {"reason": "private-secret"}}, "unspecified"),
        ({"error": {"reason": ["private-secret"]}}, "unspecified"),
        ({"error": "Too many requests"}, "unspecified"),
        (["private-message"], "unspecified"),
        (None, "unspecified"),
    ],
)
def test_429_reason_is_recorded_once_and_local_blocks_are_not_counted(
    caplog,
    body,
    expected_reason,
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            **({"json": body} if body is not None else {"text": "private-html"}),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            for _ in range(2):
                with pytest.raises(SpotifyRateLimitedException):
                    await spotify_request(
                        client.get,
                        "https://api.spotify.com/v1/search?q=private-query",
                        client_id="quota-client",
                    )

    with caplog.at_level(logging.INFO, logger="uvicorn.error.spotify"):
        anyio.run(run)
    assert len(calls) == 1
    recorded = events(caplog)
    assert len(recorded) == 1
    assert recorded[0]["reason"] == expected_reason
    assert recorded[0]["status"] == 429
    assert recorded[0]["retry_at"] == get_deadline("quota-client").isoformat()
    assert 1 <= recorded[0]["retry_after_seconds"] <= 60
    assert "private-" not in caplog.text


def test_sources_are_task_local_and_confirmation_restores_poll_source(
    caplog, monkeypatch
):
    original_client = httpx.AsyncClient

    def respond(request):
        return httpx.Response(
            200,
            json={
                "is_playing": True,
                "device": {"id": "phone"},
                "item": {"id": "track", "artists": [{"name": "Artist"}]},
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda: original_client(transport=httpx.MockTransport(respond)),
    )

    @spotify_request_source("poll")
    async def poll():
        adapter = SpotifyPlaybackAdapter("private-token")
        await adapter.get_current_playback()
        await adapter._confirm_playback("phone", "spotify:track:track")
        await adapter.get_current_playback()

    @spotify_request_source("search")
    async def search():
        async with httpx.AsyncClient() as client:
            await spotify_request(client.get, "https://api.spotify.com/v1/search")

    async def run():
        await asyncio.gather(poll(), search())
        await SpotifyPlaybackAdapter("private-token").get_current_playback()

    with caplog.at_level(logging.INFO, logger="uvicorn.error.spotify"):
        anyio.run(run)
    recorded = events(caplog)
    assert [
        event["source"] for event in recorded if event["endpoint"] != "/v1/search"
    ] == [
        "poll",
        "confirm",
        "poll",
        "interactive",
    ]
    assert [
        event["source"] for event in recorded if event["endpoint"] == "/v1/search"
    ] == [
        "search",
    ]
    assert "private-token" not in caplog.text
