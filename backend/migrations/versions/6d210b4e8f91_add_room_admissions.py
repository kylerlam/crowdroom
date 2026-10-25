"""Persist room entry approvals and preserve existing members.

Revision ID: 6d210b4e8f91
Revises: 9c304a2d11f0
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision = "6d210b4e8f91"
down_revision = "9c304a2d11f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Approve legacy room members without changing bans or current membership."""
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("room_admissions"):
        op.create_table(
            "room_admissions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("room_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'approved', 'rejected')",
                name="ck_room_admission_status",
            ),
            sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("room_id", "user_id", name="uq_room_admission"),
        )
        op.create_index("ix_room_admissions_room_id", "room_admissions", ["room_id"])
        op.create_index("ix_room_admissions_user_id", "room_admissions", ["user_id"])

    rooms = sa.table(
        "rooms",
        sa.column("id", sa.Uuid()),
        sa.column("host_user_id", sa.Uuid()),
        sa.column("settings", sa.JSON()),
    )
    users = sa.table(
        "users", sa.column("id", sa.Uuid()), sa.column("room_id", sa.Uuid())
    )
    admissions = sa.table(
        "room_admissions",
        sa.column("id", sa.Uuid()),
        sa.column("room_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("status", sa.String(16)),
        sa.column("created_at", sa.DateTime()),
    )
    members = list(bind.execute(sa.select(users)).mappings())
    existing_users = {member["id"] for member in members}
    existing = set(bind.execute(sa.select(admissions.c.room_id, admissions.c.user_id)))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for room in bind.execute(sa.select(rooms)).mappings():
        approved = {room["host_user_id"]} | {
            member["id"] for member in members if member["room_id"] == room["id"]
        }
        approved |= {
            UUID(user_id)
            for user_id in (room["settings"] or {}).get("admin_user_ids", [])
        }
        for user_id in approved & existing_users:
            if (room["id"], user_id) not in existing:
                bind.execute(
                    admissions.insert().values(
                        id=uuid4(),
                        room_id=room["id"],
                        user_id=user_id,
                        status="approved",
                        created_at=now,
                    )
                )


def downgrade() -> None:
    """Remove saved approvals without changing users, rooms, or bans."""
    op.drop_table("room_admissions")
