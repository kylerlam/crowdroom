"""Playback sync and Spotify cooldown migrations retain existing runtime data."""

# ruff: noqa: F811

from datetime import datetime, timedelta, timezone
from importlib import import_module
from uuid import uuid4

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, Table, Uuid, inspect, select
from sqlmodel import SQLModel

from backend.db.models import Room, SpotifyAPICooldown, User
from backend.tests.test_guest_migration import migration_database  # noqa: F401


@pytest.mark.parametrize("starting_schema", ["8f21c6d40a72", "create_all"])
def test_playback_migrations_preserve_runtime_state_and_repeat_safely(
    migration_database, starting_schema
):
    engine, config = migration_database
    if starting_schema == "create_all":
        SQLModel.metadata.create_all(engine)
    else:
        command.upgrade(config, starting_schema)
    host_id, guest_id, room_id, session_id = [uuid4() for _ in range(4)]
    now = datetime(2026, 10, 26, 1, 2, 3, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert().values(
                id=host_id,
                username="existing-host",
                email="host@example.com",
                hashed_password="existing-password-hash",
                token_version=7,
                public_id="CR-0000-0001",
            )
        )
        connection.execute(
            User.__table__.insert().values(
                id=guest_id,
                username="existing-guest",
                guest_name="Listener",
                is_guest=True,
                guest_first_joined_at=now - timedelta(hours=1),
                guest_last_requested_at=now,
                public_id="CR-0000-0002",
            )
        )
        connection.execute(
            Room.__table__.insert().values(
                id=room_id,
                host_user_id=host_id,
                room_name="Existing room",
                room_code="ABCDEF",
                settings={"max_members": 50},
            )
        )
        sessions = Table(
            "sessions",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            Column("room_id", Uuid()),
            autoload_with=connection,
        )
        values = dict(
            id=session_id,
            room_id=room_id,
            current_platform="SPOTIFY",
            current_song_id="existing-song",
            last_updated=now,
            playback_status="PLAYING",
            playback_position_ms=42123,
            playback_started_at=now,
            current_device_id="existing-phone",
        )
        if starting_schema == "create_all":
            values["playback_sync_hold_until"] = now + timedelta(seconds=10)
            connection.execute(
                SpotifyAPICooldown.__table__.insert().values(
                    scope="a" * 64,
                    retry_at=now + timedelta(hours=1),
                )
            )
        connection.execute(sessions.insert().values(**values))
        metadata = MetaData()
        tables = [
            Table(name, metadata, autoload_with=connection)
            for name in inspect(connection).get_table_names()
            if name != "alembic_version"
        ]
        before = {
            table.name: list(
                connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings()
            )
            for table in tables
        }

    if starting_schema == "create_all":
        migrations = [
            import_module(f"backend.migrations.versions.{name}")
            for name in (
                "9b06c12e4a31_add_playback_sync_hold",
                "a706e9c2b410_add_spotify_api_cooldowns",
            )
        ]
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                for _ in range(2):
                    for migration in migrations:
                        migration.upgrade()
    else:
        command.upgrade(config, "head")
        command.upgrade(config, "head")

    with engine.connect() as connection:
        after = {
            table.name: list(
                connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings()
            )
            for table in tables
        }
        assert after == before
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("sessions")
        }
        assert columns["playback_sync_hold_until"]["nullable"]
        if starting_schema != "create_all":
            sessions = Table("sessions", MetaData(), autoload_with=connection)
            assert (
                connection.execute(
                    select(sessions.c.playback_sync_hold_until)
                ).scalar_one()
                is None
            )
        cooldown_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("spotify_api_cooldowns")
        }
        assert set(cooldown_columns) == {"scope", "retry_at"}
        assert not cooldown_columns["retry_at"]["nullable"]
        assert inspect(connection).get_pk_constraint("spotify_api_cooldowns")[
            "constrained_columns"
        ] == ["scope"]
