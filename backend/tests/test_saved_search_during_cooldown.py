"""Saved songs remain searchable and queueable while Spotify requests must wait."""

# ruff: noqa: F811

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession
from sqlmodel import select

from backend.api.auth.dependencies import get_current_user
from backend.core.encryption import encrypt_data
from backend.core.spotify_rate_limit import record_rate_limit
from backend.db.database import get_session
from backend.db.models import PlatformConnection, QueueItem, Room, RoomBan, Song, User
from backend.db.models.enum import ItemStatus, StreamingPlatforms
from backend.db.models.session import Session
from backend.main import app
from backend.repositories.song_repo import SongRepository
from backend.tests.test_public_user_ids import identity_engine  # noqa: F401


@pytest.fixture
def saved_search_room(identity_engine, monkeypatch):
    """Use real membership, search and queue routes against private databases."""
    with DBSession(identity_engine) as db:
        users = {
            name: User(
                username=name,
                email=f"{name}@example.com",
                hashed_password="stored-hash",
            )
            for name in ("host", "admin", "test", "outsider")
        }
        users["guest"] = User(
            username="guest_internal", guest_name="Listener", is_guest=True
        )
        db.add_all(users.values())
        db.commit()
        room = Room(
            host_user_id=users["host"].id,
            room_name="Saved catalog",
            room_code="SAVED1",
            settings={"admin_user_ids": [str(users["admin"].id)]},
        )
        db.add(room)
        db.commit()
        for name, user in users.items():
            if name != "outsider":
                user.room_id = room.id
                db.add(user)
        users["guest"].guest_first_joined_at = datetime.now(timezone.utc)
        db.add(users["guest"])
        session = Session(room_id=room.id, playback_status=ItemStatus.STOPPED)
        songs = [
            Song(
                external_id="saved-honey", title="Honey", artist="Artist", duration=180
            ),
            Song(
                external_id="saved-candy",
                title="Candy",
                artist="Honey Voice",
                duration=240,
            ),
        ]
        db.add_all([session, *songs])
        db.add(
            PlatformConnection(
                user_id=users["host"].id,
                platform=StreamingPlatforms.SPOTIFY,
                credentials_encrypted=encrypt_data(
                    {"client_id": "saved-search-app", "client_secret": "dummy"}
                ),
            )
        )
        db.commit()
        record_rate_limit("saved-search-app", "3600")
        state = SimpleNamespace(
            db=db, users=users, room=room, session=session, songs=songs, actor="host"
        )
        previous_overrides = dict(app.dependency_overrides)
        previous_poller = getattr(app.state, "playback_poller", None)
        app.state.playback_poller = AsyncMock()
        app.dependency_overrides[get_session] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: users[state.actor]
        client = AsyncMock()
        client.__aenter__.return_value = client
        monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
        with patch(
            "backend.api.websocket.manager.broadcast", new=AsyncMock()
        ) as broadcast:
            state.client = TestClient(app)
            state.network = client
            state.broadcast = broadcast
            try:
                yield state
            finally:
                state.client.close()
                app.dependency_overrides.clear()
                app.dependency_overrides.update(previous_overrides)
                app.state.playback_poller = previous_poller


@pytest.mark.parametrize("actor", ["host", "admin", "test"])
def test_registered_roles_can_search_and_queue_saved_songs_without_guest_limits(
    saved_search_room, actor
):
    state = saved_search_room
    state.actor = actor
    response = state.client.get(
        "/api/v1/search/", params={"room_id": str(state.room.id), "q": "hOnEy"}
    )
    assert response.status_code == 200
    assert {track["external_id"] for track in response.json()} == {
        "saved-honey",
        "saved-candy",
    }
    assert response.json()[0]["duration_ms"] == 240000
    assert any(
        call.args[0]["type"] == "spotify_rate_limited"
        for call in state.broadcast.call_args_list
    )
    for _ in range(3):
        result = state.client.post(
            "/api/v1/queue/",
            json={
                "session_id": str(state.session.id),
                "song_id": str(state.songs[0].id),
            },
        )
        assert result.status_code == 201
    assert len(state.db.exec(select(QueueItem)).all()) == 3
    assert state.users[actor].guest_last_requested_at is None
    for method in (state.network.get, state.network.post, state.network.put):
        method.assert_not_awaited()


def test_saved_metadata_is_returned_without_spotify_and_unknown_track_keeps_429(
    saved_search_room,
):
    state = saved_search_room
    response = state.client.get(
        "/api/v1/search/saved-honey", params={"room_id": str(state.room.id)}
    )
    assert response.status_code == 200
    assert response.json()["duration_ms"] == 180000
    unknown = state.client.get(
        "/api/v1/search/new-track", params={"room_id": str(state.room.id)}
    )
    assert unknown.status_code == 429
    assert unknown.json()["code"] == "SPOTIFY_RATE_LIMITED"
    state.network.get.assert_not_awaited()
    state.network.post.assert_not_awaited()


def test_guest_still_waits_five_minutes_and_half_hour_with_one_pending_song(
    saved_search_room,
):
    state = saved_search_room
    state.actor = "guest"
    body = {"session_id": str(state.session.id), "song_id": str(state.songs[0].id)}
    assert (
        state.client.get(
            "/api/v1/search/", params={"room_id": str(state.room.id), "q": "Honey"}
        ).status_code
        == 200
    )
    assert state.client.post("/api/v1/queue/", json=body).status_code == 429
    guest = state.users["guest"]
    guest.guest_first_joined_at -= timedelta(minutes=6)
    state.db.add(guest)
    state.db.commit()
    assert state.client.post("/api/v1/queue/", json=body).status_code == 201
    assert state.client.post("/api/v1/queue/", json=body).status_code == 429
    guest.guest_last_requested_at -= timedelta(minutes=31)
    state.db.add(guest)
    state.db.commit()
    pending = state.client.post("/api/v1/queue/", json=body)
    assert pending.status_code == 403
    assert "already have a song waiting" in pending.json()["detail"]
    state.network.get.assert_not_awaited()
    state.network.post.assert_not_awaited()


@pytest.mark.parametrize("state_name", ["outsider", "banned"])
@pytest.mark.parametrize("path", ["/search/", "/search/saved-honey"])
def test_saved_search_and_metadata_remain_room_member_only(
    saved_search_room, state_name, path
):
    state = saved_search_room
    if state_name == "outsider":
        state.actor = "outsider"
    else:
        state.actor = "test"
        state.db.add(RoomBan(room_id=state.room.id, user_id=state.users["test"].id))
        state.db.commit()
    response = state.client.get(
        f"/api/v1{path}", params={"room_id": str(state.room.id), "q": "Honey"}
    )
    assert response.status_code == 403
    state.network.get.assert_not_awaited()
    state.network.post.assert_not_awaited()


def test_saved_search_treats_wildcards_literally_and_limits_results(saved_search_room):
    state = saved_search_room
    state.db.add_all(
        [
            Song(
                external_id=f"literal-{number}",
                title=f"100%_ {number:02}",
                artist="Literal",
                duration=180,
            )
            for number in range(12)
        ]
    )
    state.db.commit()
    repo = SongRepository(state.db)
    result = repo.search_saved("%_", "spotify")
    assert len(result) == 10
    assert all("%_" in song.title for song in result)
    assert repo.search_saved("   ", "spotify") == []
