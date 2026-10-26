"""Persist app-wide Spotify API cooldown deadlines.

Revision ID: a706e9c2b410
Revises: 9b06c12e4a31
"""

import sqlalchemy as sa
from alembic import op

revision = "a706e9c2b410"
down_revision = "9b06c12e4a31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "spotify_api_cooldowns" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "spotify_api_cooldowns",
            sa.Column("scope", sa.String(64), primary_key=True),
            sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("spotify_api_cooldowns")
