"""Account routes must not bypass room owner and administrator protection."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api.auth.dependencies import get_current_user
from backend.api.users.dependencies import get_user_service
from backend.api.users.router import router
from backend.core.exceptions import ForbiddenException
from backend.db.models.user import User
from backend.schemas.user import UserRead
from backend.services.user_service import UserService


@pytest.fixture
def account_api():
    app = FastAPI()
    app.include_router(router)
    service = MagicMock(spec=UserService)
    app.dependency_overrides[get_user_service] = lambda: service

    @app.exception_handler(ForbiddenException)
    async def forbidden(_request, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    yield app, TestClient(app), service


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_account_mutations_require_authentication(account_api, method):
    _, client, service = account_api
    kwargs = {"json": {"username": "changed"}} if method == "patch" else {}
    response = getattr(client, method)(f"/users/{uuid4()}", **kwargs)
    assert response.status_code == 401
    service.update_user.assert_not_called()
    service.delete_user.assert_not_called()


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_cannot_mutate_another_account_even_as_room_manager(account_api, method):
    app, client, service = account_api
    current_user = User(username="room-admin", room_id=uuid4())
    app.dependency_overrides[get_current_user] = lambda: current_user
    kwargs = {"json": {"username": "changed"}} if method == "patch" else {}
    response = getattr(client, method)(f"/users/{uuid4()}", **kwargs)
    assert response.status_code == 403
    service.update_user.assert_not_called()
    service.delete_user.assert_not_called()


def test_account_owner_can_update_self(account_api):
    app, client, service = account_api
    user = User(username="owner")
    app.dependency_overrides[get_current_user] = lambda: user
    service.update_user.return_value = UserRead(id=user.id, username="updated")
    response = client.patch(f"/users/{user.id}", json={"username": "updated"})
    assert response.status_code == 200
    assert response.json()["username"] == "updated"
    assert service.update_user.call_args.args[0] == user.id


def test_account_owner_can_delete_self(account_api):
    app, client, service = account_api
    user = User(username="owner")
    app.dependency_overrides[get_current_user] = lambda: user
    response = client.delete(f"/users/{user.id}")
    assert response.status_code == 204
    service.delete_user.assert_called_once_with(user.id)
