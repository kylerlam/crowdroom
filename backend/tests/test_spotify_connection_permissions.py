"""Only hosts and users outside rooms may change their Spotify account."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from backend.api.auth.dependencies import get_current_user, get_current_user_unchecked
from backend.core.encryption import encrypt_data
from backend.core.exceptions import ForbiddenException
from backend.db.database import get_session
from backend.db.models import PlatformConnection, Room, User
from backend.db.models.enum import ConnectionType, StreamingPlatforms
from backend.main import app
from backend.repositories.platform_connection_repo import PlatformConnectionRepo
from backend.schemas.platform_connection import (
    StoreOAuthTokens,
)
from backend.services.platform_connection_service import PlatformConnectionService
from backend.services.spotify_oauth_service import SpotifyOAuthService


@pytest.fixture
def connection_api():
    """Use real connection storage to verify role changes preserve credentials."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        host, admin, member, unattached = [
            User(username=n) for n in ("host", "admin", "member", "unattached")
        ]
        db.add_all([host, admin, member, unattached])
        db.commit()
        room = Room(
            host_user_id=host.id,
            room_name="Room",
            room_code="SPOT01",
            settings={"admin_user_ids": [str(admin.id)]},
        )
        db.add(room)
        db.commit()
        for user in (host, admin, member):
            user.room_id = room.id
            db.add(user)
        for user in (host, admin, member, unattached):
            db.add(
                PlatformConnection(
                    user_id=user.id,
                    platform=StreamingPlatforms.SPOTIFY,
                    connection_type=ConnectionType.AUTHORIZATION_CODE,
                    credentials_encrypted=encrypt_data(
                        {"client_id": "app", "client_secret": "test-secret"}
                    ),
                    access_token_encrypted=encrypt_data({"access_token": "access"}),
                    refresh_token_encrypted=encrypt_data({"refresh_token": "refresh"}),
                    token_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(hours=1),
                )
            )
        db.commit()
        actor = {"user": host}
        app.dependency_overrides[get_session] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: actor["user"]
        app.dependency_overrides[get_current_user_unchecked] = lambda: actor["user"]
        try:
            yield TestClient(app), db, actor, host, admin, member, unattached
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


@pytest.mark.parametrize("role", ["host", "admin", "member", "unattached"])
def test_connection_endpoints_enforce_current_room_role(connection_api, role):
    """An administrator cannot start OAuth or delete even their own saved account."""
    client, db, actor, host, admin, member, unattached = connection_api
    user = {"host": host, "admin": admin, "member": member, "unattached": unattached}[
        role
    ]
    actor["user"] = user
    allowed = role in {"host", "unattached"}
    assert client.get("/api/v1/platform-connections/spotify/access").json() == {
        "can_manage": allowed
    }
    start = client.post("/api/v1/auth/spotify/start")
    assert start.status_code == (200 if allowed else 403)
    original = PlatformConnectionRepo(db).get_by_user_and_platform(
        user.id, StreamingPlatforms.SPOTIFY
    )
    if not allowed:
        with patch(
            "backend.services.platform_connection_service.SpotifyAuthAdapter.validate_credentials",
            new=AsyncMock(),
        ) as validate:
            saved = client.post(
                "/api/v1/platform-connections/",
                json={
                    "platform": "spotify",
                    "credentials": {"client_id": "app", "client_secret": "replacement"},
                },
            )
            assert saved.status_code == 403
            validate.assert_not_awaited()
    disconnected = client.delete("/api/v1/platform-connections/spotify")
    assert disconnected.status_code == (204 if allowed else 403)
    if not allowed:
        assert (
            PlatformConnectionRepo(db)
            .get_by_user_and_platform(user.id, StreamingPlatforms.SPOTIFY)
            .refresh_token_encrypted
            == original.refresh_token_encrypted
        )


def test_oauth_callback_rechecks_role_after_authorization_started(connection_api):
    """A callback begun before joining another host's room cannot bypass the gate."""
    _client, db, _actor, host, _admin, _member, unattached = connection_api
    service = PlatformConnectionService(PlatformConnectionRepo(db))
    state = encrypt_data({"user_id": str(unattached.id), "code_verifier": "verifier"})
    unattached.room_id = host.room_id
    db.add(unattached)
    db.commit()
    oauth = SpotifyOAuthService(service)
    with patch.object(oauth, "_request_tokens", new=AsyncMock()) as request:

        async def run():
            await oauth.exchange_code_for_tokens("code", state)

        with pytest.raises(ForbiddenException):
            anyio.run(run)
        request.assert_not_awaited()
    tokens = StoreOAuthTokens(
        access_token="new",
        refresh_token="new",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes="user-modify-playback-state",
    )
    with pytest.raises(ForbiddenException):
        service.store_oauth_tokens(unattached.id, StreamingPlatforms.SPOTIFY, tokens)
