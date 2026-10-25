"""Add queue pin state and explicit priority order.

Revision ID: 9c304a2d11f0
Revises: 7b1c4d2a9e80
"""

import sqlalchemy as sa
from alembic import op

revision = "9c304a2d11f0"
down_revision = "7b1c4d2a9e80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve existing queue order until a manager prioritizes a song."""
    with op.batch_alter_table("queue_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(sa.Column("priority_position", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove explicit queue priority fields."""
    with op.batch_alter_table("queue_items") as batch_op:
        batch_op.drop_column("priority_position")
        batch_op.drop_column("is_pinned")
