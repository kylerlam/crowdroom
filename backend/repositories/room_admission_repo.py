"""Persistence for per-room admission requests and saved approvals."""

from uuid import UUID

from sqlmodel import Session as DBSession
from sqlmodel import select

from backend.db.models.room_admission import RoomAdmission


class RoomAdmissionRepository:
    """Read and store a room manager's decision for a user."""

    def __init__(self, session: DBSession):
        self._session = session

    def get(self, room_id: UUID, user_id: UUID) -> RoomAdmission | None:
        """Reload the latest request, including another request's decision."""
        return self._session.exec(
            select(RoomAdmission)
            .where(
                RoomAdmission.room_id == room_id,
                RoomAdmission.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        ).first()

    def set_status(self, room_id: UUID, user_id: UUID, status: str) -> RoomAdmission:
        """Save a request or decision while the caller holds the room join lock."""
        admission = self.get(room_id, user_id)
        if admission is None:
            admission = RoomAdmission(room_id=room_id, user_id=user_id)
        admission.status = status
        self._session.add(admission)
        self._session.commit()
        self._session.refresh(admission)
        return admission

    def list_for_review(self, room_id: UUID) -> list[RoomAdmission]:
        """Include declined accounts so a manager can correct a past decision."""
        return list(
            self._session.exec(
                select(RoomAdmission)
                .where(
                    RoomAdmission.room_id == room_id,
                    RoomAdmission.status.in_(["pending", "rejected"]),
                )
                .order_by(RoomAdmission.created_at, RoomAdmission.id)
                .execution_options(populate_existing=True)
            ).all()
        )
