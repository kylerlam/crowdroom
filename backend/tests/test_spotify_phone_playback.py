"""A selected phone must receive the intended track and confirm actual playback."""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest

from backend.adapters.spotify_playback_adapter import SpotifyPlaybackAdapter
from backend.core.exceptions import SpotifyUpstreamException


def phone(*, active=True, device_id="iphone", device_type="smartphone"):
    """Return a Spotify Connect phone fixture."""
    return {
        "id": device_id,
        "name": "iPhone",
        "type": device_type,
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
def spotify_http(handler, *, track_status=200, track_data=None):
    """Keep actual request construction while replacing Spotify with MockTransport."""
    client_type = httpx.AsyncClient
    requests = []

    def dispatch(request):
        requests.append(request)
        if request.method == "GET" and request.url.path.startswith("/v1/tracks/"):
            data = (
                track_data
                if track_data is not None
                else {
                    "uri": "spotify:track:" + request.url.path.rsplit("/", 1)[-1],
                    "album": {"uri": "spotify:album:album-id"},
                }
            )
            return httpx.Response(track_status, json=data)
        return handler(request)

    transport = httpx.MockTransport(dispatch)
    with (
        patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            side_effect=lambda: client_type(transport=transport),
        ),
        patch(
            "backend.adapters.spotify_playback_adapter.asyncio.sleep", new=AsyncMock()
        ),
    ):
        yield requests


def test_inactive_phone_receives_album_context_without_a_racing_transfer():
    """Phone playback uses its album offset directly, without trying a URI list."""
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
        "context_uri": "spotify:album:album-id",
        "offset": {"uri": "spotify:track:queued-track"},
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


@pytest.mark.parametrize("device_type", ["Smartphone", "Tablet"])
@pytest.mark.parametrize("position_ms", [None, 57000])
def test_mobile_play_uses_album_context_and_preserves_the_requested_offset(
    device_type, position_ms
):
    """Mobile targets receive context directly, never a first failing URI-list Play."""

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/devices"):
            return httpx.Response(
                200, json={"devices": [phone(device_type=device_type)]}
            )
        if request.method == "GET":
            return httpx.Response(200, json=playback())
        return httpx.Response(204)

    async def run():
        with spotify_http(handle) as requests:
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone", position_ms=position_ms
            )
            return requests

    requests = anyio.run(run)
    metadata = [
        request for request in requests if request.url.path.startswith("/v1/tracks/")
    ]
    commands = [request for request in requests if request.method == "PUT"]
    assert len(metadata) == len(commands) == 1
    assert metadata[0].url.path == "/v1/tracks/queued-track"
    assert json.loads(commands[0].content) == {
        "context_uri": "spotify:album:album-id",
        "offset": {"uri": "spotify:track:queued-track"},
        "position_ms": position_ms or 0,
    }
    assert commands[0].url.params["device_id"] == "iphone"


def test_desktop_keeps_uri_list_playback_without_loading_track_metadata():
    """The mobile context workaround does not add metadata calls on computers."""

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/devices"):
            return httpx.Response(
                200,
                json={"devices": [phone(device_id="desktop", device_type="Computer")]},
            )
        if request.method == "GET":
            return httpx.Response(200, json=playback(device_id="desktop"))
        return httpx.Response(204)

    async def run():
        with spotify_http(handle) as requests:
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "desktop"
            )
            return requests

    requests = anyio.run(run)
    assert not any(request.url.path.startswith("/v1/tracks/") for request in requests)
    commands = [request for request in requests if request.method == "PUT"]
    assert len(commands) == 1
    assert json.loads(commands[0].content) == {"uris": ["spotify:track:queued-track"]}


@pytest.mark.parametrize(
    "track_status,track_data",
    [
        (200, {}),
        (200, {"album": None}),
        (404, {}),
    ],
)
def test_missing_mobile_album_context_fails_clearly_before_any_play(
    track_status, track_data
):
    """An unavailable album is an actionable error, not a silent URI-list fallback."""
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"devices": [phone()]})
        writes.append(request)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle, track_status=track_status, track_data=track_data):
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone"
            )

    with pytest.raises(SpotifyUpstreamException, match="album playback context"):
        anyio.run(run)
    assert writes == []


def test_mobile_metadata_401_is_available_to_the_existing_token_refresh_path():
    """Token renewal can retry metadata before a Play command has been sent."""
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"devices": [phone()]})
        writes.append(request)
        return httpx.Response(204)

    async def run():
        with spotify_http(handle, track_status=401):
            await SpotifyPlaybackAdapter("token").play(
                "spotify:track:queued-track", "iphone"
            )

    with pytest.raises(httpx.HTTPStatusError) as error:
        anyio.run(run)
    assert error.value.response.status_code == 401
    assert writes == []


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
            "context_uri": "spotify:album:album-id",
            "offset": {"uri": "spotify:track:queued-track"},
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
