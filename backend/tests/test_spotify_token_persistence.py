"""Keep Spotify authorization through expiry, rotation, and app restarts."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.core.encryption import decrypt_data, encrypt_data
from backend.core.exceptions import (
    InvalidPlatformCredentialsException,
    SpotifyConnectionRequiredException,
    SpotifyUpstreamException,
)
from backend.db.models import PlatformConnection, User
from backend.db.models.enum import ConnectionType, StreamingPlatforms
from backend.repositories.platform_connection_repo import PlatformConnectionRepo
from backend.schemas.platform_connection import CreatePlatformConnection
from backend.services.platform_connection_service import PlatformConnectionService


@pytest.fixture
def stored_connection(tmp_path):
    """Use a real persisted connection shared by independent request sessions."""
    engine = create_engine(f"sqlite:///{tmp_path / 'tokens.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="persistent-spotify-host")
        db.add(user)
        db.commit()
        user_id = user.id
        db.add(
            PlatformConnection(
                user_id=user_id,
                platform=StreamingPlatforms.SPOTIFY,
                connection_type=ConnectionType.AUTHORIZATION_CODE,
                credentials_encrypted=encrypt_data(
                    {
                        "client_id": "test-app",
                        "client_secret": "test-secret",
                    }
                ),
                access_token_encrypted=encrypt_data({"access_token": "expired"}),
                refresh_token_encrypted=encrypt_data(
                    {"refresh_token": "original-refresh"}
                ),
                token_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1),
            )
        )
        db.commit()
    yield engine, user_id
    engine.dispose()


def test_concurrent_refresh_rotates_once_and_survives_new_sessions(stored_connection):
    """Overlapping requests reuse the renewed token instead of racing rotation."""
    engine, user_id = stored_connection
    with Session(engine) as first_db, Session(engine) as second_db:
        services = [
            PlatformConnectionService(PlatformConnectionRepo(db))
            for db in (first_db, second_db)
        ]
        # Prime both identity maps with the old token, as long-running polls can do.
        stale = [service.get_connections(user_id)[0] for service in services]
        assert all(connection.token_expires_at for connection in stale)
        client = AsyncMock()

        async def renew(**kwargs):
            assert kwargs["data"]["refresh_token"] == "original-refresh"
            await anyio.sleep(0)
            return httpx.Response(
                200,
                request=httpx.Request("POST", "https://accounts.spotify.com/api/token"),
                json={
                    "access_token": "renewed",
                    "expires_in": 3600,
                    "refresh_token": "rotated-refresh",
                },
            )

        client.post.side_effect = renew
        with patch("httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client

            async def run():
                return await asyncio.gather(
                    *(
                        service.get_valid_access_token(
                            user_id,
                            StreamingPlatforms.SPOTIFY,
                        )
                        for service in services
                    )
                )

            assert anyio.run(run) == ["renewed", "renewed"]
        assert client.post.await_count == 1

    with Session(engine) as restarted_db:
        service = PlatformConnectionService(PlatformConnectionRepo(restarted_db))
        assert service.is_spotify_authorized(user_id)
        connection = service.get_connections(user_id)[0]
        assert decrypt_data(connection.refresh_token_encrypted) == {
            "refresh_token": "rotated-refresh",
        }
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            200,
            json={
                "access_token": "renewed-again",
                "expires_in": 3600,
            },
        )
        with patch("httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client

            async def run_again():
                return await service.force_refresh_token(
                    user_id, StreamingPlatforms.SPOTIFY
                )

            assert anyio.run(run_again) == "renewed-again"
        assert (
            client.post.call_args.kwargs["data"]["refresh_token"] == "rotated-refresh"
        )
        assert (
            decrypt_data(connection.refresh_token_encrypted)["refresh_token"]
            == "rotated-refresh"
        )


@pytest.mark.parametrize("new_id", ["test-app", "different-app"])
def test_only_changing_app_identity_clears_authorization(stored_connection, new_id):
    """Updating the same app secret preserves long-lived user authorization."""
    engine, user_id = stored_connection
    with Session(engine) as db:
        service = PlatformConnectionService(PlatformConnectionRepo(db))
        data = CreatePlatformConnection(
            platform=StreamingPlatforms.SPOTIFY,
            credentials={
                "client_id": new_id,
                "client_secret": "updated-test-secret",
            },
        )
        with patch(
            "backend.services.platform_connection_service.SpotifyAuthAdapter.validate_credentials",
            new=AsyncMock(),
        ):

            async def run():
                await service.connect(user_id, data)

            anyio.run(run)
        assert service.is_spotify_authorized(user_id) == (new_id == "test-app")


@pytest.mark.parametrize(
    "status,error,exception",
    [
        (400, "invalid_grant", SpotifyConnectionRequiredException),
        (401, "invalid_client", InvalidPlatformCredentialsException),
        (429, "rate_limit", SpotifyUpstreamException),
        (503, "temporarily_unavailable", SpotifyUpstreamException),
    ],
)
def test_only_revocation_discards_saved_authorization(
    stored_connection, status, error, exception
):
    """Temporary Spotify failures must not force users to authorize again."""
    engine, user_id = stored_connection
    with Session(engine) as db:
        service = PlatformConnectionService(PlatformConnectionRepo(db))
        client = AsyncMock()
        client.post.return_value = httpx.Response(status, json={"error": error})
        with patch("httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client

            async def run():
                await service.get_valid_access_token(
                    user_id, StreamingPlatforms.SPOTIFY
                )

            with pytest.raises(exception):
                anyio.run(run)
        assert service.is_spotify_authorized(user_id) == (error != "invalid_grant")
    with Session(engine) as restarted_db:
        assert PlatformConnectionService(
            PlatformConnectionRepo(restarted_db)
        ).is_spotify_authorized(user_id) == (error != "invalid_grant")
