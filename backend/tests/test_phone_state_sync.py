"""Observe selected-device phone changes without treating them as queue commands."""

# ruff: noqa: F811

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import anyio
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine, select

from backend.adapters.spotify_playback_adapter import SpotifyPlaybackState
from backend.core.exceptions import SpotifyRateLimitedException
from backend.db.models import (
    PlatformConnection,
    QueueHistory,
    QueueItem,
    Room,
    Song,
    User,
)
from backend.db.models.enum import ConnectionType, ItemStatus, StreamingPlatforms
from backend.db.models.session import Session
from backend.repositories.queue_repo import QueueRepository
from backend.repositories.session_repo import SessionRepository
from backend.services.playback_poller_service import PlaybackPollerService
from backend.tests.test_playback_recovery import recovery_stack  # noqa: F401


@pytest.fixture
def phone_room():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        owner = User(username="owner")
        db.add(owner)
        db.commit()
        room = Room(host_user_id=owner.id, room_name="Phone room", room_code="PHONE1")
        db.add(room)
        db.commit()
        session = Session(
            room_id=room.id,
            current_device_id="iphone",
            current_song_id="first",
            playback_status=ItemStatus.PAUSED,
            playback_position_ms=30000,
        )
        songs = [
            Song(external_id=name, title=name, artist="Artist", duration=180)
            for name in ("first", "second")
        ]
        db.add_all([session, *songs])
        db.commit()
        for position, song in enumerate(songs):
            db.add(
                QueueItem(
                    session_id=session.id,
                    song_id=song.id,
                    group="manual",
                    position=position,
                    playback_status=ItemStatus.PAUSED
                    if position == 0
                    else ItemStatus.QUEUED,
                )
            )
        db.commit()
        yield engine, db, session, owner
    engine.dispose()


