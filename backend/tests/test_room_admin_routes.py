"""API regression tests preventing playback and room mutation permission bypasses."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api.auth.dependencies import get_current_user
from backend.api.playback.dependencies import get_playback_control_service
from backend.api.rooms.dependencies import get_room_service
from backend.api.rooms.router import router as rooms_router
from backend.api.session.dependencies import get_playback_service, get_session_service
from backend.api.session.router import router as sessions_router
from backend.core.exceptions import ForbiddenException
from backend.core.security import SecurityService
from backend.db.models.enum import ItemStatus, StreamingPlatforms
from backend.db.models.room import Room
from backend.db.models.session import Session
from backend.db.models.user import User
from backend.repositories.room_repo import RoomRepository
from backend.services.room_service import RoomService


@pytest.fixture
def role_client():
    room_id = uuid4()
    host = User(username="host", room_id=room_id)
    admin = User(username="admin", room_id=room_id)
    member = User(username="member", room_id=room_id)
    room = Room(
        id=room_id,
        host_user_id=host.id,
        room_name="Room",
        room_code="ABC123",
        settings={"admin_user_ids": [str(admin.id)]},
    )
    room.users = [host, admin, member]
    room_repo = MagicMock(spec=RoomRepository)
    room_repo.get_by_id.return_value = room
    room_repo.update.return_value = room
    room_service = RoomService(room_repo, MagicMock(spec=SecurityService))
    session = Session(room_id=room_id, current_platform=StreamingPlatforms.SPOTIFY)
    session_service = MagicMock()
    session_service.get_session.return_value = session
    session_service.create_session.return_value = session
    session_service.update_session.return_value = session
    playback_service = MagicMock()
    playback_service.finish_song = AsyncMock(return_value=ItemStatus.STOPPED)
    actor = {"user": member}
    app = FastAPI()
    app.include_router(rooms_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    app.state.playback_poller = MagicMock(stop_polling=AsyncMock())
    app.state.playback_control = MagicMock(start_if_idle=AsyncMock())
    app.dependency_overrides[get_current_user] = lambda: actor["user"]
    app.dependency_overrides[get_room_service] = lambda: room_service
    app.dependency_overrides[get_session_service] = lambda: session_service
    app.dependency_overrides[get_playback_service] = lambda: playback_service
    app.dependency_overrides[get_playback_control_service] = lambda: (
        app.state.playback_control
    )

    @app.exception_handler(ForbiddenException)
    async def forbidden_handler(_request, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return TestClient(app), actor, room, session, host, admin, member


@pytest.mark.parametrize("role", ["host", "admin", "member"])
def test_only_owner_can_assign_administrators(role_client, role):
    client, actor, room, _session, host, admin, member = role_client
    actor["user"] = {"host": host, "admin": admin, "member": member}[role]

    response = client.patch(
        f"/api/v1/rooms/{room.id}/admins/{member.id}", json={"is_admin": True}
    )

    assert response.status_code == (200 if role == "host" else 403)


@pytest.mark.parametrize("method", ["patch", "delete"])
@pytest.mark.parametrize("role", ["host", "admin", "member"])
def test_room_settings_and_deletion_remain_owner_only(role_client, method, role):
    client, actor, room, _session, host, admin, member = role_client
    actor["user"] = {"host": host, "admin": admin, "member": member}[role]
    kwargs = {"json": {"room_name": "Updated"}} if method == "patch" else {}

    response = getattr(client, method)(f"/api/v1/rooms/{room.id}", **kwargs)

    allowed_status = 200 if method == "patch" else 204
    assert response.status_code == (allowed_status if role == "host" else 403)


@pytest.mark.parametrize("operation", ["create", "update", "delete", "finish"])
@pytest.mark.parametrize("role", ["host", "admin", "member"])
def test_session_mutations_require_room_management(role_client, operation, role):
    client, actor, room, session, host, admin, member = role_client
    actor["user"] = {"host": host, "admin": admin, "member": member}[role]
    url = f"/api/v1/session/{session.id}"
    if operation == "create":
        response = client.post(
            "/api/v1/session/",
            json={"room_id": str(room.id), "current_platform": "spotify"},
        )
        allowed_status = 201
    elif operation == "update":
        response = client.patch(url, json={"playback_status": "paused"})
        allowed_status = 200
    elif operation == "delete":
        response = client.delete(url)
        allowed_status = 204
    else:
        response = client.post(f"{url}/finish")
        allowed_status = 200

    assert response.status_code == (403 if role == "member" else allowed_status)


def test_session_finish_rejects_unauthenticated_request(role_client):
    client, _actor, _room, session, _host, _admin, _member = role_client
    del client.app.dependency_overrides[get_current_user]

    response = client.post(f"/api/v1/session/{session.id}/finish")

    assert response.status_code == 401


@pytest.mark.parametrize("role", ["host", "admin", "member"])
def test_finish_route_starts_next_song_on_server_only_after_authorized_finish(
    role_client, role
):
    client, actor, _room, session, host, admin, member = role_client
    actor["user"] = {"host": host, "admin": admin, "member": member}[role]
    transitions = []

    async def finish(session_id):
        assert session_id == session.id
        transitions.append("finished")
        return "finished"

    async def start(session_id):
        assert session_id == session.id
        transitions.append("started")

    playback_service = client.app.dependency_overrides[get_playback_service]()
    playback_service.finish_song.side_effect = finish
    client.app.state.playback_control.start_if_idle.side_effect = start

    response = client.post(f"/api/v1/session/{session.id}/finish")

    if role == "member":
        assert response.status_code == 403
        assert transitions == []
    else:
        assert response.status_code == 200
        assert response.json() == {"status": "finished"}
        assert transitions == ["finished", "started"]
