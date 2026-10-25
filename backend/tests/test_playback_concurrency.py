"""Playback requests serialize across independent database sessions."""

from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import anyio
import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from backend.db.models import QueueItem, Room, Song, User
from backend.db.models.enum import ItemStatus
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
def playback_requests():
    """Keep two independent request identity maps cached at the stopped state."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with ExitStack() as stack:
        db = stack.enter_context(DBSession(engine, expire_on_commit=False))
        owner = User(username="owner")
        db.add(owner)
        db.commit()
        room = Room(host_user_id=owner.id, room_name="Music", room_code="ABCDEF")
        db.add(room)
        db.commit()
        session = SessionModel(room_id=room.id, playback_status=ItemStatus.STOPPED)
        first = Song(external_id="first", title="First", artist="A", duration=180)
        second = Song(external_id="second", title="Second", artist="A", duration=180)
        db.add_all([session, first, second])
        db.commit()
        db.add_all(
            [
                QueueItem(
                    session_id=session.id, song_id=first.id, group="manual", position=0
                ),
                QueueItem(
                    session_id=session.id, song_id=second.id, group="manual", position=1
                ),
            ]
        )
        db.commit()
        controls = []
        cached_sessions = []
        for _ in range(2):
            request_db = stack.enter_context(DBSession(engine, expire_on_commit=False))
            sessions = SessionRepository(request_db)
            cached_sessions.append(sessions.get_by_id(session.id))
            queue = QueueService(QueueRepository(request_db), sessions)
            playback = PlaybackService(
                sessions, queue, QueueHistoryRepository(request_db)
            )
            rooms = MagicMock(spec=RoomService)
            rooms.get_room.return_value = room
            rooms.assert_manager.return_value = room
            connection = MagicMock()
            connection.get_valid_access_token = AsyncMock(return_value="owner-token")
            controls.append(
                PlaybackControlService(
                    rooms,
                    sessions,
                    SongRepository(request_db),
                    connection,
                    playback,
                    queue,
                )
            )
        stack.enter_context(
            patch("backend.api.websocket.manager.broadcast", new=AsyncMock())
        )
        adapter = AsyncMock()
        stack.enter_context(
            patch(
                "backend.services.playback_control_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            )
        )
        stack.enter_context(
            patch("backend.services.playback_poller_service.engine", engine)
        )
        yield SimpleNamespace(
            controls=controls,
            cached_sessions=cached_sessions,
            room=room,
            session=session,
            first=first,
            adapter=adapter,
        )
    engine.dispose()


def test_concurrent_additions_only_start_spotify_once(playback_requests):
    """A second request must reload playing state after waiting for the first."""
    state = playback_requests
    first, second = state.controls

    async def run():
        playing = anyio.Event()
        release = anyio.Event()
        second_started = anyio.Event()

        async def slow_play(*_):
            playing.set()
            await release.wait()

        async def second_request():
            second_started.set()
            await second.start_if_idle(state.session.id)

        state.adapter.play.side_effect = slow_play
        async with anyio.create_task_group() as group:
            group.start_soon(first.start_if_idle, state.session.id)
            await playing.wait()
            group.start_soon(second_request)
            await second_started.wait()
            release.set()

    anyio.run(run)
    state.adapter.play.assert_awaited_once_with("spotify:track:first", None)


@pytest.mark.parametrize("pause_first", [False, True])
def test_explicit_pause_wins_over_inflight_autostart(playback_requests, pause_first):
    """An overlapping add-song request cannot undo the manager's pause."""
    state = playback_requests
    automatic, manual = state.controls

    async def run():
        operation_started = anyio.Event()
        release = anyio.Event()
        overlapping_started = anyio.Event()

        async def slow_operation(*_):
            operation_started.set()
            await release.wait()

        async def auto_start():
            await automatic.start_if_idle(state.session.id)

        async def pause():
            await manual.pause(state.room.id, state.room.host_user_id)

        async def overlapping():
            overlapping_started.set()
            await (auto_start() if pause_first else pause())

        operation = state.adapter.pause if pause_first else state.adapter.play
        operation.side_effect = slow_operation
        async with anyio.create_task_group() as group:
            group.start_soon(pause if pause_first else auto_start)
            await operation_started.wait()
            group.start_soon(overlapping)
            await overlapping_started.wait()
            release.set()

    anyio.run(run)
    with DBSession(state.controls[0]._session_repo._session.get_bind()) as db:
        saved = db.get(SessionModel, state.session.id)
        assert saved.playback_status == ItemStatus.PAUSED
    assert state.adapter.play.await_count == (0 if pause_first else 1)
    state.adapter.pause.assert_awaited_once()


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_autostart_network_failure_leaves_inserted_songs_queued(
    playback_requests, error_type
):
    """A failed Spotify connection cannot turn a committed addition into HTTP500."""
    state = playback_requests
    state.adapter.play.side_effect = error_type("Spotify unavailable")
    anyio.run(state.controls[0].start_if_idle, state.session.id)
    queue = state.controls[0]._queue_service
    assert queue.get_queue_count(state.session.id) == 2
    assert state.cached_sessions[0].playback_status == ItemStatus.STOPPED


def test_poller_rechecks_pause_after_waiting_for_manual_control(playback_requests):
    """A stale playing snapshot cannot make the poller advance a paused room."""
    state = playback_requests
    manual = state.controls[0]
    manual._session_repo.update(
        state.session.id,
        {
            "playback_status": ItemStatus.PLAYING,
            "current_song_id": state.first.external_id,
        },
    )
    stale = state.controls[1]._session_repo.get_by_id(state.session.id)
    stale.playback_status = ItemStatus.PLAYING
    stale.current_song_id = state.first.external_id
    poller = PlaybackPollerService()

    async def run():
        pausing = anyio.Event()
        release = anyio.Event()
        poller_started = anyio.Event()

        async def slow_pause():
            pausing.set()
            await release.wait()

        async def advance():
            poller_started.set()
            await poller._advance_queue(
                stale, state.room.id, state.room.host_user_id, state.adapter
            )

        state.adapter.pause.side_effect = slow_pause
        async with anyio.create_task_group() as group:
            group.start_soon(manual.pause, state.room.id, state.room.host_user_id)
            await pausing.wait()
            group.start_soon(advance)
            await poller_started.wait()
            release.set()

    anyio.run(run)
    state.adapter.play.assert_not_awaited()
    assert manual._queue_service.get_queue_count(state.session.id) == 2


@pytest.mark.parametrize("changed", ["current_song_id", "playback_started_at"])
def test_poller_does_not_advance_a_track_changed_by_manual_control(changed):
    """Discard an end-of-song snapshot after a manager plays or resumes."""
    previous = SessionModel(
        room_id=uuid4(),
        current_song_id="previous",
        playback_status=ItemStatus.PLAYING,
        playback_started_at=datetime(2026, 10, 25),
    )
    value = (
        "different-track"
        if changed == "current_song_id"
        else previous.playback_started_at + timedelta(seconds=1)
    )
    latest = previous.model_copy(update={changed: value})
    poller = PlaybackPollerService()

    async def run():
        with (
            patch.object(poller, "_get_session_state", return_value=latest),
            patch.object(poller, "_advance_queue_locked", new=AsyncMock()) as advance,
        ):
            await poller._advance_queue(
                previous, previous.room_id, uuid4(), AsyncMock()
            )
            advance.assert_not_awaited()

    anyio.run(run)
