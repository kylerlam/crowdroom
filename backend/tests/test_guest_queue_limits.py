"""Guest quotas persist and cannot be spent twice by parallel queue requests."""

import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine, select

from backend.core.exceptions import ForbiddenException, TooManyRequestsException
from backend.core.guest_access import guest_request_available_at
from backend.db.models import QueueItem, Room, RoomBan, Song, User
from backend.db.models.enum import ItemStatus
from backend.db.models.session import Session
from backend.repositories.queue_repo import QueueRepository


@pytest.fixture(params=["sqlite", "postgres"])
def guest_queue(request, tmp_path):
    """Exercise both real storage engines without touching production tables."""
    schema = None
    if request.param == "postgres":
        url = os.environ.get("CROWDROOM_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("CROWDROOM_TEST_POSTGRES_URL not set")
        base = create_engine(url)
        schema = f"guest_{uuid4().hex}"
        with base.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = base.execution_options(schema_translate_map={None: schema})
    else:
        base = engine = create_engine(
            f"sqlite:///{tmp_path / 'guest.db'}", connect_args={"timeout": 20}
        )

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        host = User(username="host")
        guest = User(
            username="guest_internal",
            guest_name="Listener",
            is_guest=True,
            guest_first_joined_at=datetime.now(timezone.utc) - timedelta(minutes=6),
        )
        db.add_all([host, guest])
        db.commit()
        room = Room(host_user_id=host.id, room_name="Guest room", room_code="GUEST1")
        db.add(room)
        db.commit()
        host.room_id = guest.room_id = room.id
        db.add_all([host, guest])
        session = Session(room_id=room.id)
        song = Song(
            external_id="guest-track", title="Track", artist="Artist", duration=180
        )
        db.add_all([session, song])
        db.commit()
        state = SimpleNamespace(
            engine=engine,
            host=host.id,
            guest=guest.id,
            session=session.id,
            song=song.id,
            room=room.id,
        )
    yield state
    if schema:
        with base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    base.dispose()


def add_guest(state, db, group="manual"):
    return QueueRepository(db).add_to_queue_atomic(
        state.session, state.song, state.guest, group
    )


def test_first_wait_and_persisted_half_hour_deadline(guest_queue):
    state = guest_queue
    with DBSession(state.engine) as db:
        guest = db.get(User, state.guest)
        guest.guest_first_joined_at = datetime.now(timezone.utc) - timedelta(minutes=4)
        db.add(guest)
        db.commit()
        with pytest.raises(TooManyRequestsException) as denied:
            add_guest(state, db)
        assert 50 < denied.value.retry_after <= 60
        assert db.get(User, state.guest).guest_last_requested_at is None
        guest.guest_first_joined_at -= timedelta(minutes=2)
        db.add(guest)
        db.commit()
        item = add_guest(state, db)
        db.delete(item)
        db.commit()
    # A fresh request/session must read the committed cooldown, across groups too.
    with DBSession(state.engine) as db:
        guest = db.get(User, state.guest)
        assert guest.guest_last_requested_at is not None
        assert (
            1790
            < (
                guest_request_available_at(guest) - datetime.now(timezone.utc)
            ).total_seconds()
            <= 1800
        )
        with pytest.raises(TooManyRequestsException):
            add_guest(state, db, "playlist")
        guest.guest_last_requested_at = datetime.now(timezone.utc) - timedelta(
            minutes=31
        )
        db.add(guest)
        db.commit()
        assert add_guest(state, db).id


def test_no_join_no_quota_and_ban_still_wins(guest_queue):
    state = guest_queue
    with DBSession(state.engine) as db:
        guest = db.get(User, state.guest)
        guest.guest_first_joined_at = None
        db.add(guest)
        db.commit()
        with pytest.raises(ForbiddenException, match="Join a room"):
            add_guest(state, db)
        guest.guest_first_joined_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        db.add(guest)
        db.add(RoomBan(room_id=state.room, user_id=state.guest))
        db.commit()
        with pytest.raises(ForbiddenException, match="banned"):
            add_guest(state, db)


def test_max_one_pending_but_playing_does_not_occupy_pending_slot(guest_queue):
    state = guest_queue
    with DBSession(state.engine) as db:
        first = add_guest(state, db)
        guest = db.get(User, state.guest)
        guest.guest_last_requested_at = datetime.now(timezone.utc) - timedelta(
            minutes=31
        )
        db.add(guest)
        db.commit()
        with pytest.raises(ForbiddenException, match="already have a song waiting"):
            add_guest(state, db)
        first.playback_status = ItemStatus.PLAYING
        db.add(first)
        db.commit()
        assert add_guest(state, db).id != first.id


def test_failed_insert_does_not_spend_quota(guest_queue):
    state = guest_queue
    with DBSession(state.engine) as db:
        with pytest.raises(IntegrityError):
            QueueRepository(db).add_to_queue_atomic(state.session, uuid4(), state.guest)
        assert db.get(User, state.guest).guest_last_requested_at is None
        assert add_guest(state, db).id


def test_registered_requests_are_unlimited(guest_queue):
    state = guest_queue
    with DBSession(state.engine) as db:
        repo = QueueRepository(db)
        for _ in range(3):
            repo.add_to_queue_atomic(state.session, state.song, state.host)
        assert len(db.exec(select(QueueItem)).all()) == 3


def test_parallel_guest_requests_share_quota_across_groups(guest_queue):
    state = guest_queue
    barrier = threading.Barrier(2)
    results, errors = [], []

    def request_song(group):
        with DBSession(state.engine) as db:
            barrier.wait(timeout=10)
            try:
                results.append(add_guest(state, db, group).id)
            except Exception as exc:
                errors.append(exc)

    threads = [
        threading.Thread(target=request_song, args=(group,))
        for group in ("manual", "playlist")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], TooManyRequestsException)
    with DBSession(state.engine) as db:
        assert len(db.exec(select(QueueItem)).all()) == 1