def observe_one_cycle(engine, room_id, state, before_response=None):
    """Run the real reconciliation paths, stubbing only Spotify and the clock wait."""
    poller = PlaybackPollerService()
    adapter = AsyncMock()
    waits = 0
    original_sleep = asyncio.sleep

    async def sleep(_seconds):
        nonlocal waits
        waits += 1
        if waits > 1:
            raise asyncio.CancelledError()
        await original_sleep(0)

    async def get_state():
        if before_response:
            before_response()
        return state

    adapter.get_current_playback.side_effect = get_state

    async def run():
        with (
            patch("backend.services.playback_poller_service.engine", engine),
            patch(
                "backend.services.playback_poller_service.SpotifyPlaybackAdapter",
                return_value=adapter,
            ),
            patch(
                "backend.services.playback_poller_service.manager.broadcast",
                new=AsyncMock(),
            ),
            patch.object(
                poller, "_get_valid_token", new=AsyncMock(return_value="token")
            ),
            patch("asyncio.sleep", side_effect=sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await poller._poll_loop(room_id, uuid4())

    anyio.run(run)
    return adapter


def phone_state(track="external", progress=45000, playing=True, device="iphone"):
    return SpotifyPlaybackState(
        is_playing=playing,
        track_id=track,
        progress_ms=progress,
        duration_ms=180000,
        device_id=device,
        track_name=track,
        track_artist="Artist",
    )


@pytest.mark.parametrize(
    "status", [ItemStatus.PAUSED, ItemStatus.STOPPED, ItemStatus.PLAYING]
)
@pytest.mark.parametrize("track", ["external", "second"])
def test_phone_switch_adopts_actual_track_and_preserves_every_waiting_song(
    phone_room, status, track
):
    engine, db, session, _owner = phone_room
    repo = SessionRepository(db)
    repo.update(
        session.id,
        {
            "playback_status": status,
            "playback_started_at": datetime.now(timezone.utc).replace(tzinfo=None)
            if status == ItemStatus.PLAYING
            else None,
        },
    )
    before = {item.id for item in db.exec(select(QueueItem)).all()}
    adapter = observe_one_cycle(engine, session.room_id, phone_state(track))
    saved = repo.get_by_id(session.id, fresh=True)
    assert saved.current_song_id == track
    assert saved.playback_status == ItemStatus.PLAYING
    assert saved.playback_position_ms == 45000
    assert saved.playback_sync_hold_until is None
    items = QueueRepository(db).get_all_by_session(session.id)
    assert {item.id for item in items} == before
    db.expire_all()
    assert all(
        item.playback_status == ItemStatus.QUEUED
        for item in items
        if item.song.external_id != track
    )
    assert db.exec(select(QueueHistory)).all() == []
    adapter.play.assert_not_awaited()
    adapter.pause.assert_not_awaited()


@pytest.mark.parametrize("expired", [False, True])
def test_failed_command_guard_only_blocks_stale_context_briefly(phone_room, expired):
    engine, db, session, _owner = phone_room
    repo = SessionRepository(db)
    repo.update(
        session.id,
        {
            "playback_sync_hold_until": datetime.now(timezone.utc)
            + timedelta(seconds=-1 if expired else 15)
        },
    )
    before = [item.id for item in QueueRepository(db).get_all_by_session(session.id)]
    adapter = observe_one_cycle(engine, session.room_id, phone_state())
    saved = repo.get_by_id(session.id, fresh=True)
    assert saved.current_song_id == ("external" if expired else "first")
    assert [
        item.id for item in QueueRepository(db).get_all_by_session(session.id)
    ] == before
    adapter.play.assert_not_awaited()
    adapter.pause.assert_not_awaited()


def test_another_device_cannot_replace_the_selected_phone_state(phone_room):
    engine, db, session, _owner = phone_room
    adapter = observe_one_cycle(
        engine, session.room_id, phone_state(device="other-device")
    )
    saved = SessionRepository(db).get_by_id(session.id, fresh=True)
    assert saved.current_song_id == "first"
    assert saved.playback_status == ItemStatus.PAUSED
    assert len(db.exec(select(QueueItem)).all()) == 2
    adapter.play.assert_not_awaited()


def test_phone_pause_and_seek_are_reflected_without_queue_writes(phone_room):
    engine, db, session, _owner = phone_room
    repo = SessionRepository(db)
    repo.update(
        session.id,
        {
            "playback_status": ItemStatus.PLAYING,
            "playback_started_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    )
    adapter = observe_one_cycle(engine, session.room_id, phone_state("first", 80000))
    assert repo.get_by_id(session.id, fresh=True).playback_position_ms == 80000
    adapter.play.assert_not_awaited()
    adapter = observe_one_cycle(
        engine, session.room_id, phone_state("first", 82000, playing=False)
    )
    saved = repo.get_by_id(session.id, fresh=True)
    assert saved.playback_status == ItemStatus.PAUSED
    assert saved.playback_position_ms == 82000
    assert saved.playback_started_at is None
    assert len(db.exec(select(QueueItem)).all()) == 2
    adapter.pause.assert_not_awaited()


def test_stale_poll_does_not_override_a_new_manual_command(phone_room):
    engine, db, session, _owner = phone_room

    def changed():
        with DBSession(engine) as other:
            SessionRepository(other).update(session.id, {"current_song_id": "second"})

    observe_one_cycle(engine, session.room_id, phone_state(), before_response=changed)
    assert (
        SessionRepository(db).get_by_id(session.id, fresh=True).current_song_id
        == "second"
    )
    assert len(db.exec(select(QueueItem)).all()) == 2


def test_restart_restores_only_authorized_room_monitors_without_playing(phone_room):
    engine, db, session, owner = phone_room
    db.add(
        PlatformConnection(
            user_id=owner.id,
            platform=StreamingPlatforms.SPOTIFY,
            connection_type=ConnectionType.AUTHORIZATION_CODE,
            access_token_encrypted="stored",
            refresh_token_encrypted="stored",
        )
    )
    other = User(username="other")
    db.add(other)
    db.commit()
    room = Room(host_user_id=other.id, room_name="No connection", room_code="PHONE2")
    db.add(room)
    db.commit()
    db.add(Session(room_id=room.id))
    db.commit()
    poller = PlaybackPollerService()

    async def run():
        with (
            patch("backend.services.playback_poller_service.engine", engine),
            patch.object(poller, "start_polling", new=AsyncMock()) as start,
        ):
            await poller.restore_polling()
            start.assert_awaited_once_with(session.room_id, owner.id)

    anyio.run(run)


@pytest.mark.parametrize("operation", ["play", "pause", "skip"])
def test_cooldown_prevents_control_commands_before_mutating_the_queue(
    recovery_stack, operation
):
    session, first, _, repo, queue, _, control, adapter = recovery_stack
    item = repo.create(
        QueueItem(session_id=session.id, song_id=first.id, group="manual")
    )
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    with patch(
        "backend.services.playback_control_service.assert_ready",
        side_effect=SpotifyRateLimitedException(deadline),
    ):
        with pytest.raises(SpotifyRateLimitedException):
            anyio.run(getattr(control, operation), session.room_id, uuid4())
    assert [entry.id for entry in queue.get_queue(session.id)] == [item.id]
    assert session.current_song_id is None
    assert session.playback_status is None
    adapter.play.assert_not_awaited()
    adapter.pause.assert_not_awaited()


def test_auto_start_during_cooldown_keeps_a_successful_addition(recovery_stack):
    session, first, _, _, queue, _, control, adapter = recovery_stack
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)

    async def run():
        await queue.add_to_queue(session.id, first.id)
        with patch(
            "backend.services.playback_control_service.assert_ready",
            side_effect=SpotifyRateLimitedException(deadline),
        ):
            await control.start_if_idle(session.id)

    anyio.run(run)
    assert queue.get_queue_count(session.id) == 1
    assert session.playback_status is None
    adapter.play.assert_not_awaited()
