"""Guest identity migration preserves existing accounts and persistent deadlines."""

# ruff: noqa: F811

from datetime import datetime, timezone
from importlib import import_module
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, Table, Uuid, inspect, select

from backend.db.models import User
from backend.tests.test_public_user_ids import identity_engine  # noqa: F401


def test_guest_migration_keeps_legacy_accounts_and_is_idempotent(identity_engine):
    migration = import_module(
        "backend.migrations.versions.8f21c6d40a72_add_guest_identities"
    )
    with identity_engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        users = Table(
            "users",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            autoload_with=connection,
        )
        connection.execute(
            users.insert(),
            [
                {
                    "id": uuid4(),
                    "username": "host",
                    "hashed_password": "stored-hash",
                    "token_version": 3,
                    "public_id": "CR-0000-0001",
                },
                {
                    "id": uuid4(),
                    "username": "legacy",
                    "hashed_password": None,
                    "token_version": 0,
                    "public_id": "CR-0000-0002",
                },
            ],
        )
        before = list(connection.execute(select(users).order_by(users.c.id)).mappings())
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        after = list(connection.execute(select(users).order_by(users.c.id)).mappings())
        assert after == before
        upgraded = Table(
            "users",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            autoload_with=connection,
        )
        assert all(not row.is_guest for row in connection.execute(select(upgraded)))
        assert not next(
            column
            for column in inspect(connection).get_columns("users")
            if column["name"] == "is_guest"
        )["nullable"]
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert (
            list(connection.execute(select(users).order_by(users.c.id)).mappings())
            == before
        )


def test_guest_migration_preserves_create_all_guests_and_deadlines(identity_engine):
    migration = import_module(
        "backend.migrations.versions.8f21c6d40a72_add_guest_identities"
    )
    joined = datetime.now(timezone.utc)
    with identity_engine.begin() as connection:
        connection.execute(
            User.__table__.insert().values(
                username="guest_random",
                is_guest=True,
                guest_name="Shared nickname",
                guest_first_joined_at=joined,
                guest_last_requested_at=joined,
            )
        )
        before = list(connection.execute(select(User.__table__)).mappings())
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert list(connection.execute(select(User.__table__)).mappings()) == before
