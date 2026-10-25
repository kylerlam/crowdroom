"""Exercise room approval and invite policy through the active HTTP routes."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine, select

from backend.api.auth.dependencies import get_current_user
from backend.api.rooms.dependencies import get_room_membership_service
from backend.core.config import settings
from backend.core.rate_limit import RequestRateLimiter
from backend.core.security import SecurityService
from backend.db.database import get_session
from backend.db.models import Room, RoomAdmission, RoomInvite, User
from backend.main import app
from backend.repositories.room_admission_repo import RoomAdmissionRepository
from backend.repositories.room_ban_repo import RoomBanRepository
from backend.repositories.room_repo import RoomRepository
from backend.repositories.user_repo import UserRepository
from backend.services.room_membership_service import _pin_attempt_cooldowns
from backend.services.room_service import RoomService


@pytest.fixture
def admission_room(tmp_path):
    """Use independent DB sessions, real PIN checks and real role enforcement."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admissions.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with DBSession(engine) as db:
        users = {
            role: User(username=role)
            for role in ("host", "admin", "member", "applicant", "new_account")
        }
        db.add_all(users.values())
        db.commit()
        room = Room(
            host_user_id=users["host"].id,
            room_name="Approval room",
            room_code="ABCDEF",
            is_private=True,
            pin_hash=SecurityService(settings).generate_password_hash("1234"),
            settings={
                "max_members": 10,
                "admin_user_ids": [str(users["admin"].id)],
            },
        )
        db.add(room)
        db.commit()
        for role in ("host", "admin", "member"):
            users[role].room_id = room.id
            db.add(users[role])
            db.add(
                RoomAdmission(
                    room_id=room.id, user_id=users[role].id, status="approved"
                )
            )
        invite = RoomInvite(room_id=room.id, token="valid-invite", max_uses=1)
        db.add(invite)
        db.commit()
        state = SimpleNamespace(
            engine=engine,
            room_id=room.id,
            ids={role: user.id for role, user in users.items()},
            invite_id=invite.id,
            actor="applicant",
        )

    def session():
        with DBSession(engine) as db:
            yield db

    def current_user():
        with DBSession(engine) as db:
            return db.get(User, state.ids[state.actor])

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = current_user
    _pin_attempt_cooldowns.clear()
    with (
        patch("backend.main.request_limiter", RequestRateLimiter()),
        patch("backend.api.websocket.manager.broadcast", new=AsyncMock()),
        patch(
            "backend.api.websocket.manager.broadcast_to_users", new=AsyncMock()
        ) as notify,
        patch("backend.api.websocket.manager.disconnect_user", new=AsyncMock()),
    ):
        state.client = TestClient(app)
        state.notify = notify
        yield state
        state.client.close()
    app.dependency_overrides.clear()
    app.dependency_overrides.update(overrides)
    _pin_attempt_cooldowns.clear()
    engine.dispose()


def join_path(state, entrance):
    """Build each real public entry route, including a QR-generated invitation."""
    if entrance == "pin":
        return f"/api/v1/rooms/{state.room_id}/join", {"pin": "1234"}
    if entrance == "qr":
        state.actor = "host"
        qr = state.client.post(f"/api/v1/rooms/{state.room_id}/invite-qr")
        assert qr.status_code == 201
        token = parse_qs(urlparse(qr.json()["url"]).query)["token"][0]
        state.actor = "applicant"
    else:
        token = "valid-invite"
    return f"/api/v1/rooms/invite/{token}/join", None


def review(state, *, approved=True, actor="admin", applicant="applicant"):
    """Ask the actual manager endpoint to save the admission decision."""
    state.actor = actor
    return state.client.patch(
        f"/api/v1/rooms/{state.room_id}/admissions/{state.ids[applicant]}",
        json={"approved": approved},
    )


