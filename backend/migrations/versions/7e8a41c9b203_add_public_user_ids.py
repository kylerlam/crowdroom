"""Backfill permanent public CrowdRoom account IDs.

Revision ID: 7e8a41c9b203
Revises: 6d210b4e8f91
"""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "7e8a41c9b203"
down_revision = "6d210b4e8f91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep existing identities and assign missing IDs without collisions."""
    bind = op.get_bind()
    columns = {
        column["name"]: column for column in sa.inspect(bind).get_columns("users")
    }
    if "public_id" not in columns:
        op.add_column("users", sa.Column("public_id", sa.String(12), nullable=True))

    users = sa.table(
        "users", sa.column("id", sa.Uuid()), sa.column("public_id", sa.String(12))
    )
    rows = list(bind.execute(sa.select(users)).mappings())
    used = {row["public_id"] for row in rows if row["public_id"] is not None}
    for row in rows:
        if row["public_id"] is not None:
            continue
        while True:
            digits = f"{secrets.randbelow(100_000_000):08d}"
            public_id = f"CR-{digits[:4]}-{digits[4:]}"
            if public_id not in used:
                break
        bind.execute(
            users.update().where(users.c.id == row["id"]).values(public_id=public_id)
        )
        used.add(public_id)

    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("users")
    }
    if columns["public_id"]["nullable"] or "uq_users_public_id" not in constraints:
        with op.batch_alter_table("users") as batch:
            if columns["public_id"]["nullable"]:
                batch.alter_column(
                    "public_id", existing_type=sa.String(12), nullable=False
                )
            if "uq_users_public_id" not in constraints:
                batch.create_unique_constraint("uq_users_public_id", ["public_id"])


def downgrade() -> None:
    """Remove public labels without changing UUIDs or account membership."""
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_public_id", type_="unique")
        batch.drop_column("public_id")
