"""Service for managing room invite links."""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from backend.core.exceptions import (
    EntityNotFoundException,
    ForbiddenException,
    InviteExpiredException,
)
from backend.core.invite_token import generate_invite_token
from backend.db.models.room_invite import RoomInvite
from backend.db.models.user import User
from backend.repositories.room_invite_repo import RoomInviteRepository
from backend.repositories.room_repo import RoomRepository
from backend.schemas.room_invite import (
    CreateRoomInvite,
    InviteJoinResult,
    InvitePreview,
)
from backend.services.room_service import RoomService

if TYPE_CHECKING:
    from backend.services.room_membership_service import RoomMembershipService

MAX_TOKEN_RETRIES = 5


class RoomInviteService:
    """Service for creating, validating, and redeeming room invites."""

    def __init__(
        self,
        invite_repo: RoomInviteRepository,
        room_repo: RoomRepository,
        room_service: RoomService,
    ) -> None:
        """Initialize the RoomInviteService with its dependencies.

        Args:
            invite_repo: Repository for invite data operations.
            room_repo: Repository for room lookups.
            room_service: Service for room operations including host verification.
        """
        self._invite_repo = invite_repo
        self._room_repo = room_repo
        self._room_service = room_service

    def _validate_invite(self, invite: RoomInvite) -> None:
        """Check that an invite is still valid (not expired or exhausted).

        Args:
            invite: The invite to validate.

        Raises:
            InviteExpiredException: If invite is expired or has reached max uses.
        """
        now = datetime.now(timezone.utc)
        expires_at = invite.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at <= now:
            raise InviteExpiredException()
        if invite.max_uses is not None and invite.use_count >= invite.max_uses:
            raise InviteExpiredException()

    def create_invite(
        self,
        room_id: UUID,
        user_id: UUID,
        data: CreateRoomInvite,
        token_factory: Callable[[], str] = generate_invite_token,
    ) -> RoomInvite:
        """Create a new invite link for a room.

        Args:
            room_id: The room to create an invite for.
            user_id: The requesting user (must be host).
            data: Invite creation parameters.
            token_factory: Callable returning a token string. Defaults to
                the standard 12-char generator. Pass `generate_long_invite_token`
                for harder-to-guess QR invites.

        Returns:
            The created RoomInvite.

        Raises:
            EntityNotFoundException: If room does not exist.
            ForbiddenException: If user is not the room host.
            IntegrityError: If token generation exhausts all retries.
        """
        self._room_service.assert_host(room_id, user_id)

        expires_at = None
        if data.expires_in_hours is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(
                hours=data.expires_in_hours
            )

        for attempt in range(MAX_TOKEN_RETRIES + 1):
            invite = RoomInvite(
                room_id=room_id,
                token=token_factory(),
                max_uses=data.max_uses,
                expires_at=expires_at,
            )
            try:
                return self._invite_repo.create(invite)
            except IntegrityError:
                if attempt == MAX_TOKEN_RETRIES:
                    raise

    def get_invite_preview(self, token: str) -> InvitePreview:
        """Validate an invite token and return room preview info.

        Args:
            token: The invite token to look up.

        Returns:
            Preview information about the room.

        Raises:
            EntityNotFoundException: If token does not exist.
            InviteExpiredException: If invite is expired or exhausted.
        """
        invite = self._invite_repo.get_by_token(token)
        if not invite:
            raise EntityNotFoundException("Invite", token)
        self._validate_invite(invite)

        room = self._room_repo.get_by_id(invite.room_id)
        if not room:
            raise EntityNotFoundException("Room", invite.room_id)

        return InvitePreview(
            room_id=room.id,
            room_name=room.room_name,
            is_private=room.is_private,
            host_user_id=room.host_user_id,
        )

    async def join_via_invite(
        self, token: str, user: User, membership_service: "RoomMembershipService"
    ) -> InviteJoinResult:
        """Apply the same ban, capacity, and approval checks as ordinary joins.

        Args:
            token: The invite token to redeem.
            user: The authenticated user joining the room.
            membership_service: Shared room-entry policy, supplied without a DI cycle.

        Returns:
            Result containing the joined room info.

        Raises:
            EntityNotFoundException: If token or room does not exist.
            InviteExpiredException: If invite is expired or exhausted.
        """
        invite = self._invite_repo.get_by_token(token)
        if not invite:
            raise EntityNotFoundException("Invite", token)
        return await membership_service.join_room(
            invite.room_id, user, invite_token=token
        )

    def list_invites(self, room_id: UUID, user_id: UUID) -> list[RoomInvite]:
        """List all invites for a room (host only).

        Args:
            room_id: The room to list invites for.
            user_id: The requesting user (must be host).

        Returns:
            List of RoomInvite instances.

        Raises:
            EntityNotFoundException: If room does not exist.
            ForbiddenException: If user is not the room host.
        """
        self._room_service.assert_host(room_id, user_id)
        return self._invite_repo.get_by_room(room_id)

    def revoke_invite(self, room_id: UUID, invite_id: UUID, user_id: UUID) -> None:
        """Revoke (delete) a specific invite.

        Args:
            room_id: The room the invite belongs to.
            invite_id: The invite to revoke.
            user_id: The requesting user (must be host).

        Raises:
            EntityNotFoundException: If room or invite does not exist.
            ForbiddenException: If user is not the room host or invite
                doesn't belong to the room.
        """
        self._room_service.assert_host(room_id, user_id)

        invite = self._invite_repo.get_by_id(invite_id)
        if not invite:
            raise EntityNotFoundException("Invite", invite_id)
        if invite.room_id != room_id:
            raise ForbiddenException("Invite does not belong to this room")

        self._invite_repo.delete(invite_id)

    def get_valid_invite(self, token: str, expected_room_id: UUID) -> RoomInvite:
        """Validate an invitation without spending a use on a pending applicant.

        Args:
            token: The invite token to check.
            expected_room_id: The room the invite must belong to.

        Returns:
            The invitation, to consume only after an approved account joins.
        """
        invite = self._invite_repo.get_by_token(token)
        if not invite or invite.room_id != expected_room_id:
            raise EntityNotFoundException("Invite")
        self._validate_invite(invite)
        return invite

    def consume_invite(self, invite: RoomInvite) -> None:
        """Count an actual approved join, serialized by the room-entry lock."""
        self._invite_repo.increment_use_count(invite)
