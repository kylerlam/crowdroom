"""Distinguish Spotify application credentials from user playback authorization."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine

from backend.api.auth.dependencies import get_current_user
from backend.api.playback.dependencies import get_playback_control_service
from backend.core.config import settings
from backend.core.encryption import encrypt_data
from backend.core.exceptions import SpotifyConnectionRequiredException
from backend.db.models import PlatformConnection, User
from backend.db.models.enum import ConnectionType, StreamingPlatforms
from backend.main import app
from backend.repositories.platform_connection_repo import PlatformConnectionRepo
from backend.services.platform_connection_service import PlatformConnectionService
from backend.services.spotify_oauth_service import SpotifyOAuthService


@pytest.mark.parametrize("method", ["get_valid_access_token", "force_refresh_token"])
@pytest.mark.parametrize(
    "state", ["missing", "app_only", "missing_access", "missing_refresh"]
)
def test_playback_requires_complete_user_oauth(method, state):
    """App credentials and incomplete OAuth records must prompt a connection."""
    user_id = uuid4()
    connection = None
    if state != "missing":
        connection = PlatformConnection(
            user_id=user_id,
            platform=StreamingPlatforms.SPOTIFY,
            connection_type=(
                ConnectionType.CLIENT_CREDENTIALS
                if state == "app_only"
                else ConnectionType.AUTHORIZATION_CODE
            ),
            access_token_encrypted=None if state == "missing_access" else "access",
            refresh_token_encrypted=None if state == "missing_refresh" else "refresh",
        )
    repo = MagicMock(spec=PlatformConnectionRepo)
    repo.get_by_user_and_platform.return_value = connection
    service = PlatformConnectionService(repo)

    async def run():
        await getattr(service, method)(user_id, StreamingPlatforms.SPOTIFY)

    with pytest.raises(
        SpotifyConnectionRequiredException, match="connect Spotify"
    ) as error:
        anyio.run(run)
    assert str(user_id) not in str(error.value)


def test_configured_app_supports_oauth_without_storing_secrets_for_each_user():
    """The configured server app completes OAuth for a new user's own connection."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        user = User(username="spotify-global-app-user")
        db.add(user)
        db.commit()
        connections = PlatformConnectionService(PlatformConnectionRepo(db))
        oauth = SpotifyOAuthService(connections)
        with (
            patch.object(settings, "SPOTIFY_CLIENT_ID", "configured-app-id"),
            patch.object(settings, "SPOTIFY_CLIENT_SECRET", "configured-app-secret"),
            patch.object(oauth, "_request_tokens", new=AsyncMock()) as request_tokens,
        ):
            request_tokens.return_value = {
                "access_token": "user-access",
                "refresh_token": "user-refresh",
                "expires_in": 3600,
            }
            url = oauth.generate_authorization_url(user.id)
            parameters = parse_qs(urlparse(url).query)
            assert parameters["client_id"] == ["configured-app-id"]
            assert "configured-app-secret" not in url

            async def run():
                await oauth.exchange_code_for_tokens(
                    "user-code", parameters["state"][0]
                )
                return await connections.get_valid_access_token(
                    user.id, StreamingPlatforms.SPOTIFY
                )

            assert anyio.run(run) == "user-access"
            stored = connections.get_connections(user.id)
            assert len(stored) == 1
            assert stored[0].connection_type == ConnectionType.AUTHORIZATION_CODE
            assert stored[0].credentials_encrypted is None
            assert request_tokens.call_args.args[-2:] == (
                "configured-app-id",
                "configured-app-secret",
            )
    engine.dispose()


def test_personal_app_credentials_take_precedence_over_server_configuration():
    """Existing users continue authorizing against their own configured app."""
    repo = MagicMock(spec=PlatformConnectionRepo)
    repo.get_by_user_and_platform.return_value = PlatformConnection(
        user_id=uuid4(),
        platform=StreamingPlatforms.SPOTIFY,
        credentials_encrypted=encrypt_data(
            {"client_id": "personal-id", "client_secret": "personal-secret"}
        ),
    )
    with (
        patch.object(settings, "SPOTIFY_CLIENT_ID", "server-id"),
        patch.object(settings, "SPOTIFY_CLIENT_SECRET", "server-secret"),
    ):
        assert PlatformConnectionService(repo).get_spotify_app_credentials(uuid4()) == {
            "client_id": "personal-id",
            "client_secret": "personal-secret",
        }


def test_no_app_credentials_remains_unconfigured():
    """A missing server configuration cannot produce an unusable OAuth URL."""
    repo = MagicMock(spec=PlatformConnectionRepo)
    repo.get_by_user_and_platform.return_value = None
    with (
        patch.object(settings, "SPOTIFY_CLIENT_ID", ""),
        patch.object(settings, "SPOTIFY_CLIENT_SECRET", ""),
    ):
        assert (
            PlatformConnectionService(repo).get_spotify_app_credentials(uuid4()) is None
        )


def test_revoked_refresh_authorization_requires_reconnect():
    """Spotify's refresh rejection returns the same actionable connection error."""
    connection = PlatformConnection(
        user_id=uuid4(),
        platform=StreamingPlatforms.SPOTIFY,
        connection_type=ConnectionType.AUTHORIZATION_CODE,
        access_token_encrypted=encrypt_data({"access_token": "expired-access"}),
        refresh_token_encrypted=encrypt_data({"refresh_token": "revoked-refresh"}),
        token_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(hours=1),
    )
    repo = MagicMock(spec=PlatformConnectionRepo)
    repo.get_by_user_and_platform.return_value = connection
    service = PlatformConnectionService(repo)
    client = AsyncMock()
    client.post.return_value = MagicMock(status_code=400)
    with patch("httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value = client

        async def run():
            await service.get_valid_access_token(
                connection.user_id, StreamingPlatforms.SPOTIFY
            )

        with pytest.raises(SpotifyConnectionRequiredException):
            anyio.run(run)


def test_playback_route_returns_connection_action_instead_of_database_error():
    """The real API translates missing OAuth into a stable code and clear next step."""
    user = User(id=uuid4(), username="host", room_id=uuid4())
    service = MagicMock()
    service.play = AsyncMock(side_effect=SpotifyConnectionRequiredException())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_playback_control_service] = lambda: service
    try:
        response = TestClient(app).post("/api/v1/playback/play", json={})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["code"] == "SPOTIFY_AUTH_REQUIRED"
    assert "connect Spotify in Profile" in response.json()["detail"]
    assert "Entity" not in response.json()["detail"]
