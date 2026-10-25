"""Persistent first-entry approval for an account in a room."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from backend.db.models.room import Room
    from backend.db.models.user import User


class RoomAdmission(SQLModel, table=True):
    """A pending request or a manager's saved admission decision."""

    __tablename__ = "room_admissions"
    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_room_admission"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_room_admission_status",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    room_id: UUID = Field(foreign_key="rooms.id", nullable=False, index=True)
    user_id: UUID = Field(foreign_key="users.id", nullable=False, index=True)
    status: str = Field(default="pending", max_length=16, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )

    room: "Room" = Relationship(back_populates="admissions")
    user: "User" = Relationship(back_populates="room_admissions")
