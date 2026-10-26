"""Retain a brief failed-command guard independently of explicit pauses.

Revision ID: 9b06c12e4a31
Revises: 8f21c6d40a72
"""

import sqlalchemy as sa
from alembic import op

revision = "9b06c12e4a31"
down_revision = "8f21c6d40a72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve existing playback state while adding a nullable sync guard."""
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("sessions")
    }
    if "playback_sync_hold_until" not in columns:
        op.add_column(
            "sessions",
            sa.Column(
                "playback_sync_hold_until", sa.DateTime(timezone=True), nullable=True
            ),
        )


def downgrade() -> None:
    """Remove the guard without changing playback or pending songs."""
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("playback_sync_hold_until")
