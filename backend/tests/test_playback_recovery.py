"""Regression checks for exhausted queues and deliberate playback pauses."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import anyio
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from backend.adapters.spotify_playback_adapter import SpotifyPlaybackState
from backend.core.exceptions import (
    SpotifyConnectionRequiredException,
    SpotifyUpstreamException,
)
from backend.db.models import QueueItem, Room, Song, User
from backend.db.models.enum import ItemStatus, StreamingPlatforms
from backend.db.models.session import Session as SessionModel
from backend.repositories.queue_history_repo import QueueHistoryRepository
from backend.repositories.queue_repo import QueueRepository
from backend.repositories.session_repo import SessionRepository
from backend.repositories.song_repo import SongRepository
from backend.services.playback_control_service import PlaybackControlService
from backend.services.playback_poller_service import PlaybackPollerService
from backend.services.playback_service import PlaybackService
from backend.services.queue_service import QueueService
from backend.services.room_service import RoomService


@pytest.fixture
def recovery_stack():
    """Wire real queue/session persistence with a mocked Spotify boundary."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        owner = User(username="owner")
        db.add(owner)
        db.commit()
        room = Room(host_user_id=owner.id, room_name="Music", room_code="ABCDEF")
        db.add(room)
        db.commit()
        session = SessionModel(room_id=room.id)
        first = Song(external_id="first", title="First", artist="A", duration=180)
        second = Song(external_id="second", title="Second", artist="A", duration=180)
        db.add_all([session, first, second])
        db.commit()
        session_repo = SessionRepository(db)
        queue_repo = QueueRepository(db)
        queue = QueueService(queue_repo, session_repo)
        playback = PlaybackService(session_repo, queue, QueueHistoryRepository(db))
        rooms = MagicMock(spec=RoomService)
        rooms.get_room.return_value = room
        rooms.assert_manager.return_value = room
        connection = MagicMock()
        connection.get_valid_access_token = AsyncMock(return_value="owner-token")
        control = PlaybackControlService(
            rooms, session_repo, SongRepository(db), connection, playback, queue
        )
        with (
            patch("backend.services.queue_service.manager.broadcast", new=AsyncMock()),
            patch(
                "backend.services.playback_service.manager.broadcast", new=AsyncMock()
            ),
            patch(
                "backend.services.playback_control_service.manager.broadcast",
                new=AsyncMock(),
            ),
            patch(
                "backend.services.playback_control_service.SpotifyPlaybackAdapter"
            ) as adapter_type,
        ):
            adapter = AsyncMock()
            adapter_type.return_value = adapter
            yield session, first, second, queue_repo, queue, playback, control, adapter
    engine.dispose()


def test_exhausted_queue_auto_starts_next_added_song(recovery_stack):
    """Finishing the last song must not leave an ID blocking future additions."""
    session, first, second, repo, queue, playback, control, adapter = recovery_stack
    session.current_song_id = first.external_id
    session.playback_status = ItemStatus.PLAYING
    repo.create(QueueItem(session_id=session.id, song_id=first.id, group="manual"))

    async def run():
        await playback.finish_song(session.id)
        assert session.current_song_id is None
        assert session.playback_status == ItemStatus.STOPPED
        added = await queue.add_to_queue(session.id, second.id)
        await control.start_if_idle(session.id)
        assert added.playback_status == ItemStatus.PLAYING

    anyio.run(run)
    adapter.play.assert_awaited_once_with("spotify:track:second", None)
    assert session.current_song_id == "second"
    assert session.playback_status == ItemStatus.PLAYING


def test_missing_oauth_keeps_a_successfully_added_song_queued(recovery_stack):
    """Missing account consent cannot turn a persisted addition into an error."""
    session, first, _, _, queue, _, control, adapter = recovery_stack
    control._platform_connection_service.get_valid_access_token.side_effect = (
        SpotifyConnectionRequiredException()
    )

    async def run():
        await queue.add_to_queue(session.id, first.id)
        await control.start_if_idle(session.id)

    anyio.run(run)
    assert queue.get_queue(session.id)[0].song_id == first.id
    assert session.current_song_id is None
    adapter.play.assert_not_awaited()


