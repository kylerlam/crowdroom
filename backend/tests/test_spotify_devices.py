"""Spotify device recovery preserves authorization, queue order and permissions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from backend.adapters.spotify_playback_adapter import (
    SpotifyPlaybackAdapter,
    SpotifyPlaybackState,
)
from backend.api.auth.dependencies import get_current_user
from backend.api.playback.dependencies import get_playback_control_service
from backend.core.exceptions import (
    SpotifyConnectionRequiredException,
    SpotifyUpstreamException,
)
from backend.core.security import SecurityService
from backend.db.models import Room, User
from backend.db.models.enum import ItemStatus
from backend.db.models.session import Session
from backend.main import app
from backend.repositories.room_repo import RoomRepository
from backend.services.platform_connection_service import PlatformConnectionService
from backend.services.playback_control_service import PlaybackControlService
from backend.services.playback_poller_service import PlaybackPollerService
from backend.services.room_service import RoomService


def device(device_id, *, active=False, restricted=False):
    """Create a non-secret Spotify Connect device fixture."""
    return {
        "id": device_id,
        "name": "Browser",
        "type": "computer",
        "is_active": active,
        "is_restricted": restricted,
    }


def response(status):
    """Build a real HTTP response so failures exercise raise_for_status."""
    return httpx.Response(
        status,
        request=httpx.Request("PUT", "https://api.spotify.com/v1/me/player/play"),
    )


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("preferred,expected", [("chosen", "chosen"), (None, "active")])
def test_missing_device_rediscovery_preserves_command_and_retries_once(
    resume, preferred, expected
):
    """A 404 re-activates the same chosen device and preserves the command."""
    adapter = SpotifyPlaybackAdapter("token")
    adapter._resolve_device = AsyncMock(
        return_value={"id": expected, "type": "computer"}
    )
    adapter.prepare_device = AsyncMock(return_value=expected)
    adapter._confirm_playback = AsyncMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.put.side_effect = [response(404), response(204)]

    async def run():
        with patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            return_value=client,
        ):
            return await (
                adapter.resume(preferred)
                if resume
                else adapter.play("spotify:track:first", preferred)
            )

    assert anyio.run(run) == expected
    assert adapter.prepare_device.await_count == 1
    assert adapter.prepare_device.await_args.kwargs == {"force_transfer": True}
    assert client.put.await_count == 2
    retried = client.put.await_args_list[1].kwargs
    assert retried["params"] == {"device_id": expected}
    if resume:
        assert "json" not in retried
    else:
        assert retried["json"] == {"uris": ["spotify:track:first"]}


def test_inactive_available_device_can_receive_play_without_an_extra_transfer():
    """An explicit URI can activate an available player without racing a transfer."""
    adapter = SpotifyPlaybackAdapter("token")
    adapter.get_devices = AsyncMock(
        side_effect=[[device("browser")], [device("browser", active=True)]]
    )
    adapter.transfer = AsyncMock()
    adapter._confirm_playback = AsyncMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.put.return_value = response(204)

    async def run():
        with patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            return_value=client,
        ):
            return await adapter.play("spotify:track:first")

    assert anyio.run(run) == "browser"
    adapter.transfer.assert_not_awaited()


@pytest.mark.parametrize(
    "devices", [[], [device(None)], [device("blocked", restricted=True)]]
)
def test_offline_or_restricted_devices_do_not_retry_forever(devices):
    """A missing browser cannot be created or authorized by retrying play."""
    adapter = SpotifyPlaybackAdapter("token")
    adapter.get_devices = AsyncMock(return_value=devices)
    adapter.transfer = AsyncMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.put.return_value = response(404)

    async def run():
        with patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            return_value=client,
        ):
            await adapter.play("spotify:track:first")

    with pytest.raises(SpotifyUpstreamException, match="unavailable"):
        anyio.run(run)
    client.put.assert_not_awaited()
    adapter.transfer.assert_not_awaited()


def test_transfer_preserves_playing_or_paused_state():
    """Selecting a browser is not an implicit Resume command."""
    adapter = SpotifyPlaybackAdapter("token")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.put.return_value = response(204)

    async def run():
        with patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            return_value=client,
        ):
            await adapter.transfer("browser")

    anyio.run(run)
    assert client.put.await_args.kwargs["json"] == {
        "device_ids": ["browser"],
        "play": False,
    }


def test_device_404_retry_is_bounded_even_if_transfer_succeeds():
    """An unavailable web player cannot cause an unbounded retry loop."""
    adapter = SpotifyPlaybackAdapter("token")
    adapter._resolve_device = AsyncMock(
        return_value={"id": "browser", "type": "computer"}
    )
    adapter.prepare_device = AsyncMock(return_value="browser")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.put.return_value = response(404)

    async def run():
        with patch(
            "backend.adapters.spotify_playback_adapter.httpx.AsyncClient",
            return_value=client,
        ):
            await adapter.resume("browser")

    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(run)
    assert client.put.await_count == 2
    assert adapter.prepare_device.await_count == 1


@pytest.fixture
def manager_client():
    """Use real role checks and playback service with a mocked Spotify boundary."""
    room_id = uuid4()
    host = User(username="host", room_id=room_id)
    admin = User(username="admin", room_id=room_id)
    member = User(username="member", room_id=room_id)
    room = Room(
        id=room_id,
        room_name="Music",
        room_code="ABCDEF",
        host_user_id=host.id,
        settings={"admin_user_ids": [str(admin.id)]},
    )
    room.users = [host, admin, member]
    room_repo = MagicMock(spec=RoomRepository)
    room_repo.get_by_id.return_value = room
    rooms = RoomService(room_repo, MagicMock(spec=SecurityService))
    session = Session(
        room_id=room_id, current_device_id="saved", playback_status=ItemStatus.PAUSED
    )
    sessions = MagicMock()
    sessions.get_by_room.return_value = session
    connections = MagicMock(spec=PlatformConnectionService)
    connections.is_spotify_authorized.return_value = True
    connections.get_spotify_rate_limit_deadline.return_value = None
    connections.get_valid_access_token = AsyncMock(return_value="host-token")
    service = PlaybackControlService(
        rooms, sessions, MagicMock(), connections, MagicMock(), MagicMock()
    )
    adapter = AsyncMock()
    adapter.get_devices.return_value = [device("browser")]
    actor = {"user": host}
    app.dependency_overrides[get_current_user] = lambda: actor["user"]
    app.dependency_overrides[get_playback_control_service] = lambda: service
    with patch.object(service, "_get_adapter", new=AsyncMock(return_value=adapter)):
        yield TestClient(app), actor, host, admin, member, service, session, adapter
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_playback_control_service, None)


@pytest.mark.parametrize("role", ["host", "admin", "member"])
@pytest.mark.parametrize("operation", ["status", "devices", "select"])
def test_role_controls_host_status_access_and_owner_device_selection(
    manager_client, role, operation
):
    """Ordinary members cannot infer Spotify authorization through either API."""
    client, actor, host, admin, member, service, session, adapter = manager_client
    actor["user"] = {"host": host, "admin": admin, "member": member}[role]
    if operation == "select":
        result = client.put("/api/v1/playback/device", json={"device_id": "browser"})
    else:
        suffix = "spotify/status" if operation == "status" else "devices"
        result = client.get(f"/api/v1/playback/{suffix}")
    if role == "member" or (role == "admin" and operation in {"devices", "select"}):
        assert result.status_code == 403
        adapter.get_devices.assert_not_awaited()
        service._platform_connection_service.is_spotify_authorized.assert_not_called()
    else:
        assert result.status_code == (204 if operation == "select" else 200)
        if operation == "status":
            assert result.json() == {"authorized": True, "rate_limit_until": None}
            service._platform_connection_service.is_spotify_authorized.assert_called_once_with(
                host.id
            )
        elif operation == "devices":
            assert result.json() == {
                "devices": [device("browser")],
                "selected_device_id": "saved",
            }
        else:
            adapter.prepare_device.assert_awaited_once_with("browser")
            adapter.play.assert_not_awaited()
            adapter.resume.assert_not_awaited()
            service._session_repo.update.assert_called_once_with(
                session.id, {"current_device_id": "browser"}
            )
            assert session.playback_status == ItemStatus.PAUSED


def test_unavailable_device_selection_does_not_change_saved_target(manager_client):
    """A stale UI device is rejected before altering room state."""
    client, _, _, _, _, service, _, adapter = manager_client
    adapter.prepare_device.side_effect = SpotifyUpstreamException(
        404, "This Spotify device is unavailable"
    )
    result = client.put("/api/v1/playback/device", json={"device_id": "gone"})
    assert result.status_code == 400
    service._session_repo.update.assert_not_called()
    adapter.transfer.assert_not_awaited()


@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize("device_id", ["iphone", "saved", ""])
def test_play_body_cannot_bypass_owner_only_device_selection(
    manager_client, role, device_id
):
    """Specifying a device in Play cannot circumvent the owner-only selector."""
    client, actor, _, admin, member, service, _, adapter = manager_client
    actor["user"] = admin if role == "admin" else member
    result = client.post("/api/v1/playback/play", json={"device_id": device_id})
    assert result.status_code == 403
    service._session_repo.update.assert_not_called()
    adapter.prepare_device.assert_not_awaited()
    adapter.play.assert_not_awaited()


def test_pause_while_device_offline_still_preserves_manager_intent(manager_client):
    """An offline browser cannot leave the room stuck in the playing state."""
    client, _, _, _, _, service, session, adapter = manager_client
    session.playback_status = ItemStatus.PLAYING
    adapter.pause.side_effect = httpx.HTTPStatusError(
        "offline", request=response(404).request, response=response(404)
    )
    result = client.post("/api/v1/playback/pause")
    assert result.status_code == 204
    adapter.pause.assert_awaited_once_with("saved")
    service._session_repo.update.assert_called_once_with(
        session.id,
        {"playback_status": ItemStatus.PAUSED, "playback_sync_hold_until": None},
    )
    adapter.transfer.assert_not_awaited()


@pytest.mark.parametrize("available", [[], [device("other-browser")]])
def test_missing_playback_does_not_finish_a_track_early(available):
    """Device loss must not skip a song even if another player remains online."""
    poller = PlaybackPollerService()
    session = Session(
        room_id=uuid4(), playback_status=ItemStatus.PLAYING, current_song_id="first"
    )
    adapter = AsyncMock()
    adapter.get_current_playback.return_value = None
    adapter.get_devices.return_value = available
    current_item = MagicMock()
    current_item.song.external_id = "first"
    current_item.song.duration = 180

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as config,
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            ),
            patch.object(
                poller, "_get_session_state", side_effect=[session, session, None]
            ),
            patch.object(poller, "_get_current_queue_item", return_value=current_item),
            patch.object(poller, "_update_session_status") as update_status,
            patch.object(poller, "_broadcast_state_changed", new=AsyncMock()),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ),
            patch.object(poller, "_advance_queue", new=AsyncMock()) as advance,
        ):
            config.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            await poller._poll_loop(session.room_id, uuid4())
            advance.assert_not_awaited()
            update_status.assert_called_once_with(session.id, ItemStatus.PAUSED, 0)

    anyio.run(run)


def test_poller_renews_an_early_rejected_token_before_retrying_playback_state():
    """A Spotify 401 is not left looping until the stored expiry timestamp."""
    poller = PlaybackPollerService()
    session = Session(room_id=uuid4(), playback_status=ItemStatus.STOPPED)
    host_id = uuid4()
    rejected = AsyncMock()
    rejected.get_current_playback.side_effect = httpx.HTTPStatusError(
        "expired", request=response(401).request, response=response(401)
    )
    renewed = AsyncMock()
    renewed.get_current_playback.return_value = None

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as config,
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                side_effect=[rejected, renewed],
            ),
            patch.object(poller, "_get_session_state", side_effect=[session, None]),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ) as get_token,
        ):
            config.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            await poller._poll_loop(session.room_id, host_id)
            assert get_token.await_count == 2
            assert get_token.await_args_list[1].kwargs == {"force_refresh": True}
            renewed.get_current_playback.assert_awaited_once()

    anyio.run(run)


@pytest.mark.parametrize(
    "status,reported_device",
    [
        (ItemStatus.PLAYING, "browser"),
        (ItemStatus.PAUSED, "iphone"),
        (ItemStatus.PAUSED, "browser"),
    ],
)
def test_foreign_playback_cannot_consume_a_selected_or_paused_queue(
    status, reported_device
):
    """A failed phone start cannot let a surviving poller consume the current item."""
    session = Session(
        room_id=uuid4(),
        current_song_id="queued-first",
        current_device_id="iphone",
        playback_status=status,
        playback_position_ms=40000,
        playback_sync_hold_until=datetime.now(timezone.utc) + timedelta(seconds=15),
    )
    adapter = AsyncMock()
    adapter.get_current_playback.return_value = SpotifyPlaybackState(
        is_playing=True,
        track_id="other-track",
        progress_ms=5000,
        duration_ms=180000,
        device_id=reported_device,
    )
    poller = PlaybackPollerService()
    states = (
        [session, session, None] if status == ItemStatus.PLAYING else [session, None]
    )

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as config,
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            ),
            patch.object(poller, "_get_session_state", side_effect=states),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ),
            patch.object(poller, "_build_playback_service_with_queue") as build,
            patch.object(poller, "_update_session_status") as update,
            patch.object(poller, "_broadcast_state_changed", new=AsyncMock()),
            patch.object(poller, "_adopt_external_track") as adopt,
            patch.object(poller, "_advance_queue", new=AsyncMock()) as advance,
        ):
            config.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            await poller._poll_loop(session.room_id, uuid4())
            if status == ItemStatus.PLAYING:
                update.assert_called_once_with(session.id, ItemStatus.PAUSED, 40000)
            else:
                update.assert_not_called()
            build.assert_not_called()
            adopt.assert_not_called()
            advance.assert_not_awaited()

    anyio.run(run)


def test_unconfirmed_automatic_start_holds_pending_queue_for_explicit_retry():
    """A surviving external player cannot trigger background Play every poll."""
    session = Session(
        room_id=uuid4(),
        current_song_id="external-track",
        current_device_id="iphone",
        playback_status=ItemStatus.PLAYING,
    )
    current_item = MagicMock()
    current_item.song.external_id = "queued-first"
    queue = MagicMock()
    queue.get_current_song.return_value = current_item
    playback_service = MagicMock()
    playback_service.finish_song = AsyncMock()
    adapter = AsyncMock()
    adapter.play.side_effect = SpotifyUpstreamException(502, "Not confirmed")
    poller = PlaybackPollerService()

    async def run():
        with (
            patch.object(
                poller,
                "_build_playback_service_with_queue",
                return_value=(playback_service, queue),
            ),
            patch.object(poller, "_update_session_status") as update,
            patch.object(poller, "_update_session_track") as update_track,
            patch.object(poller, "_broadcast_state_changed", new=AsyncMock()) as notify,
        ):
            await poller._advance_queue_locked(
                session, session.room_id, uuid4(), adapter
            )
            update.assert_called_once_with(
                session.id, ItemStatus.PAUSED, sync_hold=True
            )
            update_track.assert_not_called()
            notify.assert_awaited_once_with(session.room_id, "paused")
            playback_service.finish_song.assert_not_awaited()
            adapter.play.assert_awaited_once_with(
                "spotify:track:queued-first", "iphone"
            )

    anyio.run(run)


def test_pause_and_external_resume_reset_the_elapsed_time_baseline():
    """Elapsed time is added once after a device disconnect and external resume."""
    original_start = datetime(2026, 10, 25, 12, 0)
    resumed_at = original_start + timedelta(minutes=2)
    session = Session(
        room_id=uuid4(),
        current_song_id="first",
        playback_status=ItemStatus.PLAYING,
        playback_started_at=original_start,
        playback_position_ms=30000,
    )
    repo = MagicMock()
    repo.get_by_id.return_value = session

    def save(_session_id, data):
        for key, value in data.items():
            setattr(session, key, value)

    repo.update.side_effect = save
    with (
        patch("backend.services.playback_poller_service.DBSession"),
        patch(
            "backend.services.playback_poller_service.SessionRepository",
            return_value=repo,
        ),
        patch("backend.services.playback_poller_service.QueueRepository") as queue_type,
        patch("backend.services.playback_poller_service.datetime") as clock,
    ):
        queue_type.return_value.get_first_item.return_value = None
        poller = PlaybackPollerService()
        poller._update_session_status(session.id, ItemStatus.PAUSED, 90000)
        assert session.playback_started_at is None
        assert session.playback_position_ms == 90000

        # The browser reports its actual position after reconnecting and resuming.
        clock.now.return_value = resumed_at.replace(tzinfo=timezone.utc)
        poller._update_session_status(session.id, ItemStatus.PLAYING, 95000)
        assert session.playback_started_at == resumed_at
        assert session.current_song_id == "first"
        estimated_position = session.playback_position_ms + int(
            (
                resumed_at + timedelta(seconds=10) - session.playback_started_at
            ).total_seconds()
            * 1000
        )
        assert estimated_position == 105000
        assert estimated_position < 180000


def test_oauth_required_event_routes_by_type_and_only_targets_current_managers():
    """The frontend receives its subscribed type without exposing status to members."""
    room_id = uuid4()
    host = User(username="host", room_id=room_id)
    admin = User(username="admin", room_id=room_id)
    member = User(username="member", room_id=room_id)
    departed_admin = User(username="departed", room_id=uuid4())
    room = Room(
        id=room_id,
        room_name="Music",
        room_code="ABCDEF",
        host_user_id=host.id,
        settings={"admin_user_ids": [str(admin.id), str(departed_admin.id)]},
    )
    room.users = [host, admin, member, departed_admin]
    poller = PlaybackPollerService()

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as config,
            patch("backend.services.playback_poller_service.DBSession"),
            patch("backend.services.playback_poller_service.RoomRepository") as rooms,
            patch.object(
                poller, "_get_session_state", return_value=Session(room_id=room_id)
            ),
            patch.object(
                poller,
                "_get_valid_token",
                new=AsyncMock(side_effect=SpotifyConnectionRequiredException()),
            ),
            patch(
                "backend.services.playback_poller_service.manager.broadcast_to_users",
                new=AsyncMock(),
            ) as targeted,
            patch(
                "backend.services.playback_poller_service.manager.broadcast",
                new=AsyncMock(),
            ) as public,
        ):
            config.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            rooms.return_value.get_by_id.return_value = room
            await poller._poll_loop(room_id, host.id)
            targeted.assert_awaited_once_with(
                {
                    "type": "oauth_required",
                    "code": "oauth_required",
                    "message": "Host must connect their streaming account",
                    "room_id": str(room_id),
                },
                str(room_id),
                {host.id, admin.id},
            )
            public.assert_not_awaited()

    anyio.run(run)


def test_retained_paused_spotify_track_cannot_consume_newly_started_queue_item():
    """A Play 204 can precede web-player activation and still return the old track."""
    session = Session(
        room_id=uuid4(),
        playback_status=ItemStatus.PLAYING,
        current_song_id="queued-first",
        playback_position_ms=0,
        playback_started_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    current_item = MagicMock()
    current_item.song.external_id = "queued-first"
    adapter = AsyncMock()
    adapter.get_current_playback.return_value = SpotifyPlaybackState(
        is_playing=False,
        track_id="old-paused-track",
        progress_ms=0,
        duration_ms=180000,
        device_id="browser",
    )
    poller = PlaybackPollerService()

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as config,
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            ),
            patch.object(
                poller, "_get_session_state", side_effect=[session, session, None]
            ),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ),
            patch.object(poller, "_get_current_queue_item", return_value=current_item),
            patch.object(poller, "_get_next_queue_item", return_value=None),
            patch.object(
                poller, "_build_playback_service_with_queue"
            ) as build_playback,
            patch.object(poller, "_update_session_status") as update,
            patch.object(poller, "_broadcast_state_changed", new=AsyncMock()) as notify,
            patch.object(poller, "_adopt_external_track") as adopt,
            patch.object(poller, "_advance_queue", new=AsyncMock()) as advance,
        ):
            config.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            await poller._poll_loop(session.room_id, uuid4())
            update.assert_called_once_with(session.id, ItemStatus.PAUSED, 0)
            notify.assert_awaited_once_with(session.room_id, "paused", "queued-first")
            build_playback.assert_not_called()
            adopt.assert_not_called()
            advance.assert_not_awaited()
            assert session.current_song_id == "queued-first"

    anyio.run(run)


@pytest.mark.parametrize("elapsed_seconds,ended", [(1, False), (181, True)])
def test_paused_zero_progress_requires_elapsed_duration_before_queue_advance(
    elapsed_seconds, ended
):
    """Distinguish blocked initial playback from a completed track resetting to zero."""
    session = Session(
        room_id=uuid4(),
        playback_status=ItemStatus.PLAYING,
        current_song_id="queued-first",
        playback_position_ms=0,
        playback_started_at=(
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=elapsed_seconds)
        ),
    )
    adapter = AsyncMock()
    adapter.get_current_playback.return_value = SpotifyPlaybackState(
        is_playing=False,
        track_id="queued-first",
        progress_ms=0,
        duration_ms=180000,
        device_id="browser",
    )
    poller = PlaybackPollerService()
    host_id = uuid4()

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as config,
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            ),
            patch.object(poller, "_get_session_state", side_effect=[session, None]),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ),
            patch.object(poller, "_update_session_status") as update,
            patch.object(poller, "_broadcast_state_changed", new=AsyncMock()),
            patch.object(poller, "_adopt_external_track") as adopt,
            patch.object(poller, "_advance_queue", new=AsyncMock()) as advance,
        ):
            config.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            await poller._poll_loop(session.room_id, host_id)
            if ended:
                advance.assert_awaited_once_with(
                    session, session.room_id, host_id, adapter
                )
                update.assert_not_called()
            else:
                advance.assert_not_awaited()
                update.assert_called_once_with(session.id, ItemStatus.PAUSED, 0)
            adopt.assert_not_called()
            assert session.current_song_id == "queued-first"

    anyio.run(run)
