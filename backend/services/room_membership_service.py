"""Service for managing room join and leave operations."""

import asyncio
import time
from uuid import UUID
from weakref import WeakValueDictionary

from backend.api.websocket import manager
from backend.core.config import settings
from backend.core.exceptions import (
    EntityNotFoundException,
    ForbiddenException,
    TooManyRequestsException,
    UserAlreadyInRoomException,
    UserBannedException,
)
from backend.db.models.room import Room
from backend.db.models.user import User
from backend.repositories.room_admission_repo import RoomAdmissionRepository
from backend.repositories.room_ban_repo import RoomBanRepository
from backend.repositories.room_repo import RoomRepository
from backend.repositories.user_repo import UserRepository
from backend.schemas.room import (
    RoomAdmissionRequest,
    RoomAdmissionStatus,
    RoomJoinResult,
)
from backend.services.room_invite_service import RoomInviteService
from backend.services.room_service import RoomService

_pin_attempt_cooldowns: dict[UUID, float] = {}
# ponytail: one app worker; use database locks if multiple workers are introduced.
_join_locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()


def _room_join_lock(room_id: UUID) -> asyncio.Lock:
    """Serialize approval decisions, capacity checks, and invite consumption."""
    return _join_locks.setdefault(room_id, asyncio.Lock())