@pytest.mark.parametrize("has_current_track", [False, True])
def test_new_song_does_not_undo_explicit_pause(recovery_stack, has_current_track):
    """An explicit pause holds even if the now-playing pane has no song."""
    session, first, second, repo, queue, _, control, adapter = recovery_stack
    session.current_song_id = first.external_id if has_current_track else None
    session.playback_status = ItemStatus.PAUSED
    if has_current_track:
        repo.create(QueueItem(session_id=session.id, song_id=first.id, group="manual"))
    else:
        control._session_repo.update(session.id, {"playback_status": ItemStatus.PAUSED})

    async def run():
        await queue.add_to_queue(session.id, second.id)
        await control.start_if_idle(session.id)

    anyio.run(run)
    adapter.play.assert_not_awaited()
    adapter.resume.assert_not_awaited()
    assert session.playback_status == ItemStatus.PAUSED


def test_resume_idle_queue_uses_first_song_and_owner_credentials(recovery_stack):
    """An administrator resumes the room queue on its creator's Spotify."""
    session, first, _, repo, _, _, control, adapter = recovery_stack
    session.playback_status = ItemStatus.STOPPED
    session.current_song_id = "stale-finished-track"
    repo.create(QueueItem(session_id=session.id, song_id=first.id, group="manual"))
    administrator_id = uuid4()

    async def run():
        await control.play(session.room_id, administrator_id)

    anyio.run(run)
    adapter.play.assert_awaited_once_with("spotify:track:first", None)
    adapter.resume.assert_not_awaited()
    owner = control._room_service.assert_manager.return_value.host_user_id
    control._platform_connection_service.get_valid_access_token.assert_awaited_once_with(
        owner, StreamingPlatforms.SPOTIFY
    )
    assert session.current_song_id == "first"


def test_resume_empty_queue_reports_add_song_message(recovery_stack):
    """An idle empty queue must not resume Spotify's unrelated old context."""
    session, _, _, _, _, _, control, adapter = recovery_stack
    with pytest.raises(ValueError, match="Add a song first"):
        anyio.run(control.play, session.room_id, uuid4())
    adapter.play.assert_not_awaited()
    adapter.resume.assert_not_awaited()


def test_automatic_playback_keeps_and_updates_the_selected_device(recovery_stack):
    """Automatically queued songs reuse the selected browser after its ID rotates."""
    session, first, _, _, queue, _, control, adapter = recovery_stack
    control._session_repo.update(session.id, {"current_device_id": "saved-browser"})
    adapter.play.return_value = "recovered-browser"

    async def run():
        await queue.add_to_queue(session.id, first.id)
        await control.start_if_idle(session.id)

    anyio.run(run)
    adapter.play.assert_awaited_once_with("spotify:track:first", "saved-browser")
    assert session.current_device_id == "recovered-browser"


def test_resume_on_a_new_phone_sends_current_queue_uri_and_saved_position(
    recovery_stack,
):
    """A phone without Spotify context cannot resume the room via an empty body."""
    session, first, _, repo, _, _, control, adapter = recovery_stack
    repo.create(QueueItem(session_id=session.id, song_id=first.id, group="manual"))
    control._session_repo.update(
        session.id,
        {
            "current_song_id": first.external_id,
            "current_device_id": "iphone",
            "playback_status": ItemStatus.PAUSED,
            "playback_position_ms": 43000,
        },
    )
    adapter.get_current_playback.return_value = None
    adapter.play.return_value = "iphone"
    anyio.run(control.play, session.room_id, uuid4())
    adapter.play.assert_awaited_once_with(
        "spotify:track:first", "iphone", position_ms=43000
    )
    adapter.resume.assert_not_awaited()
    assert session.current_song_id == first.external_id
    assert session.playback_position_ms == 43000


def test_confirmed_phone_playback_is_not_reset_by_resume(recovery_stack):
    """A repeated Resume observes the actual device and preserves its progress."""
    session, first, _, repo, _, _, control, adapter = recovery_stack
    repo.create(QueueItem(session_id=session.id, song_id=first.id, group="manual"))
    control._session_repo.update(
        session.id,
        {
            "current_song_id": first.external_id,
            "current_device_id": "iphone",
            "playback_status": ItemStatus.PAUSED,
            "playback_position_ms": 43000,
        },
    )
    adapter.get_current_playback.return_value = SpotifyPlaybackState(
        is_playing=True,
        track_id="first",
        progress_ms=47000,
        duration_ms=180000,
        device_id="iphone",
    )
    anyio.run(control.play, session.room_id, uuid4())
    adapter.play.assert_not_awaited()
    adapter.resume.assert_not_awaited()
    assert session.playback_position_ms == 47000
    assert session.playback_status == ItemStatus.PLAYING


