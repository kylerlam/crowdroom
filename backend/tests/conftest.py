"""Isolate Spotify cooldown storage from the deployment database in every test."""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from backend.core import spotify_rate_limit
from backend.db.models.spotify_api_cooldown import SpotifyAPICooldown


@pytest.fixture(autouse=True)
def isolated_spotify_cooldown(monkeypatch):
    """Let existing HTTP adapter mocks use a real, private cooldown database."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SpotifyAPICooldown.__table__.create(engine)
    monkeypatch.setattr(spotify_rate_limit, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()
