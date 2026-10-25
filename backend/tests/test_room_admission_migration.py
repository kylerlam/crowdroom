"""Backfill legacy room access without changing membership or existing decisions."""

from importlib import import_module
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select
from sqlmodel import SQLModel, create_engine

from backend.db.models import Room, RoomAdmission, RoomBan, User


def test_admission_migration_preserves_existing_access_and_bans():
    migration = import_module(
        "backend.migrations.versions.6d210b4e8f91_add_room_admissions"
    )
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    RoomAdmission.__table__.drop(engine)
    room_id, host_id, member_id, admin_id, outsider_id = [uuid4() for _ in range(5)]
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            [
                {"id": user_id, "username": name, "room_id": membership}
                for user_id, name, membership in (
                    (host_id, "host", None),
                    (member_id, "member", room_id),
                    (admin_id, "admin", None),
                    (outsider_id, "outsider", None),
                )
            ],
        )
        connection.execute(
            Room.__table__.insert().values(
                id=room_id,
                room_name="Legacy room",
                room_code="ABCDEF",
                host_user_id=host_id,
                settings={"admin_user_ids": [str(admin_id)]},
            )
        )
        connection.execute(
            RoomBan.__table__.insert().values(room_id=room_id, user_id=outsider_id)
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            records = connection.execute(
                select(RoomAdmission.user_id, RoomAdmission.status)
            ).all()
            assert set(records) == {
                (host_id, "approved"),
                (member_id, "approved"),
                (admin_id, "approved"),
            }
            # create_all may already have installed the table; reruns remain safe.
            migration.upgrade()
            assert len(connection.execute(select(RoomAdmission.id)).all()) == 3
            connection.execute(
                RoomAdmission.__table__.update()
                .where(RoomAdmission.user_id == member_id)
                .values(status="rejected")
            )
            migration.upgrade()
            assert (
                connection.execute(
                    select(RoomAdmission.status).where(
                        RoomAdmission.user_id == member_id
                    )
                ).scalar_one()
                == "rejected"
            )
            migration.downgrade()
        assert not inspect(connection).has_table("room_admissions")
        assert (
            connection.execute(
                select(User.room_id).where(User.id == member_id)
            ).scalar_one()
            == room_id
        )
        assert connection.execute(select(RoomBan.user_id)).scalar_one() == outsider_id
    engine.dispose()
