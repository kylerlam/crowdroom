"""Regression tests for owner-only delegation and room administrator boundaries."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import anyio
import pytest

from backend.core.exceptions import EntityNotFoundException, ForbiddenException
from backend.core.security import SecurityService
from backend.db.models.room import Room
from backend.db.models.user import User
from backend.repositories.room_ban_repo import RoomBanRepository
from backend.repositories.room_repo import RoomRepository
from backend.repositories.user_repo import UserRepository
from backend.schemas.room import ReadRoom, RoomSettings, UpdateRoom
from backend.services.room_membership_service import RoomMembershipService
from backend.services.room_moderation_service import RoomModerationService
from backend.services.room_service import RoomService


@pytest.fixture
def room_roles():
    room_id = uuid4()
    host = User(username="host", room_id=room_id)
    admin = User(username="admin", room_id=room_id)
    other_admin = User(username="other-admin", room_id=room_id)
    member = User(username="member", room_id=room_id)
    room = Room(
        id=room_id,
        host_user_id=host.id,
        room_name="Room",
        room_code="ABC123",
        settings={
            "skip_threshold": 2,
            "admin_user_ids": [str(admin.id), str(other_admin.id)],
        },
    )
    room.users = [host, admin, other_admin, member]
    repo = MagicMock(spec=RoomRepository)
    repo.get_by_id.return_value = room

    def update(_room_id, data):
        for key, value in data.items():
            setattr(room, key, value)
        return room

    repo.update.side_effect = update
    service = RoomService(repo, MagicMock(spec=SecurityService))
    with patch("backend.services.room_service.manager") as manager:
        manager.broadcast = AsyncMock()
        yield room, host, admin, other_admin, member, service, manager


def test_owner_can_grant_and_revoke_administration(room_roles):
    room, host, _admin, _other_admin, member, service, manager = room_roles

    anyio.run(service.set_admin, room.id, host.id, member.id, True)

    assert member.id in ReadRoom.model_validate(room).admin_user_ids
    assert room.settings["skip_threshold"] == 2
    manager.broadcast.assert_awaited_with(
        {
            "type": "admins_updated",
            "payload": {"admin_user_ids": room.settings["admin_user_ids"]},
        },
        str(room.id),
    )
    assert service.assert_manager(room.id, member.id) is room

    anyio.run(service.set_admin, room.id, host.id, member.id, False)

    assert member.id not in room.admin_user_ids
    with pytest.raises(ForbiddenException):
        service.assert_manager(room.id, member.id)


def test_administrator_cannot_promote_or_demote_members(room_roles):
    room, _host, admin, other_admin, member, service, _manager = room_roles

    for target, is_admin in [(member, True), (other_admin, False), (admin, False)]:
        with pytest.raises(ForbiddenException):
            anyio.run(service.set_admin, room.id, admin.id, target.id, is_admin)


def test_owner_cannot_change_own_role_or_promote_nonmember(room_roles):
    room, host, _admin, _other_admin, _member, service, _manager = room_roles

    with pytest.raises(ForbiddenException):
        anyio.run(service.set_admin, room.id, host.id, host.id, False)
    with pytest.raises(EntityNotFoundException):
        anyio.run(service.set_admin, room.id, host.id, uuid4(), True)


def test_guest_cannot_receive_administrator_role(room_roles):
    room, host, _admin, _other_admin, member, service, _manager = room_roles
    member.is_guest = True
    with pytest.raises(ForbiddenException, match="Guests cannot"):
        anyio.run(service.set_admin, room.id, host.id, member.id, True)
    assert member.id not in room.admin_user_ids


def test_manager_must_be_owner_or_current_administrator(room_roles):
    room, host, admin, _other_admin, member, service, _manager = room_roles

    assert service.assert_manager(room.id, host.id) is room
    assert service.assert_manager(room.id, admin.id) is room
    with pytest.raises(ForbiddenException):
        service.assert_manager(room.id, member.id)
    admin.room_id = None
    with pytest.raises(ForbiddenException):
        service.assert_manager(room.id, admin.id)


def test_updating_room_settings_preserves_administrators(room_roles):
    room, _host, admin, other_admin, _member, service, _manager = room_roles
    data = UpdateRoom(settings=RoomSettings(skip_threshold=7))

    anyio.run(service.update_room, room.id, data)

    assert room.settings["skip_threshold"] == 7
    assert room.admin_user_ids == [admin.id, other_admin.id]


@pytest.mark.parametrize("action", ["kick_user", "ban_user"])
def test_administrator_can_moderate_only_ordinary_members(room_roles, action):
    room, host, admin, other_admin, member, service, _manager = room_roles
    user_repo = MagicMock(spec=UserRepository)
    user_repo.get_by_id.return_value = member
    moderation = RoomModerationService(
        service, MagicMock(spec=RoomBanRepository), user_repo
    )
    with patch("backend.services.room_moderation_service.manager") as manager:
        manager.broadcast = AsyncMock()
        manager.disconnect_user = AsyncMock()

        for protected in [host, admin, other_admin]:
            with pytest.raises(ForbiddenException):
                anyio.run(getattr(moderation, action), room.id, admin.id, protected.id)
        anyio.run(getattr(moderation, action), room.id, admin.id, member.id)

    assert member.room_id is None
    user_repo.save.assert_called_once_with(member)


def test_owner_kicking_administrator_also_revokes_role(room_roles):
    room, host, admin, _other_admin, _member, service, _manager = room_roles
    user_repo = MagicMock(spec=UserRepository)
    user_repo.get_by_id.return_value = admin
    moderation = RoomModerationService(
        service, MagicMock(spec=RoomBanRepository), user_repo
    )
    with patch("backend.services.room_moderation_service.manager") as manager:
        manager.broadcast = AsyncMock()
        manager.disconnect_user = AsyncMock()
        anyio.run(moderation.kick_user, room.id, host.id, admin.id)

    assert admin.id not in room.admin_user_ids
    assert admin.room_id is None


def test_leaving_room_revokes_administration(room_roles):
    room, _host, admin, _other_admin, _member, service, _manager = room_roles
    membership = RoomMembershipService(
        service,
        MagicMock(),
        MagicMock(spec=UserRepository),
        service._room_repo,
        MagicMock(spec=RoomBanRepository),
        MagicMock(),
    )
    with patch("backend.services.room_membership_service.manager") as manager:
        manager.broadcast = AsyncMock()
        anyio.run(membership.leave_room, admin)

    assert admin.id not in room.admin_user_ids
    assert admin.room_id is None


def test_administrator_cannot_unban_protected_roles(room_roles):
    room, host, admin, other_admin, _member, service, _manager = room_roles
    moderation = RoomModerationService(
        service, MagicMock(spec=RoomBanRepository), MagicMock(spec=UserRepository)
    )

    for protected in [host, admin, other_admin]:
        with pytest.raises(ForbiddenException):
            moderation.unban_user(room.id, admin.id, protected.id)
