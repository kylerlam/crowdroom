"""Regression tests for pending queue priorities and manager-only actions."""

import threading
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import anyio
import pytest
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from backend.api.queue.router import (
    get_current_song,
    jump_queue_item,
    pin_queue_item,
    remove_from_queue,
)
from backend.core.exceptions import ForbiddenException
from backend.db.models.enum import ItemStatus
from backend.db.models.queue_item import QueueItem
from backend.db.models.session import Session as SessionModel
from backend.db.models.song import Song
from backend.repositories.queue_history_repo import QueueHistoryRepository
from backend.repositories.queue_repo import QueueRepository
from backend.repositories.session_repo import SessionRepository
from backend.schemas.queue_item import UpdateQueuePin
from backend.services.playback_service import PlaybackService
from backend.services.queue_service import QueueService
from backend.services.room_service import RoomService


@pytest.fixture
def queue():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        session = SessionModel(room_id=uuid4())
        db.add(session)
        db.commit()
        repo = QueueRepository(db)

        def add(title, group="manual"):
            song = Song(
                external_id=title,
                title=title,
                artist="Artist",
                duration=180,
            )
            db.add(song)
            db.commit()
            return repo.add_to_queue_atomic(session.id, song.id, group=group)

        yield db, session, repo, add
    engine.dispose()


def test_jump_keeps_current_track_and_can_prioritize_playlist_song(queue):
    db, session, repo, add = queue
    current = add("current")
    ordinary = add("ordinary")
    jumped = add("playlist", group="playlist")
    session.current_song_id = current.song.external_id
    session.playback_status = ItemStatus.PLAYING
    current.playback_status = ItemStatus.PLAYING
    db.add(session)
    db.add(current)
    db.commit()

    repo.prioritize_item(jumped.id)

    assert [item.id for item in repo.get_all_by_session(session.id)] == [
        current.id,
        jumped.id,
        ordinary.id,
    ]
    assert repo.get_first_item(session.id).id == current.id
    assert session.current_song_id == "current"
    with pytest.raises(ValueError, match="currently playing"):
        repo.prioritize_item(current.id, True)


def test_pin_order_jump_and_unpin_ignore_votes(queue):
    db, session, repo, add = queue
    ordinary, first_pin, second_pin, jumped = [add(str(i)) for i in range(4)]
    repo.prioritize_item(first_pin.id, True)
    repo.prioritize_item(second_pin.id, True)
    repo.prioritize_item(jumped.id)
    ordinary.votes_skip = 100
    db.add(ordinary)
    db.commit()

    assert [item.id for item in repo.get_all_by_session(session.id)] == [
        first_pin.id,
        second_pin.id,
        jumped.id,
        ordinary.id,
    ]
    repo.prioritize_item(second_pin.id)
    assert repo.get_first_item(session.id).id == second_pin.id
    repo.prioritize_item(first_pin.id, False)
    assert [item.id for item in repo.get_all_by_session(session.id)] == [
        second_pin.id,
        first_pin.id,
        jumped.id,
        ordinary.id,
    ]
    assert first_pin.is_pinned is False


def test_pin_limit_and_repeated_pin_are_atomic_and_idempotent(queue):
    _, session, repo, add = queue
    items = [add(str(i)) for i in range(6)]
    for item in items[:5]:
        repo.prioritize_item(item.id, True)
    repo.prioritize_item(items[0].id, True)
    with pytest.raises(ValueError, match="up to 5"):
        repo.prioritize_item(items[5].id, True)
    assert [item.id for item in repo.get_all_by_session(session.id)][:5] == [
        item.id for item in items[:5]
    ]
    assert repo.get_by_id(items[5].id).is_pinned is False
    repo.prioritize_item(items[2].id, False)
    repo.prioritize_item(items[5].id, True)
    assert sum(item.is_pinned for item in repo.get_all_by_session(session.id)) == 5


def test_priority_is_persistent_and_played_pin_is_removed_normally(queue):
    db, session, repo, add = queue
    ordinary, pinned = add("ordinary"), add("pinned")
    repo.prioritize_item(pinned.id, True)
    db.expire_all()
    assert repo.get_first_item(session.id).id == pinned.id
    assert repo.delete(pinned.id)
    assert repo.get_first_item(session.id).id == ordinary.id
    assert repo.count_by_session(session.id) == 1