@pytest.mark.parametrize("entrance", ["pin", "invite", "qr"])
def test_first_entry_waits_for_approval_and_only_actual_join_spends_invite(
    admission_room, entrance
):
    state = admission_room
    path, body = join_path(state, entrance)
    result = state.client.post(path, json=body)
    assert result.status_code == 200
    assert result.json()["status"] == "pending"
    assert "Please contact the room host" in result.json()["message"]
    with DBSession(state.engine) as db:
        assert db.get(User, state.ids["applicant"]).room_id is None
        assert all(
            invite.use_count == 0 for invite in db.exec(select(RoomInvite)).all()
        )
    assert state.notify.call_args.args[0]["type"] == "admission_requested"
    assert state.notify.call_args.args[2] == {state.ids["host"], state.ids["admin"]}

    # Polls and duplicate join attempts do not consume a single-use invitation.
    status_path = f"/api/v1/rooms/{state.room_id}/admission/status"
    assert state.client.get(status_path).json()["status"] == "pending"
    assert state.client.post(path, json=body).json()["status"] == "pending"
    assert review(state).json()["status"] == "approved"
    with DBSession(state.engine) as db:
        assert db.get(User, state.ids["applicant"]).room_id is None

    state.actor = "applicant"
    assert state.client.get(status_path).json()["status"] == "approved"
    assert state.client.post(path, json=body).json()["status"] == "joined"
    assert state.client.post(path, json=body).json()["status"] == "joined"
    with DBSession(state.engine) as db:
        assert db.get(User, state.ids["applicant"]).room_id == state.room_id
        uses = sum(invite.use_count for invite in db.exec(select(RoomInvite)).all())
        assert uses == (0 if entrance == "pin" else 1)


@pytest.mark.parametrize("entrance", ["pin", "invite", "qr"])
def test_ban_and_capacity_checks_cannot_be_bypassed_by_an_invite(
    admission_room, entrance
):
    state = admission_room
    path, body = join_path(state, entrance)
    with DBSession(state.engine) as db:
        RoomBanRepository(db).add(state.room_id, state.ids["applicant"])
        RoomAdmissionRepository(db).set_status(
            state.room_id, state.ids["applicant"], "approved"
        )
    assert state.client.post(path, json=body).status_code == 403
    with DBSession(state.engine) as db:
        RoomBanRepository(db).remove(state.room_id, state.ids["applicant"])
        room = db.get(Room, state.room_id)
        room.settings = {**room.settings, "max_members": 3}
        db.add(room)
        db.commit()
    result = state.client.post(path, json=body)
    assert result.status_code == 403
    assert result.json()["detail"] == "Room is full"
    with DBSession(state.engine) as db:
        assert db.get(User, state.ids["applicant"]).room_id is None
        assert all(
            invite.use_count == 0 for invite in db.exec(select(RoomInvite)).all()
        )


@pytest.mark.parametrize(
    "credential", ["wrong-pin", "missing-pin", "invalid-token", "expired-token"]
)
def test_bad_credentials_never_create_an_admission_request(admission_room, credential):
    state = admission_room
    body = {}
    path = f"/api/v1/rooms/{state.room_id}/join"
    if credential == "wrong-pin":
        body = {"pin": "0000"}
    elif credential == "invalid-token":
        path = "/api/v1/rooms/invite/missing/join"
    elif credential == "expired-token":
        with DBSession(state.engine) as db:
            invite = db.get(RoomInvite, state.invite_id)
            invite.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.add(invite)
            db.commit()
        path = "/api/v1/rooms/invite/valid-invite/join"
    assert state.client.post(path, json=body).status_code in {403, 404, 410}
    assert (
        state.client.get(f"/api/v1/rooms/{state.room_id}/admission/status").json()[
            "status"
        ]
        == "none"
    )
    with DBSession(state.engine) as db:
        assert (
            RoomAdmissionRepository(db).get(state.room_id, state.ids["applicant"])
            is None
        )
    state.notify.assert_not_awaited()


def test_only_room_managers_can_list_and_review_requests(admission_room):
    state = admission_room
    path = f"/api/v1/rooms/{state.room_id}/join"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "pending"
    list_path = f"/api/v1/rooms/{state.room_id}/admissions"
    for actor in ("applicant", "member", "new_account"):
        state.actor = actor
        assert state.client.get(list_path).status_code == 403
        assert review(state, actor=actor).status_code == 403
    for actor in ("host", "admin"):
        state.actor = actor
        assert state.client.get(list_path).json() == [
            {
                "user_id": str(state.ids["applicant"]),
                "username": "applicant",
                "status": "pending",
            }
        ]
    assert review(state, approved=False).json()["status"] == "rejected"
    state.actor = "applicant"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "rejected"
    state.actor = "admin"
    assert state.client.get(list_path).json() == [
        {
            "user_id": str(state.ids["applicant"]),
            "username": "applicant",
            "status": "rejected",
        }
    ]
    assert review(state).json()["status"] == "approved"
    assert state.client.get(list_path).json() == []
    state.actor = "applicant"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "joined"


