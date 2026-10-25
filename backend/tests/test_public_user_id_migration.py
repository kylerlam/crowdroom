"""Backfill readable IDs while preserving accounts, UUIDs and room approval."""

# Imported fixture is injected into test parameters by pytest.
# ruff: noqa: F811

from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, Table, Uuid, inspect, select
from sqlalchemy.exc import IntegrityError

from backend.db.models import Room, RoomAdmission
from backend.tests.test_public_user_ids import identity_engine  # noqa: F401


def test_migration_backfills_once_resolves_collision_and_preserves_existing_data(
    identity_engine,
):
    migration = import_module(
        "backend.migrations.versions.7e8a41c9b203_add_public_user_ids"
    )
    with identity_engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        old_users = Table(
            "users",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            Column("room_id", Uuid()),
            autoload_with=connection,
        )
        host_id, member_id, room_id = uuid4(), uuid4(), uuid4()
        connection.execute(
            old_users.insert(),
            [
                {
                    "id": host_id,
                    "username": "host",
                    "hashed_password": "existing-host-hash",
                    "token_version": 4,
                },
                {
                    "id": member_id,
                    "username": "member",
                    "hashed_password": "existing-member-hash",
                    "token_version": 2,
                },
            ],
        )
        connection.execute(
            Room.__table__.insert().values(
                id=room_id,
                room_name="Existing",
                room_code="ABCDEF",
                host_user_id=host_id,
                settings={},
            )
        )
        connection.execute(old_users.update().values(room_id=room_id))
        connection.execute(
            RoomAdmission.__table__.insert().values(
                room_id=room_id, user_id=member_id, status="approved"
            )
        )
        before = list(
            connection.execute(select(old_users).order_by(old_users.c.id)).mappings()
        )
        with Operations.context(MigrationContext.configure(connection)):
            with patch.object(migration.secrets, "randbelow", side_effect=[1, 1, 2]):
                migration.upgrade()
            users = Table(
                "users",
                MetaData(),
                Column("id", Uuid(), primary_key=True),
                autoload_with=connection,
            )
            ids = dict(connection.execute(select(users.c.id, users.c.public_id)).all())
            assert set(ids.values()) == {"CR-0000-0001", "CR-0000-0002"}
            migration.upgrade()
            assert (
                dict(connection.execute(select(users.c.id, users.c.public_id)).all())
                == ids
            )
            after = list(
                connection.execute(
                    select(old_users).order_by(old_users.c.id)
                ).mappings()
            )
            assert before == after
            assert (
                connection.execute(select(RoomAdmission.status)).scalar_one()
                == "approved"
            )
            assert not next(
                column
                for column in inspect(connection).get_columns("users")
                if column["name"] == "public_id"
            )["nullable"]
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    users.insert().values(
                        id=uuid4(),
                        username="duplicate-id",
                        token_version=0,
                        public_id=next(iter(ids.values())),
                    )
                )
            migration.downgrade()
            assert "public_id" not in {
                column["name"] for column in inspect(connection).get_columns("users")
            }
            assert (
                list(
                    connection.execute(
                        select(old_users).order_by(old_users.c.id)
                    ).mappings()
                )
                == before
            )
            assert (
                connection.execute(select(RoomAdmission.status)).scalar_one()
                == "approved"
            )


def test_migration_is_safe_when_create_all_already_installed_the_column(
    identity_engine,
):
    migration = import_module(
        "backend.migrations.versions.7e8a41c9b203_add_public_user_ids"
    )
    with identity_engine.begin() as connection:
        users = Table(
            "users",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            autoload_with=connection,
        )
        user_id = uuid4()
        connection.execute(
            users.insert().values(
                id=user_id,
                username="already-new",
                token_version=0,
                public_id="CR-4827-9163",
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert (
            connection.execute(
                select(users.c.public_id).where(users.c.id == user_id)
            ).scalar_one()
            == "CR-4827-9163"
        )
