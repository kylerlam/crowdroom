"""Exercise permanent account labels and their database uniqueness contract."""

import io
import os
import re
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Column, MetaData, Table, Uuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.api.auth.router as auth_routes
import backend.repositories.user_repo as user_repo_module
from backend.api.users.router import router as users_router
from backend.core.user_identity import generate_public_id
from backend.db.database import get_session
from backend.db.models import Room, User
from backend.repositories.user_repo import UserRepository
from backend.schemas.user import UserCreate, UserRead, UserUpdate


@pytest.fixture(params=["sqlite", "postgresql"])
def identity_engine(request):
    """Give each check an independent SQLite DB or PostgreSQL schema."""
    base = None
    schema = None
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        url = os.environ.get("CROWDROOM_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("CROWDROOM_TEST_POSTGRES_URL not set")
        schema = "identity_" + uuid4().hex
        base = create_engine(url)
        with base.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()
    if base is not None:
        with base.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.dispose()


def test_generated_id_keeps_all_eight_digits_including_leading_zero(monkeypatch):
    monkeypatch.setattr("backend.core.user_identity.secrets.randbelow", lambda _: 1234)
    assert generate_public_id() == "CR-0000-1234"
    assert User(username="zero").public_id == "CR-0000-1234"


def test_database_requires_unique_nonnull_public_id_even_for_core_inserts(
    identity_engine,
):
    with identity_engine.begin() as connection:
        connection.execute(User.__table__.insert().values(username="core-user"))
        users = Table(
            "users",
            MetaData(),
            Column("id", Uuid(), primary_key=True),
            autoload_with=connection,
        )
        original = connection.execute(select(users)).mappings().one()
        assert re.fullmatch(r"CR-[0-9]{4}-[0-9]{4}", original["public_id"])
        for value in (None, original["public_id"]):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    users.insert().values(
                        id=uuid4(),
                        username="duplicate",
                        public_id=value,
                        token_version=0,
                    )
                )


def test_new_account_collision_retries_without_changing_its_uuid(
    identity_engine, monkeypatch
):
    with Session(identity_engine) as db:
        repo = UserRepository(db)
        existing = repo.save(User(username="first", public_id="CR-0000-0001"))
        incoming = User(username="second", public_id=existing.public_id)
        incoming_uuid = incoming.id
        replacements = iter(["CR-0000-0001", "CR-0000-0002"])
        monkeypatch.setattr(
            user_repo_module, "generate_public_id", lambda: next(replacements)
        )
        saved = repo.save(incoming)
        assert saved.id == incoming_uuid
        assert saved.public_id == "CR-0000-0002"
        assert db.get(User, existing.id).public_id == "CR-0000-0001"


@pytest.mark.parametrize("duplicate", ["username", "email"])
def test_other_unique_conflicts_are_not_retried(
    identity_engine, monkeypatch, duplicate
):
    with Session(identity_engine) as db:
        repo = UserRepository(db)
        repo.save(User(username="first", email="first@example.com"))
        new_user = User(
            username="first" if duplicate == "username" else "second",
            email="first@example.com" if duplicate == "email" else "second@example.com",
        )

        def unexpected_regeneration():
            raise AssertionError(
                "Username/email errors must not regenerate the account ID"
            )

        monkeypatch.setattr(
            user_repo_module, "generate_public_id", unexpected_regeneration
        )
        with pytest.raises(IntegrityError):
            repo.save(new_user)


def test_profile_and_membership_changes_keep_public_id(identity_engine):
    with Session(identity_engine) as db:
        repo = UserRepository(db)
        user = repo.save(User(username="before"))
        original_id = user.public_id
        room = Room(host_user_id=user.id, room_name="Home", room_code="ABCDEF")
        db.add(room)
        db.commit()
        user.username = "after"
        user.room_id = room.id
        repo.save(user)
        assert UserRead.model_validate(user).public_id == original_id
        user.room_id = None
        repo.save(user)
        assert db.get(User, user.id).public_id == original_id
        assert (
            "public_id"
            not in UserCreate(username="x", public_id="CR-9999-9999").model_dump()
        )
        assert "public_id" not in UserUpdate(public_id="CR-9999-9999").model_dump(
            exclude_unset=True
        )


@pytest.fixture
def identity_api(identity_engine, monkeypatch, tmp_path):
    """Use actual user/auth services and signed cookies without the runtime DB."""
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")

    def session():
        with Session(identity_engine) as db:
            yield db

    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(auth_routes.settings, "AUTH_MODE", "ONLINE")
    monkeypatch.setattr(auth_routes, "AVATARS_DIR", tmp_path)
    with TestClient(app) as client:
        yield client


def test_register_login_profile_avatar_and_rename_return_same_id(identity_api):
    client = identity_api
    registration = {
        "username": "identity-user",
        "email": "identity@example.com",
        "password": "identity-password-123",
    }
    assert client.post("/api/v1/auth/register", json=registration).status_code == 201
    before = client.get("/api/v1/auth/me").json()
    assert re.fullmatch(r"CR-[0-9]{4}-[0-9]{4}", before["public_id"])
    patch = client.patch(
        f"/api/v1/users/{before['id']}",
        json={"username": "renamed-user", "public_id": "CR-9999-9999"},
    )
    assert patch.status_code == 200
    assert patch.json()["public_id"] == before["public_id"]
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    avatar = client.post(
        "/api/v1/auth/avatar",
        files={"file": ("avatar.png", image.getvalue(), "image/png")},
    )
    assert avatar.status_code == 200
    assert avatar.json()["public_id"] == before["public_id"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"identifier": "renamed-user", "password": registration["password"]},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me").json()["public_id"] == before["public_id"]


def test_every_account_creation_endpoint_assigns_a_new_id(identity_api, monkeypatch):
    client = identity_api
    direct = client.post(
        "/api/v1/users/", json={"username": "direct-user", "public_id": "CR-9999-9999"}
    )
    assert direct.status_code == 201
    assert re.fullmatch(r"CR-[0-9]{4}-[0-9]{4}", direct.json()["public_id"])
    monkeypatch.setattr(auth_routes.settings, "AUTH_MODE", "LOCAL")
    assert (
        client.post(
            "/api/v1/auth/local-login", json={"username": "local-user"}
        ).status_code
        == 200
    )
    local = client.get("/api/v1/auth/me").json()
    assert re.fullmatch(r"CR-[0-9]{4}-[0-9]{4}", local["public_id"])
    assert direct.json()["public_id"] != local["public_id"]