def test_unconfirmed_phone_playback_keeps_current_queue_and_paused_position(
    recovery_stack,
):
    """A Play 204 without actual output cannot mark or consume the queued song."""
    session, first, _, repo, queue, _, control, adapter = recovery_stack
    item = repo.create(
        QueueItem(session_id=session.id, song_id=first.id, group="manual")
    )
    control._session_repo.update(
        session.id,
        {
            "current_song_id": first.external_id,
            "current_device_id": "iphone",
            "playback_status": ItemStatus.PAUSED,
            "playback_position_ms": 43000,
        },
    )
    adapter.get_current_playback.return_value = None
    adapter.play.side_effect = SpotifyUpstreamException(502, "Playback not confirmed")
    with pytest.raises(SpotifyUpstreamException):
        anyio.run(control.play, session.room_id, uuid4())
    assert [queued.id for queued in queue.get_queue(session.id)] == [item.id]
    assert session.playback_status == ItemStatus.PAUSED
    assert session.playback_position_ms == 43000
    assert session.current_song_id == first.external_id


def test_explicit_queue_song_overrides_old_external_context_on_selected_phone(
    recovery_stack,
):
    """The empty-player Resume may explicitly select its visible first queued song."""
    session, first, _, repo, _, _, control, adapter = recovery_stack
    repo.create(QueueItem(session_id=session.id, song_id=first.id, group="manual"))
    control._session_repo.update(
        session.id,
        {
            "current_song_id": "old-external-track",
            "current_device_id": "iphone",
            "playback_status": ItemStatus.PAUSED,
            "playback_position_ms": 43000,
        },
    )
    adapter.play.return_value = "iphone"
    anyio.run(control.play, session.room_id, uuid4(), first.id)
    adapter.play.assert_awaited_once_with("spotify:track:first", "iphone")
    adapter.resume.assert_not_awaited()
    assert session.current_song_id == first.external_id
    assert session.playback_position_ms == 0


def test_lost_phone_context_restores_elapsed_position_and_holds_after_failure(
    recovery_stack,
):
    """A previously playing room must not restore a disconnected phone at zero."""
    session, first, _, repo, queue, _, control, adapter = recovery_stack
    item = repo.create(
        QueueItem(session_id=session.id, song_id=first.id, group="manual")
    )
    now = datetime(2026, 10, 26, 12, 0)
    control._session_repo.update(
        session.id,
        {
            "current_song_id": first.external_id,
            "current_device_id": "iphone",
            "playback_status": ItemStatus.PLAYING,
            "playback_position_ms": 30000,
            "playback_started_at": now - timedelta(seconds=90),
        },
    )
    adapter.get_current_playback.return_value = None
    adapter.play.side_effect = SpotifyUpstreamException(502, "Playback not confirmed")
    with patch("backend.services.playback_control_service._utcnow", return_value=now):
        with pytest.raises(SpotifyUpstreamException):
            anyio.run(control.play, session.room_id, uuid4())
    adapter.play.assert_awaited_once_with(
        "spotify:track:first", "iphone", position_ms=120000
    )
    assert [queued.id for queued in queue.get_queue(session.id)] == [item.id]
    assert session.playback_status == ItemStatus.PAUSED
    assert session.playback_position_ms == 120000
    assert session.playback_started_at is None


@pytest.mark.parametrize("status", [ItemStatus.PAUSED, ItemStatus.STOPPED])
@pytest.mark.parametrize("same_track", [False, True])
def test_poller_leaves_stopped_or_explicitly_paused_player_idle(status, same_track):
    """A retained Spotify track at progress zero is not a request to restart."""
    poller = PlaybackPollerService()
    session = SessionModel(room_id=uuid4(), playback_status=status)
    session.current_song_id = "last-track" if same_track else None
    adapter = AsyncMock()
    adapter.get_current_playback.return_value = SpotifyPlaybackState(
        is_playing=False,
        track_id="last-track",
        progress_ms=0,
        duration_ms=180000,
        device_id="device",
    )

    async def run():
        with (
            patch("backend.services.playback_poller_service.settings") as settings,
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            ),
            patch.object(poller, "_get_session_state", side_effect=[session, None]),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ),
            patch.object(poller, "_advance_queue", new=AsyncMock()) as advance,
            patch.object(poller, "_adopt_external_track") as adopt,
            patch.object(poller, "_update_session_status") as update_status,
        ):
            settings.PLAYBACK_POLL_INTERVAL_SECONDS = 0
            await poller._poll_loop(session.room_id, uuid4())
            advance.assert_not_awaited()
            adopt.assert_not_called()
            if status == ItemStatus.PAUSED and same_track:
                update_status.assert_called_once_with(session.id, ItemStatus.PAUSED, 0)
            else:
                update_status.assert_not_called()

    anyio.run(run)
