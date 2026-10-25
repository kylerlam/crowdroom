"""Database model representing a User in the system."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, UniqueConstraint, false
from sqlalchemy.orm import Mapped
from sqlmodel import Field, Relationship, SQLModel

from backend.core.user_identity import generate_public_id

if TYPE_CHECKING:
    from backend.db.models.platform_connection import PlatformConnection
    from backend.db.models.queue_item import QueueItem
    from backend.db.models.queue_vote import QueueVote
    from backend.db.models.room import Room
    from backend.db.models.room_admission import RoomAdmission


class User(SQLModel, table=True):
    """Database model representing a User in the system.

    This model defines the persistent storage for user identities, supporting
    both authenticated users (with email/password) and anonymous/local users
    (without email/password).

    Attributes:
        id (UUID): Primary key, universally unique identifier.
        public_id (str): Permanent public identifier for room admission checks.
        username (str): Unique handle for the user (max 32 chars).
        email (str, optional): Email address for authentication.
        hashed_password (str, optional): Argon2/Bcrypt hashed password.
        room_id (Optional[UUID]): Foreign key to the room the user is currently in.
        room (Optional[Room]): Relationship to the Room the user belongs to.
        queue_items (list[QueueItem]): List of items added to the queue.
        user_votes (list[QueueVote]): List of skip votes cast by this user.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("public_id", name="uq_users_public_id"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    public_id: str = Field(
        default_factory=generate_public_id, min_length=12, max_length=12, nullable=False
    )
    username: str = Field(max_length=32, unique=True, nullable=False)
    email: str | None = Field(default=None, max_length=255, unique=True, nullable=True)
    hashed_password: str | None = Field(default=None, max_length=255, nullable=True)
    avatar_path: str | None = Field(default=None, max_length=255, nullable=True)
    room_id: UUID | None = Field(default=None, foreign_key="rooms.id", nullable=True)
    token_version: int = Field(default=0, nullable=False)
    is_guest: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=false())
    )
    guest_name: str | None = Field(default=None, max_length=32, nullable=True)
    guest_first_joined_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    guest_last_requested_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )

    @property
    def display_name(self) -> str:
        """Show a guest's chosen nickname without exposing its random login handle."""
        return self.guest_name if self.is_guest and self.guest_name else self.username

    # Relations
    room: "Room" = Relationship(
        back_populates="users",
        sa_relationship_kwargs={"foreign_keys": "[User.room_id]"},
    )
    queue_items: Mapped[list["QueueItem"]] = Relationship(back_populates="added_by")
    user_votes: Mapped[list["QueueVote"]] = Relationship(back_populates="user")
    platform_connections: Mapped[list["PlatformConnection"]] = Relationship(
        back_populates="user"
    )
    room_admissions: Mapped[list["RoomAdmission"]] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