@pytest.mark.parametrize("current_id", [None, "external-song"])
def test_pending_queue_head_is_not_reported_as_current_song(queue, current_id):
    db, session, repo, add = queue
    add("pending")
    session.current_song_id = current_id
    db.add(session)
    db.commit()
    queue_service, session_repo = MagicMock(), MagicMock()
    queue_service.get_current_song.return_value = repo.get_first_item(session.id)
    session_repo.get_by_id.return_value = session
    assert get_current_song(session.id, queue_service, session_repo) is None


def test_pending_duplicate_can_be_pinned_without_displacing_active_entry(queue):
    db, session, repo, add = queue
    current = add("repeated", group="playlist")
    duplicate = repo.add_to_queue_atomic(session.id, current.song_id)
    session.current_song_id = "repeated"
    session.playback_status = ItemStatus.PAUSED
    current.playback_status = ItemStatus.PAUSED
    db.add(session)
    db.add(current)
    db.commit()
    repo.prioritize_item(duplicate.id, True)
    assert [item.id for item in repo.get_all_by_session(session.id)] == [
        current.id,
        duplicate.id,
    ]


def test_finishing_song_does_not_pull_pending_duplicate_ahead_of_pin(queue):
    db, session, repo, add = queue
    current = add("repeated")
    duplicate = repo.add_to_queue_atomic(session.id, current.song_id)
    pinned = add("pinned")
    repo.prioritize_item(pinned.id, True)
    session.current_song_id = "repeated"
    session.playback_status = ItemStatus.PLAYING
    current.playback_status = ItemStatus.PLAYING
    db.add(session)
    db.add(current)
    db.commit()
    session_repo = SessionRepository(db)
    queue_service = QueueService(repo, session_repo)
    playback = PlaybackService(session_repo, queue_service, QueueHistoryRepository(db))

    async def run():
        with patch("backend.services.queue_service.manager.broadcast", new=AsyncMock()):
            await playback.finish_song(session.id)

    anyio.run(run)
    assert [item.id for item in repo.get_all_by_session(session.id)] == [
        pinned.id,
        duplicate.id,
    ]


def test_concurrent_pin_requests_never_exceed_five(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    SQLModel.metadata.create_all(engine)
    session_id = uuid4()
    with DBSession(engine) as db:
        repo = QueueRepository(db)
        item_ids = [repo.add_to_queue_atomic(session_id, uuid4()).id for _ in range(6)]
    errors = []

    def pin(item_id):
        with DBSession(engine) as db:
            try:
                QueueRepository(db).prioritize_item(item_id, True)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=pin, args=(item_id,)) for item_id in item_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "up to 5" in str(errors[0])
    with DBSession(engine) as db:
        items = QueueRepository(db).get_all_by_session(session_id)
        assert sum(item.is_pinned for item in items) == 5
    engine.dispose()


@pytest.mark.parametrize("action", ["jump", "pin", "remove"])
def test_queue_management_requires_room_manager(action):
    queue_service = MagicMock()
    item = QueueItem(session_id=uuid4(), song_id=uuid4())
    queue_service.get_queue_item.return_value = item
    queue_service.prioritize_item = AsyncMock()
    queue_service.remove_from_queue = AsyncMock()
    session_repo = MagicMock()
    room_service = MagicMock(spec=RoomService)
    user = MagicMock()
    room_service.assert_manager.side_effect = ForbiddenException()
    kwargs = {
        "queue_item_id": item.id,
        "queue_service": queue_service,
        "session_repo": session_repo,
        "room_service": room_service,
        "current_user": user,
    }

    async def run():
        if action == "jump":
            await jump_queue_item(**kwargs)
        elif action == "pin":
            await pin_queue_item(data=UpdateQueuePin(is_pinned=True), **kwargs)
        else:
            await remove_from_queue(**kwargs)

    with pytest.raises(ForbiddenException):
        anyio.run(run)
    queue_service.prioritize_item.assert_not_awaited()
    queue_service.remove_from_queue.assert_not_awaited()
    room_service.assert_manager.assert_called_once_with(
        session_repo.get_by_id.return_value.room_id, user.id
    )
