"""Persist explicit guest identities and their song-request deadlines.

Revision ID: 8f21c6d40a72
Revises: 7e8a41c9b203
"""

import sqlalchemy as sa
from alembic import op

revision = "8f21c6d40a72"
down_revision = "7e8a41c9b203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve existing accounts and add fields used by nickname-only guests."""
    existing = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")
    }
    columns = (
        sa.Column("is_guest", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("guest_name", sa.String(32), nullable=True),
        sa.Column("guest_first_joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("guest_last_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("users", column)


def downgrade() -> None:
    """Remove guest-specific fields without modifying pre-existing account data."""
    with op.batch_alter_table("users") as batch:
        batch.drop_column("guest_last_requested_at")
        batch.drop_column("guest_first_joined_at")
        batch.drop_column("guest_name")
        batch.drop_column("is_guest")
