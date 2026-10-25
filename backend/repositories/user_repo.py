"""Repository for user data access."""

from uuid import UUID

from sqlalchemy import func, inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession
from sqlmodel import or_, select

from backend.core.user_identity import generate_public_id
from backend.db.models.user import User


class UserRepository:
    """Repository for user data persistence.

    This class handles direct database operations for User entities using SQLModel.

    Attributes:
        _db_session (Session): The database session used for queries and transactions.
    """

    def __init__(self, db_session: DBSession):
        """Initializes the UserRepository with a database session.

        Args:
            db_session (Session): The active SQLModel session.
        """
        self._db_session = db_session

    def get_all(self) -> list[User]:
        """Retrieves all users from the database.

        Returns:
            list[User]: A list containing all User entities.
        """
        return list(self._db_session.exec(select(User)).all())

    def get_by_id(self, user_id: UUID, *, fresh: bool = False) -> User | None:
        """Retrieves a user by their unique ID.

        Args:
            user_id (UUID): The ID of the user to retrieve.
            fresh: Reload membership changed by another join request.

        Returns:
            User | None: The User object if found, otherwise None.
        """
        return self._db_session.get(User, user_id, populate_existing=fresh)

    def get_by_email(self, email: str) -> User | None:
        """Retrieves a user by their email address.

        Args:
            email (str): The email address to search for.

        Returns:
            User | None: The User object if found, otherwise None.
        """
        return self._db_session.exec(
            select(User).where(User.email == email.lower())
        ).first()

    def get_by_username(self, username: str) -> User | None:
        """Retrieves a user by their username.

        Args:
            username (str): The username to search for.

        Returns:
            User | None: The User object if found, otherwise None.
        """
        return self._db_session.exec(
            select(User).where(User.username == username.lower())
        ).first()

    def get_by_room(self, room_id: UUID) -> list[User]:
        """Retrieve all users currently in a specific room.

        Args:
            room_id: The room ID to filter users by.

        Returns:
            List of users whose room_id matches the given ID.
        """
        return list(
            self._db_session.exec(select(User).where(User.room_id == room_id)).all()
        )

    def count_by_room(self, room_id: UUID) -> int:
        """Count users currently in a specific room.

        Args:
            room_id: The room ID to count users for.

        Returns:
            Number of users in the room.
        """
        return self._db_session.exec(
            select(func.count()).where(User.room_id == room_id)
        ).one()

    def get_by_identifier(self, identifier: str) -> User | None:
        """Retrieves a user by their identifier (email or username).

        Args:
            identifier (str): The identifier to search for.

        Returns:
            User | None: The User object if found, otherwise None.
        """
        return self._db_session.exec(
            select(User).where(
                or_(
                    User.email == identifier.lower(),
                    User.username == identifier.lower(),
                )
            )
        ).first()

    def save(self, user: User) -> User:
        """Saves a user entity to the database.

        Args:
            user (User): The User model instance to save.

        Returns:
            User: The saved User instance, refreshed from the database.

        Raises:
            IntegrityError: If a unique constraint is violated
                (duplicate email/username).
        """
        state = inspect(user)
        new_user = state.transient or state.pending
        for attempt in range(10):
            try:
                self._db_session.add(user)
                self._db_session.commit()
                self._db_session.refresh(user)
                return user
            except IntegrityError as error:
                self._db_session.rollback()
                constraint = getattr(
                    getattr(error.orig, "diag", None), "constraint_name", None
                )
                public_id_collision = (
                    constraint == "uq_users_public_id"
                    or str(error.orig) == "UNIQUE constraint failed: users.public_id"
                )
                if not new_user or not public_id_collision or attempt == 9:
                    raise
                user.public_id = generate_public_id()

    def delete(self, user: User) -> None:
        """Deletes a user entity from the database.

        Args:
            user (User): The User model instance to delete.
        """
        try:
            self._db_session.delete(user)
            self._db_session.commit()
        except IntegrityError:
            self._db_session.rollback()
            raise
