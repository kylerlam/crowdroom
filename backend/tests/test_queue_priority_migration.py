"""Verify priority migration preserves existing queue entries."""

from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_priority_upgrade_and_downgrade_preserve_existing_entries():
    migration = import_module(
        "backend.migrations.versions.9c304a2d11f0_add_queue_priorities"
    )
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE queue_items (id INTEGER PRIMARY KEY, position INTEGER)")
        )
        connection.execute(text("INSERT INTO queue_items VALUES (1, 3)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            row = connection.execute(text("SELECT * FROM queue_items")).one()
            assert tuple(row) == (1, 3, 0, None)
            migration.downgrade()
        assert [
            column["name"] for column in inspect(connection).get_columns("queue_items")
        ] == ["id", "position"]
        assert tuple(connection.execute(text("SELECT * FROM queue_items")).one()) == (
            1,
            3,
        )
