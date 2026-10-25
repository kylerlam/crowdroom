"""Service for handling user authentication and registration."""

from uuid import UUID

import jwt

from backend.core.exceptions import (
    EntityExistsException,
    ForbiddenException,
    InvalidCredentialsException,
    PasswordRequiredException,
)
from backend.core.rate_limit import (
    limit_guest_creation_from_source,
    limit_signup_from_source,
)
from backend.core.security import SecurityService
from backend.db.models.enum import TokenType
from backend.db.models.user import User
from backend.schemas.auth import (
    GuestLoginRequest,
    LocalLoginRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
)
from backend.schemas.user import UserCreate
from backend.services.user_service import UserService


class AuthService:
    """Service for handling user authentication and registration."""

    def __init__(self, user_service: UserService, security_service: SecurityService):
        """Initialize the AuthService with its dependencies.

        Args:
            user_service: UserService for user lookups and management.
            security_service: SecurityService for JWT and password operations.
        """
        self._user_service = user_service
        self._security_service = security_service

    def register_user(self, user_data: RegisterRequest) -> TokenResponse:
        """Registers a new user and returns an access token.

        Args:
            user_data (RegisterRequest): The schema containing registration details.

        Returns:
            TokenResponse: Access token for immediate login.

        Raises:
            EntityExistsException: If the username or email is already taken.
        """
        if self._user_service.get_by_username(user_data.username):
            raise EntityExistsException("Username already taken")
        if user_data.email and self._user_service.get_by_email(str(user_data.email)):
            raise EntityExistsException("Email already in use")

        user_to_create = UserCreate(
            username=user_data.username,
            email=user_data.email,
            password=user_data.password,
        )
        created_user = self._user_service.create_user(user_to_create)

        token = self._security_service.create_token(
            token_type=TokenType.ACCESS,
            data={"sub": str(created_user.id), "ver": created_user.token_version},
        )
        return TokenResponse(access_token=token, token_type="bearer")

    def local_login(
        self, user_data: LocalLoginRequest, source_ip: str | None = None
    ) -> TokenResponse:
        """Authenticate or auto-register a user in LOCAL mode.

        If a password is provided and the user doesn't exist, creates a
        password-protected account. If the user exists with a password,
        verifies it. Nickname-only entry must use the signed guest flow.

        Args:
            user_data: The schema containing username and optional password.
            source_ip: Server-resolved client address for new-account limits.

        Returns:
            TokenResponse with access token.

        Raises:
            InvalidCredentialsException: If password verification fails.
        """
        username = user_data.username.strip().lower()
        plain_password = (
            user_data.password.get_secret_value() if user_data.password else None
        )
        user = self._user_service.get_by_username(username)

        if not plain_password:
            if user and user.hashed_password:
                raise PasswordRequiredException()
            raise InvalidCredentialsException(
                "Use the guest entry to join without a password"
            )
        if not user:
            if source_ip is not None:
                limit_signup_from_source(source_ip)
            user_to_create = UserCreate(
                username=username, email=None, password=plain_password
            )
            user = self._user_service.create_user(user_to_create)
        elif user.hashed_password:
            if not self._security_service.verify_password(
                plain_password, user.hashed_password
            ):
                raise InvalidCredentialsException()
        else:
            raise InvalidCredentialsException(
                "Use the guest entry to restore your session"
            )

        token = self._security_service.create_token(
            token_type=TokenType.ACCESS,
            data={"sub": str(user.id), "ver": user.token_version},
        )

        return TokenResponse(access_token=token, token_type="bearer")

    def guest_login(
        self,
        user_data: GuestLoginRequest,
        identity_token: str | None,
        source_ip: str,
        *,
        local_login: bool = False,
    ) -> tuple[TokenResponse, str]:
        """Resume a signed guest identity or create one without trusting a nickname."""
        if local_login:
            existing = self._user_service.get_by_username(user_data.username.lower())
            if existing and existing.hashed_password:
                raise PasswordRequiredException()
        user = None
        if identity_token:
            try:
                payload = self._security_service.decode_token(
                    identity_token, expected_type=TokenType.GUEST_IDENTITY
                )
                candidate = self._user_service.get_by_id(UUID(payload["sub"]))
                if (
                    candidate is not None
                    and candidate.is_guest
                    and payload.get("ver") == candidate.token_version
                ):
                    user = candidate
            except jwt.InvalidTokenError, ValueError, KeyError, TypeError:
                # Invalid, expired or deleted identities receive a fresh limited guest.
                pass
        if user is None:
            limit_guest_creation_from_source(source_ip)
            user = self._user_service.create_guest(user_data.username)
        elif user.guest_name != user_data.username:
            from backend.schemas.user import UserUpdate

            self._user_service.update_user(
                user.id, UserUpdate(username=user_data.username)
            )

        claims = {"sub": str(user.id), "ver": user.token_version}
        token = self._security_service.create_token(TokenType.ACCESS, claims)
        guest_identity = self._security_service.create_token(
            TokenType.GUEST_IDENTITY, claims
        )
        return TokenResponse(access_token=token), guest_identity

    def login_user(self, user_data: LoginRequest) -> TokenResponse:
        """Authenticates a user and returns a token.

        Args:
            user_data (LoginRequest): The schema containing login credentials.

        Returns:
            TokenResponse: The authentication token.

        Raises:
            InvalidCredentialsException: If authentication fails.
        """
        identifier = user_data.identifier.strip().lower()
        user = self._user_service.get_by_identifier(identifier)

        if not user or user.is_guest is True or not user.hashed_password:
            raise InvalidCredentialsException()

        is_valid = self._security_service.verify_password(
            user_data.password.get_secret_value(),
            user.hashed_password,
        )
        if not is_valid:
            raise InvalidCredentialsException()

        token = self._security_service.create_token(
            token_type=TokenType.ACCESS,
            data={"sub": str(user.id), "ver": user.token_version},
        )

        return TokenResponse(access_token=token, token_type="bearer")

    def set_password(self, user_id: UUID, plain_password: str) -> None:
        """Set a password on a user account that doesn't have one.

        Args:
            user_id: The user's ID.
            plain_password: The plain-text password to hash and store.
        """
        hashed = self._security_service.generate_password_hash(plain_password)
        self._user_service.update_user_password(user_id, hashed)

    def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> TokenResponse:
        """Change a user's password and reissue an access token.

        Verifies the current password, updates it, and bumps the user's
        token_version so every other active session is invalidated. Issues
        a fresh token bound to the new version so the caller stays logged in.

        Args:
            user: The authenticated user.
            current_password: The current password for verification.
            new_password: The new password to set.

        Returns:
            TokenResponse: A fresh access token bound to the bumped version.

        Raises:
            InvalidCredentialsException: If current password is incorrect.
        """
        if not user.hashed_password:
            raise InvalidCredentialsException()

        is_valid = self._security_service.verify_password(
            current_password, user.hashed_password
        )
        if not is_valid:
            raise InvalidCredentialsException()

        hashed = self._security_service.generate_password_hash(new_password)
        self._user_service.update_user_password(user.id, hashed)
        self._user_service.increment_token_version(user.id)

        refreshed = self._user_service.get_by_id(user.id)
        token = self._security_service.create_token(
            token_type=TokenType.ACCESS,
            data={"sub": str(refreshed.id), "ver": refreshed.token_version},
        )
        return TokenResponse(access_token=token, token_type="bearer")

    def complete_profile(self, user: User, email: str, plain_password: str) -> None:
        """Set email and password on an incomplete profile.

        Only updates fields that are currently missing. Skips if already set.

        Args:
            user: The authenticated user to complete.
            email: The email to set (if user has no email).
            plain_password: The plain-text password to hash and set (if no password).

        Raises:
            EntityExistsException: If the email is already taken by another user.
        """
        if user.is_guest is True:
            raise ForbiddenException("Register an account to use account features")
        normalized_email = email.strip().lower()

        if not user.email:
            existing = self._user_service.get_by_email(normalized_email)
            if existing and existing.id != user.id:
                raise EntityExistsException("Email already in use")
            from backend.schemas.user import UserUpdate

            self._user_service.update_user(user.id, UserUpdate(email=normalized_email))

        if not user.hashed_password:
            self.set_password(user.id, plain_password)

    def resolve_user_from_token(self, token: str) -> User | None:
        """Decode a JWT token and return the associated user.

        Args:
            token: Raw JWT access token string.

        Returns:
            The User if token is valid and user exists, None otherwise.
        """
        try:
            payload = self._security_service.decode_token(
                token, expected_type=TokenType.ACCESS
            )
            user_id = payload.get("sub")
            user = self._user_service.get_by_id(UUID(user_id))
            if user is None or payload.get("ver") != user.token_version:
                return None
            return user
        except Exception:
            return None
