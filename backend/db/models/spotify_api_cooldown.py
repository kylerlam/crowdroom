"""Persist Spotify app-wide API cooldowns across requests and restarts."""

from datetime import datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class SpotifyAPICooldown(SQLModel, table=True):
    """An opaque app scope and Spotify's latest permitted retry time."""

    __tablename__ = "spotify_api_cooldowns"

    scope: str = Field(primary_key=True, max_length=64)
    retry_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
