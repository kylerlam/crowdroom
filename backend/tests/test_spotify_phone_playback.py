"""A selected phone must receive the intended track and confirm actual playback."""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest

from backend.adapters.spotify_playback_adapter import SpotifyPlaybackAdapter
from backend.core.exceptions import SpotifyUpstreamException


def phone(*, active=True, device_id="iphone"):
    """Return a Spotify Connect phone fixture."""
    return {
        "id": device_id,
        "name": "iPhone",
        "type": "smartphone",
        "is_active": active,
        "is_restricted": False,
    }


def playback(*, track="queued-track", device_id="iphone", playing=True):
    """Build a real Player API response, including the target device."""
    return {
        "is_playing": playing,
        "progress_ms": 45000,
        "device": {"id": device_id},
        "item": {"id": track, "duration_ms": 180000},
    }


@contextmanager
def spotify_http(handler):
    """Keep actual request construction while replacing Spotify with MockTransport."""
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    with (
        patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            side_effect=lambda: client_type(transport=transport),
        ),
        patch(
            "backend.adapters.spotify_playback_adapter.asyncio.sleep", new=AsyncMock()
        ),
    ):
        yield


def test_inactive_phone_receives_uri_without_a_racing_transfer():
    """Initial URI Play targets an available inactive phone without another transfer."""
    requests = []
    counts = {"devices": 0, "state": 0}

    def handle(request):
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/devices"):
            counts["devices"] += 1
            return httpx.Response(200, json={"devices": [phone(active=False)]})
        if request.method == "GET":
            counts["state"] += 1
            if counts["state"] < 3:
                return httpx.Response(204)
            return httpx.Response(200, json=playback())
        return httpx.Response(204)

    async def run():
        with spotify_http(handle):
            return await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone", position_ms=45000
            )

    assert anyio.run(run) == "iphone"
    transfers = [
        request
        for request in requests
        if request.method == "PUT" and request.url.path == "/v1/me/player"
    ]
    commands = [request for request in requests if request.url.path.endswith("/play")]
    assert transfers == []
    assert len(commands) == 1
    assert commands[0].url.params["device_id"] == "iphone"
    assert json.loads(commands[0].content) == {
        "uris": ["spotify:track:queued-track"],
        "position_ms": 45000,
    }
    assert counts["state"] == 3
    assert counts["devices"] == 1


@pytest.mark.parametrize(
    "state",
    [
        playback(playing=False),
        playback(track="old-track"),
        playback(device_id="browser"),
        None,
    ],
)
def test_play_204_does_not_report_success_or_repeat_the_command_without_confirmation(
    state,
):
    """Paused playback, the wrong track/device, or no player is not success."""
    commands = []
    state_reads = []

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/devices"):
            return httpx.Response(200, json={"devices": [phone()]})
        if request.method == "GET":
            state_reads.append(request)
            return (
                httpx.Response(204)
                if state is None
                else httpx.Response(200, json=state)
            )
        commands.append(request)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle):
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone"
            )

    with pytest.raises(SpotifyUpstreamException, match="has not started") as error:
        anyio.run(run)
    assert str(error.value) == (
        "Spotify has not started the requested song on the selected device. "
        "Open Spotify there and play a song once, then return here and press Resume."
    )
    assert len(commands) == 1
    assert len(state_reads) == 5


def test_unavailable_selected_phone_is_not_replaced_by_another_device():
    """Room managers cannot silently move the host's chosen output elsewhere."""
    commands = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"devices": [phone(device_id="browser")]})
        commands.append(request)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle):
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone"
            )

    with pytest.raises(SpotifyUpstreamException, match="unavailable"):
        anyio.run(run)
    assert commands == []


def test_explicit_device_selection_rejects_failed_transfer():
    """An actual transfer error is not confused with an available inactive phone."""
    commands = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"devices": [phone(active=False)]})
        commands.append(request)
        return httpx.Response(503)

    async def run():
        with spotify_http(handle):
            await SpotifyPlaybackAdapter("token").prepare_device("iphone")

    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(run)
    assert len(commands) == 1
    assert commands[0].url.path == "/v1/me/player"


def test_explicit_selection_still_requests_transfer_without_claiming_playback():
    """Selecting an available inactive phone may transfer while keeping pause."""
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"devices": [phone(active=False)]})
        writes.append(request)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle):
            return await SpotifyPlaybackAdapter("token").prepare_device("iphone")

    assert anyio.run(run) == "iphone"
    assert len(writes) == 1
    assert writes[0].url.path == "/v1/me/player"
    assert json.loads(writes[0].content) == {"device_ids": ["iphone"], "play": False}


def test_only_explicit_404_recovery_transfers_before_one_retry():
    """A rejected Play may re-activate its exact target once without losing position."""
    writes = []
    play_count = 0

    def handle(request):
        nonlocal play_count
        if request.method == "GET" and request.url.path.endswith("/devices"):
            return httpx.Response(200, json={"devices": [phone(active=False)]})
        if request.method == "GET":
            return httpx.Response(200, json=playback())
        writes.append(request)
        if request.url.path.endswith("/play"):
            play_count += 1
            return httpx.Response(404 if play_count == 1 else 204)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle):
            return await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone", position_ms=45000
            )

    assert anyio.run(run) == "iphone"
    assert [request.url.path for request in writes] == [
        "/v1/me/player/play",
        "/v1/me/player",
        "/v1/me/player/play",
    ]
    for command in (writes[0], writes[2]):
        assert command.url.params["device_id"] == "iphone"
        assert json.loads(command.content) == {
            "uris": ["spotify:track:queued-track"],
            "position_ms": 45000,
        }


def test_rejected_confirmation_read_never_resends_an_accepted_play():
    """A token error while verifying must not reset a song by repeating Play."""
    commands = []

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/devices"):
            return httpx.Response(200, json={"devices": [phone()]})
        if request.method == "GET":
            return httpx.Response(401)
        commands.append(request)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle):
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone"
            )

    with pytest.raises(SpotifyUpstreamException, match="could not be verified"):
        anyio.run(run)
    assert len(commands) == 1
