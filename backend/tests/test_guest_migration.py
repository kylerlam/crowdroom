"""Run the real migration chain for new and existing guest-enabled databases."""

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Column, MetaData, Table, Uuid, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import Session as DBSession
from sqlmodel import create_engine

from backend.core.config import settings
from backend.db.models import (
    PlatformConnection,
    QueueItem,
    Room,
    RoomAdmission,
    Song,
    User,
)
from backend.db.models.enum import ConnectionType, ItemStatus, StreamingPlatforms
from backend.db.models.session import Session


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_database(request, tmp_path, monkeypatch):
    """Run Alembic against a private file or a disposable PostgreSQL schema."""
    base = None
    schema = None
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'guest-migration.db'}"
    else:
        raw_url = os.environ.get("CROWDROOM_TEST_POSTGRES_URL")
        if not raw_url:
            pytest.skip("CROWDROOM_TEST_POSTGRES_URL not set")
        schema = "guest_migration_" + uuid4().hex
        base = create_engine(raw_url)
        with base.begin() as connection:
            connection.execute(CreateSchema(schema))
        url = (
            make_url(raw_url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
    engine = create_engine(url)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        yield engine, config
    finally:
        engine.dispose()
        if base is not None:
            with base.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            base.dispose()


def test_empty_database_migrates_to_guest_schema_and_accepts_guest(migration_database):
    engine, config = migration_database
    command.upgrade(config, "head")
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    assert {
        "is_guest",
        "guest_name",
        "guest_first_joined_at",
        "guest_last_requested_at",
    } <= columns
    with DBSession(engine) as db:
        guest = User(username="guest_random", is_guest=True, guest_name="Listener")
        db.add(guest)
        db.commit()
        db.refresh(guest)
        assert guest.is_guest
        assert guest.display_name == "Listener"
        assert guest.public_id.startswith("CR-")
    command.upgrade(config, "head")
    with DBSession(engine) as db:
        assert db.get(User, guest.id).guest_name == "Listener"


def test_upgrade_from_public_ids_preserves_accounts_room_queue_and_oauth(
    migration_database,
):
    engine, config = migration_database
    command.upgrade(config, "7e8a41c9b203")
    host_id, member_id, room_id, session_id, song_id = [uuid4() for _ in range(5)]
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    with engine.begin() as connection:
        users = Table(
            "users",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            Column("room_id", Uuid()),
            autoload_with=connection,
        )
        connection.execute(
            users.insert(),
            [
                {
                    "id": host_id,
                    "username": "host",
                    "email": "host@example.com",
                    "hashed_password": "existing-password-hash",
                    "token_version": 7,
                    "public_id": "CR-0000-0001",
                },
                {
                    "id": member_id,
                    "username": "legacy",
                    "email": None,
                    "hashed_password": None,
                    "token_version": 2,
                    "public_id": "CR-0000-0002",
                },
            ],
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
        connection.execute(
            users.update()
            .where(users.c.id == host_id)
            .values(
                room_id=room_id,
            )
        )
        connection.execute(
            Session.__table__.insert().values(
                id=session_id,
                room_id=room_id,
                current_platform=StreamingPlatforms.SPOTIFY,
                last_updated=now,
                playback_status=ItemStatus.PAUSED,
                playback_position_ms=42123,
                current_device_id="existing-phone",
            )
        )
        connection.execute(
            Song.__table__.insert().values(
                id=song_id,
                external_id="existing-track",
                title="Existing song",
                artist="Artist",
                duration=240,
                platform=StreamingPlatforms.SPOTIFY,
            )
        )
        connection.execute(
            QueueItem.__table__.insert().values(
                session_id=session_id,
                song_id=song_id,
                added_by_user_id=host_id,
                group="manual",
                position=0,
                is_pinned=True,
                priority_position=-3,
            )
        )
        connection.execute(
            RoomAdmission.__table__.insert().values(
                room_id=room_id,
                user_id=member_id,
                status="rejected",
                created_at=now,
            )
        )
        connection.execute(
            PlatformConnection.__table__.insert().values(
                user_id=host_id,
                platform=StreamingPlatforms.SPOTIFY,
                connection_type=ConnectionType.AUTHORIZATION_CODE,
                access_token_encrypted="existing-encrypted-access",
                refresh_token_encrypted="existing-encrypted-refresh",
                token_expires_at=now,
                scopes="user-modify-playback-state",
            )
        )
        # Reflect the old schema: current models may gain columns in later revisions.
        metadata = MetaData()
        tables = [
            Table(name, metadata, autoload_with=connection)
            for name in (
                "users",
                "rooms",
                "sessions",
                "songs",
                "queue_items",
                "room_admissions",
                "platform_connections",
            )
        ]
        before = {
            table.name: list(
                connection.execute(select(table).order_by(table.c.id)).mappings()
            )
            for table in tables
        }
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        after = {
            table.name: list(
                connection.execute(select(table).order_by(table.c.id)).mappings()
            )
            for table in tables
        }
        assert after == before
    with DBSession(engine) as db:
        for user_id in (host_id, member_id):
            user = db.get(User, user_id)
            assert user.is_guest is False
            assert user.guest_name is None
            assert user.guest_first_joined_at is None
            assert user.guest_last_requested_at is None