class RoomMembershipService:
    """Handles user join and leave operations for rooms."""

    def __init__(
        self,
        room_service: RoomService,
        room_invite_service: RoomInviteService,
        user_repo: UserRepository,
        room_repo: RoomRepository,
        ban_repo: RoomBanRepository,
        admission_repo: RoomAdmissionRepository,
    ) -> None:
        """Initialize the RoomMembershipService with its dependencies.

        Args:
            room_service: Service for room CRUD and PIN verification.
            room_invite_service: Service for invite token validation.
            user_repo: Repository for persisting user changes.
            room_repo: Repository for room data access.
            ban_repo: Repository for checking room bans.
            admission_repo: Persistent first-entry requests and approvals.
        """
        self._room_service = room_service
        self._room_invite_service = room_invite_service
        self._user_repo = user_repo
        self._room_repo = room_repo
        self._ban_repo = ban_repo
        self._admission_repo = admission_repo

    def _check_pin_cooldown(self, user_id: UUID) -> None:
        """Reject the request if the user is locked out from a recent wrong PIN.

        Args:
            user_id: UUID of the user attempting to join.

        Raises:
            TooManyRequestsException: If the cooldown window has not elapsed
                since the last failed PIN attempt.
        """
        cooldown = settings.PIN_ATTEMPT_COOLDOWN_SECONDS
        last = _pin_attempt_cooldowns.get(user_id)
        if last is None:
            return
        elapsed = time.monotonic() - last
        if elapsed < cooldown:
            raise TooManyRequestsException(retry_after=cooldown - elapsed)
        _pin_attempt_cooldowns.pop(user_id, None)

    def _record_pin_failure(self, user_id: UUID) -> None:
        """Stamp the user's most recent failed PIN attempt.

        Args:
            user_id: UUID of the user whose attempt failed.
        """
        _pin_attempt_cooldowns[user_id] = time.monotonic()

    async def join_room(
        self,
        room_id: UUID,
        user: User,
        pin: str | None = None,
        invite_token: str | None = None,
    ) -> RoomJoinResult:
        """Validate access and join an approved account or request first entry.

        Args:
            room_id: The room to join.
            user: The authenticated user.
            pin: Optional PIN for private rooms.
            invite_token: Optional invite token to bypass PIN.

        Raises:
            UserAlreadyInRoomException: If user is already in another room.
            EntityNotFoundException: If room does not exist.
            UserBannedException: If the user is banned from this room.
            ForbiddenException: If room is private and no valid credentials.
            TooManyRequestsException: If the user recently submitted a wrong
                PIN and the cooldown window has not yet elapsed.
        """
        async with _room_join_lock(room_id):
            user = self._user_repo.get_by_id(user.id, fresh=True)
            if user is None:
                raise EntityNotFoundException("User")
            return await self._join_locked(room_id, user, pin, invite_token)

    async def _join_locked(
        self, room_id: UUID, user: User, pin: str | None, invite_token: str | None
    ) -> RoomJoinResult:
        """Validate credentials and approval before changing room membership."""
        if user.room_id is not None and user.room_id != room_id:
            raise UserAlreadyInRoomException()
        room = self._room_service.get_room(room_id)

        if self._ban_repo.exists(room_id, user.id):
            raise UserBannedException()

        if user.room_id == room_id:
            if self._admission_repo.get(room_id, user.id) is None:
                self._admission_repo.set_status(room_id, user.id, "approved")
            return self._join_result(room, "joined")

        max_members = room.settings.get("max_members", 50)
        current_count = self._user_repo.count_by_room(room_id)
        if current_count >= max_members:
            raise ForbiddenException("Room is full")

        invite = (
            self._room_invite_service.get_valid_invite(invite_token, room_id)
            if invite_token
            else None
        )
        if room.is_private:
            # The host always has access to their own room and never needs a
            # PIN or invite to enter it.
            if room.host_user_id != user.id:
                self._check_pin_cooldown(user.id)
                if invite is not None:
                    _pin_attempt_cooldowns.pop(user.id, None)
                else:
                    if pin and self._room_service.verify_pin(room_id, pin):
                        _pin_attempt_cooldowns.pop(user.id, None)
                    else:
                        self._record_pin_failure(user.id)
                        raise ForbiddenException("The PIN you entered is not valid")

        admission = self._admission_repo.get(room_id, user.id)
        if user.id == room.host_user_id:
            if admission is None or admission.status != "approved":
                self._admission_repo.set_status(room_id, user.id, "approved")
        elif admission is None:
            self._admission_repo.set_status(room_id, user.id, "pending")
            await self._notify_admission(room, user.id, "pending")
            return self._join_result(room, "pending")
        elif admission.status != "approved":
            return self._join_result(room, admission.status)

        user.room_id = room_id
        self._user_repo.save(user)
        if invite is not None:
            self._room_invite_service.consume_invite(invite)

        await manager.broadcast(
            {
                "type": "member_joined",
                "payload": {
                    "user_id": str(user.id),
                    "username": user.username,
                },
            },
            str(room_id),
        )
        return self._join_result(room, "joined")

    @staticmethod
    def _join_result(room: Room, status: str) -> RoomJoinResult:
        """Describe whether the account joined or needs a manager's decision."""
        messages = {
            "joined": "Joined room successfully",
            "pending": (
                "Your request to join has been sent. Please contact the room host "
                "or an administrator for approval."
            ),
            "rejected": (
                "Your request was not approved. Please contact the room host "
                "or an administrator."
            ),
        }
        return RoomJoinResult(
            room_id=room.id,
            room_name=room.room_name,
            status=status,
            message=messages[status],
        )

    def get_admission_status(self, room_id: UUID, user: User) -> RoomAdmissionStatus:
        """Read only the caller's approval; polling never creates a request."""
        room = self._room_service.get_room(room_id)
        if self._ban_repo.exists(room_id, user.id):
            raise UserBannedException()
        admission = self._admission_repo.get(room_id, user.id)
        status = admission.status if admission is not None else "none"
        if user.id == room.host_user_id or user.room_id == room_id:
            status = "approved"
        return RoomAdmissionStatus(room_id=room_id, user_id=user.id, status=status)

    def list_admissions(
        self, room_id: UUID, actor_id: UUID
    ) -> list[RoomAdmissionRequest]:
        """Show waiting and declined accounts only to the room's managers."""
        self._room_service.assert_manager(room_id, actor_id)
        return [
            RoomAdmissionRequest(
                user_id=entry.user_id, username=entry.user.username, status=entry.status
            )
            for entry in self._admission_repo.list_for_review(room_id)
        ]

    async def review_admission(
        self, room_id: UUID, actor_id: UUID, user_id: UUID, approved: bool
    ) -> RoomAdmissionStatus:
        """Save a manager's decision without joining the applicant for them."""
        async with _room_join_lock(room_id):
            room = self._room_service.assert_manager(room_id, actor_id)
            admission = self._admission_repo.get(room_id, user_id)
            if admission is None:
                raise EntityNotFoundException("Admission request", user_id)
            if approved and self._ban_repo.exists(room_id, user_id):
                raise UserBannedException()
            if admission.status == "approved" and not approved:
                raise ForbiddenException(
                    "This account is already approved. Use Ban to block room access."
                )
            status = "approved" if approved else "rejected"
            self._admission_repo.set_status(room_id, user_id, status)
            await self._notify_admission(room, user_id, status)
            return RoomAdmissionStatus(room_id=room_id, user_id=user_id, status=status)

    async def _notify_admission(self, room: Room, user_id: UUID, status: str) -> None:
        """Notify actual managers without exposing requests to ordinary members."""
        recipients = {room.host_user_id} | {
            user.id
            for user in self._user_repo.get_by_room(room.id)
            if user.id in room.admin_user_ids
        }
        await manager.broadcast_to_users(
            {
                "type": "admission_requested"
                if status == "pending"
                else "admission_updated",
                "room_id": str(room.id),
                "payload": {"user_id": str(user_id), "status": status},
            },
            str(room.id),
            recipients,
        )

    async def leave_room(self, user: User) -> None:
        """Remove a user from their current room.

        Args:
            user: The authenticated user.

        Raises:
            EntityNotFoundException: If user is not in any room.
        """
        if user.room_id is None:
            raise EntityNotFoundException("Room membership")
        room_id = user.room_id
        room = self._room_service.get_room(room_id)
        if user.id in room.admin_user_ids:
            await self._room_service.set_admin(
                room_id, room.host_user_id, user.id, False
            )
        user.room_id = None
        self._user_repo.save(user)

        await manager.broadcast(
            {
                "type": "member_left",
                "payload": {
                    "user_id": str(user.id),
                    "username": user.username,
                },
            },
            str(room_id),
        )

        if room.host_user_id == user.id:
            await self._handle_host_departure(room)

    async def _handle_host_departure(self, room: Room) -> None:
        """Close room and evict remaining members when host leaves.

        Args:
            room: The room whose host departed.
        """
        await manager.broadcast(
            {
                "type": "room_closed",
                "payload": {"reason": "host_left"},
            },
            str(room.id),
        )

        for member in room.users:
            if member.room_id == room.id:
                member.room_id = None
                self._user_repo.save(member)

        self._room_repo.delete(room.id)