def test_approval_survives_kick_but_never_overrides_a_ban_or_approves_new_accounts(
    admission_room,
):
    state = admission_room
    path = f"/api/v1/rooms/{state.room_id}/join"
    state.client.post(path, json={"pin": "1234"})
    assert review(state).status_code == 200
    state.actor = "applicant"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "joined"
    state.actor = "admin"
    assert (
        state.client.post(
            f"/api/v1/rooms/{state.room_id}/kick/{state.ids['applicant']}"
        ).status_code
        == 200
    )
    state.actor = "applicant"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "joined"
    state.actor = "admin"
    ban_path = f"/api/v1/rooms/{state.room_id}/ban/{state.ids['applicant']}"
    assert state.client.post(ban_path).status_code == 200
    state.actor = "applicant"
    assert (
        state.client.post("/api/v1/rooms/invite/valid-invite/join").status_code == 403
    )
    state.actor = "new_account"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "pending"
    state.actor = "admin"
    assert state.client.delete(ban_path).status_code == 204
    state.actor = "applicant"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "joined"


def test_approval_does_not_reserve_an_invitation_that_later_expires(admission_room):
    state = admission_room
    path = "/api/v1/rooms/invite/valid-invite/join"
    assert state.client.post(path).json()["status"] == "pending"
    assert review(state).status_code == 200
    with DBSession(state.engine) as db:
        invite = db.get(RoomInvite, state.invite_id)
        invite.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.add(invite)
        db.commit()
    state.actor = "applicant"
    assert state.client.post(path).status_code == 410
    with DBSession(state.engine) as db:
        assert db.get(User, state.ids["applicant"]).room_id is None
        assert db.get(RoomInvite, state.invite_id).use_count == 0


def test_public_room_still_requires_first_approval(admission_room):
    state = admission_room
    with DBSession(state.engine) as db:
        room = db.get(Room, state.room_id)
        room.is_private = False
        db.add(room)
        db.commit()
    path = f"/api/v1/rooms/{state.room_id}/join"
    assert state.client.post(path, json={}).json()["status"] == "pending"
    assert review(state).status_code == 200
    state.actor = "applicant"
    assert state.client.post(path, json={}).json()["status"] == "joined"


def test_concurrent_approved_invite_retries_spend_one_use(admission_room):
    """A request queued behind a completed join reloads the user's membership."""
    state = admission_room
    with DBSession(state.engine) as db:
        RoomAdmissionRepository(db).set_status(
            state.room_id, state.ids["applicant"], "approved"
        )
    with DBSession(state.engine) as first_db, DBSession(state.engine) as second_db:
        requests = []
        for db in (first_db, second_db):
            rooms = RoomRepository(db)
            user_repo = UserRepository(db)
            membership = get_room_membership_service(
                RoomService(rooms, SecurityService(settings)),
                user_repo,
                rooms,
                RoomBanRepository(db),
                db,
            )
            # Both identity maps see the account outside the room before joining.
            user = user_repo.get_by_id(state.ids["applicant"])
            assert user.room_id is None
            requests.append((membership, user))

        async def run():
            joined = anyio.Event()
            release = anyio.Event()

            async def slow_broadcast(*_):
                joined.set()
                await release.wait()

            async def join(request):
                membership, user = request
                return await membership._room_invite_service.join_via_invite(
                    "valid-invite", user, membership
                )

            with patch(
                "backend.api.websocket.manager.broadcast",
                new=AsyncMock(side_effect=slow_broadcast),
            ):
                first = asyncio.create_task(join(requests[0]))
                await joined.wait()
                second = asyncio.create_task(join(requests[1]))
                await anyio.sleep(0)
                release.set()
                return await asyncio.gather(first, second)

        assert [result.status for result in anyio.run(run)] == ["joined", "joined"]
    with DBSession(state.engine) as db:
        assert db.get(RoomInvite, state.invite_id).use_count == 1


def test_stale_deny_cannot_revoke_an_account_already_approved(admission_room):
    """A second manager's old pending view cannot undo permanent first approval."""
    state = admission_room
    path = f"/api/v1/rooms/{state.room_id}/join"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "pending"
    assert review(state, actor="host").json()["status"] == "approved"
    result = review(state, approved=False, actor="admin")
    assert result.status_code == 403
    assert "Use Ban" in result.json()["detail"]
    state.actor = "applicant"
    assert state.client.post(path, json={"pin": "1234"}).json()["status"] == "joined"
