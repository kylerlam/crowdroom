"""Room membership and bans protect queue writes, including direct API calls."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine, select

from backend.api.auth.dependencies import get_current_user
from backend.api.playback.dependencies import get_playback_control_service
from backend.db.database import get_session
from backend.db.models import QueueItem, Room, RoomBan, Song, User
from backend.db.models.session import Session
from backend.main import app


@pytest.fixture
def room_api():
    """Exercise real API dependencies against a shared isolated database."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        host, member, outsider = [
            User(username=name) for name in ("host", "member", "outsider")
        ]
        db.add_all([host, member, outsider])
        db.commit()
        room = Room(host_user_id=host.id, room_name="Room", room_code="ACCESS")
        db.add(room)
        db.commit()
        host.room_id = member.room_id = room.id
        db.add_all([host, member])
        session = Session(room_id=room.id)
        song = Song(
            external_id="access-track", title="Track", artist="Artist", duration=180
        )
        db.add_all([session, song])
        db.commit()
        item = QueueItem(session_id=session.id, song_id=song.id)
        db.add(item)
        db.commit()
        actor = {"user": outsider}
        app.dependency_overrides[get_session] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: actor["user"]
        app.dependency_overrides[get_playback_control_service] = lambda: AsyncMock()
        try:
            yield (
                TestClient(app),
                db,
                actor,
                room,
                session,
                song,
                item,
                member,
                outsider,
            )
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize("operation", ["add", "vote", "unvote"])
@pytest.mark.parametrize("state", ["outsider", "other_room", "banned", "kicked"])
def test_nonmembers_cannot_mutate_room_queue(room_api, operation, state):
    """Knowing a session or item ID never grants permission to write."""
    client, db, actor, room, session, song, item, member, outsider = room_api
    if state == "other_room":
        other = Room(host_user_id=outsider.id, room_name="Other", room_code="OTHER1")
        db.add(other)
        db.commit()
        outsider.room_id = other.id
        db.add(outsider)
        db.commit()
    elif state in {"banned", "kicked"}:
        actor["user"] = member
        if state == "banned":
            # Ban must win even before a concurrent eviction clears room_id.
            db.add(RoomBan(room_id=room.id, user_id=member.id))
        else:
            member.room_id = None
            db.add(member)
        db.commit()
    if operation == "add":
        result = client.post(
            "/api/v1/queue/",
            json={
                "session_id": str(session.id),
                "song_id": str(song.id),
            },
        )
    elif operation == "vote":
        result = client.post("/api/v1/queue/vote", json={"queue_item_id": str(item.id)})
    else:
        result = client.delete(
            "/api/v1/queue/vote", params={"queue_item_id": str(item.id)}
        )
    assert result.status_code == 403
    assert len(db.exec(select(QueueItem)).all()) == 1


def test_admitted_member_can_add_and_vote(room_api):
    """The boundary retains normal point-and-vote behavior for actual members."""
    client, _db, actor, _room, session, song, item, member, _outsider = room_api
    actor["user"] = member
    added = client.post(
        "/api/v1/queue/",
        json={
            "session_id": str(session.id),
            "song_id": str(song.id),
        },
    )
    assert added.status_code == 201
    voted = client.post("/api/v1/queue/vote", json={"queue_item_id": str(item.id)})
    assert voted.status_code == 201
