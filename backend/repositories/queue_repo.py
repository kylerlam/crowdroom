"""Repository for QueueItem data access."""

from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession
from sqlmodel import select

from backend.db.models import QueueItem
from backend.db.models.enum import ItemStatus
from backend.db.models.session import Session as SessionModel
from backend.db.models.song import Song

_ACTIVE_STATES = (
    ItemStatus.NOW_PLAYING,
    ItemStatus.PLAYING,
    ItemStatus.PAUSED,
    ItemStatus.BUFFERING,
)


def _get_dialect_name(session: DBSession) -> str:
    """Determine the active database dialect (sqlite, postgresql, etc).

    Inspects the session's bound engine to identify which database backend
    is in use. Falls back to checking the connection URL string, and
    finally to assuming SQLite.

    Args:
        session: Database session with an active engine connection.

    Returns:
        A lowercase string identifying the database dialect name.
    """
    bind = session.get_bind()
    if bind is not None:
        return bind.dialect.name
    # Fallback: try to detect from connection URL
    from sqlalchemy import create_engine

    conn_str = str(session.bind.url) if session.bind else ""
    if conn_str:
        tmp_engine = create_engine(conn_str)
        return tmp_engine.dialect.name
    return "sqlite"


def _make_lock(
    session: DBSession, session_id: UUID | None = None, group: str | None = None
):
    """Return a context-managed queue lock suitable for the active database.

    SQLite uses BEGIN IMMEDIATE (file-level lock). PostgreSQL uses
    pg_advisory_xact_lock (application-level lock) with an MD5-derived
    key for uniqueness per session and group.

    Args:
        session: Database session for executing the lock query.
        session_id: Optional UUID identifying the session.
        group: Optional queue group name for lock key derivation.

    Returns:
        A context manager implementing SQLiteQueueLock or PGQueueLock.
    """
    dialect = _get_dialect_name(session)

    if dialect == "postgresql":
        from backend.services.drivers.postgres import PGQueueLock

        return PGQueueLock(session, session_id, group)  # type: ignore[arg-type]
    else:
        # Default: SQLite (BEGIN IMMEDIATE)
        from backend.services.drivers.sqlite import SQLiteQueueLock

        return SQLiteQueueLock(session)


class QueueRepository:
    """Data access layer for queue items."""

    def __init__(self, session: DBSession) -> None:
        """Initialize the QueueRepository with a database session.

        Args:
            session: Database session for all CRUD operations.
        """
        self._session = session

    def create(self, queue_item: QueueItem) -> QueueItem:
        """Persist a new queue item to the database.

        Raises:
            IntegrityError: If unique constraint violated (duplicate position).
        """
        try:
            self._session.add(queue_item)
            self._session.commit()
            self._session.refresh(queue_item)
            return queue_item
        except IntegrityError:
            self._session.rollback()
            raise

    def delete(self, queue_item_id: UUID) -> bool:
        """Delete a queue item and shift subsequent positions down.

        Atomic under both SQLite and PostgreSQL via the configured locker.
        Returns True if deleted, False if item didn't exist.

        Raises:
            IntegrityError: If a constraint violation occurs during shift.
        """
        self._session.expire_on_commit = False

        with _make_lock(self._session):
            queue_item = self._session.get(QueueItem, queue_item_id)
            if not queue_item:
                return False

            session_id = queue_item.session_id
            group = queue_item.group
            position = queue_item.position

            try:
                self._session.delete(queue_item)
                self._session.flush()

                subsequent = self._session.exec(
                    select(QueueItem)
                    .where(
                        QueueItem.session_id == session_id,
                        QueueItem.group == group,
                        QueueItem.position > position,
                    )
                    .order_by(QueueItem.position)
                ).all()

                for item in subsequent:
                    item.position -= 1
                    self._session.add(item)
                    self._session.flush()

                self._session.commit()
                return True
            except IntegrityError:
                self._session.rollback()
                raise

        return False

    def add_to_queue_atomic(
        self,
        session_id: UUID,
        song_id: UUID,
        added_by_user_id: UUID | None = None,
        group: str = "manual",
    ) -> QueueItem:
        """Atomically read max position and create a new queue item.

        Uses the configured database locker to serialize concurrent writes:
          - SQLite: BEGIN IMMEDIATE (file-level lock)
          - PostgreSQL: pg_advisory_xact_lock() (application-level lock)

        The unique constraint on (session_id, group, position) is the final
        safety net — if two threads somehow slip through, the database raises
        an IntegrityError instead of silently corrupting data.

        Raises:
            IntegrityError: If a unique constraint violation occurs.
        """
        with _make_lock(self._session, session_id, group):
            stmt = select(func.coalesce(func.max(QueueItem.position), -1)).where(
                QueueItem.session_id == session_id,
                QueueItem.group == group,
            )
            max_pos = self._session.exec(stmt).one()

            queue_item = QueueItem(
                session_id=session_id,
                song_id=song_id,
                added_by_user_id=added_by_user_id,
                position=max_pos + 1,
                group=group,
            )
            self._session.add(queue_item)
            self._session.commit()

            # Refresh outside the lock to get back the generated PK
            self._session.refresh(queue_item)
            return queue_item

    def get_by_id(self, queue_item_id: UUID) -> QueueItem | None:
        """Fetch a single queue item by its primary key.

        Args:
            queue_item_id: The UUID of the queue item to retrieve.

        Returns:
            The QueueItem if found, otherwise None.
        """
        return self._session.get(QueueItem, queue_item_id)

    def get_all_by_session(self, session_id: UUID) -> list[QueueItem]:
        """Retrieve all queue items for a session.

        The active track stays first, followed by pins and manual priorities.
        """
        stmt = (
            select(QueueItem)
            .where(QueueItem.session_id == session_id)
            .order_by(*self._queue_order(session_id))
        )
        return self._session.exec(stmt).all()

    def _current_item_id(self, session_id: UUID) -> UUID | None:
        """Resolve the playing entry without confusing repeated songs."""
        session = self._session.get(SessionModel, session_id)
        if (
            session is None
            or not session.current_song_id
            or session.playback_status not in _ACTIVE_STATES
        ):
            return None
        return self._session.exec(
            select(QueueItem.id)
            .join(Song, QueueItem.song_id == Song.id)
            .where(
                QueueItem.session_id == session_id,
                Song.external_id == session.current_song_id,
            )
            .order_by(
                case((QueueItem.playback_status.in_(_ACTIVE_STATES), 0), else_=1),
                case((QueueItem.group == "manual", 0), else_=1),
                QueueItem.position,
            )
            .limit(1)
        ).first()

    def _queue_order(self, session_id: UUID) -> tuple:
        """Use the same ordering for display and playback selection."""
        current_id = self._current_item_id(session_id)
        return (
            case((QueueItem.id == current_id, 0), else_=1),
            case((QueueItem.is_pinned.is_(True), 0), else_=1),
            case((QueueItem.priority_position.is_not(None), 0), else_=1),
            QueueItem.priority_position,
            case((QueueItem.group == "manual", 0), else_=1),
            QueueItem.position,
        )

    def prioritize_item(
        self, queue_item_id: UUID, is_pinned: bool | None = None
    ) -> QueueItem:
        """Atomically jump or pin a pending track without changing the player.

        Explicit priorities are independent of skip votes and the original
        manual/playlist positions. A single session lock serializes pin-cap
        checks and ordering changes across both groups.
        """
        item = self.get_by_id(queue_item_id)
        if item is None:
            raise ValueError("Queue item no longer exists")
        with _make_lock(self._session, item.session_id, "priority"):
            try:
                self._session.refresh(item)
                current_id = self._current_item_id(item.session_id)
                if item.id == current_id:
                    raise ValueError("The currently playing song cannot be reordered")
                pending = [
                    other
                    for other in self.get_all_by_session(item.session_id)
                    if other.id != current_id and other.id != item.id
                ]
                if is_pinned is not None and item.is_pinned == is_pinned:
                    self._session.commit()
                    return item
                pinned = item.is_pinned if is_pinned is None else is_pinned
                peers = [other for other in pending if other.is_pinned == pinned]
                if is_pinned is True and len(peers) >= 5:
                    raise ValueError("You can pin up to 5 songs")
                positions = [
                    other.priority_position
                    for other in peers
                    if other.priority_position is not None
                ]
                if is_pinned is True:
                    item.priority_position = max(positions, default=-1) + 1
                else:
                    item.priority_position = min(positions, default=1) - 1
                item.is_pinned = pinned
                self._session.add(item)
                self._session.commit()
                self._session.refresh(item)
                return item
            except Exception:
                self._session.rollback()
                raise

    def get_max_position_in_group(self, session_id: UUID, group: str) -> int:
        """Get the maximum position within a specific group for a session.

        Returns -1 if the group is empty.
        """
        stmt = select(func.coalesce(func.max(QueueItem.position), -1)).where(
            QueueItem.session_id == session_id, QueueItem.group == group
        )
        return self._session.exec(stmt).one()

    def get_first_item(self, session_id: UUID) -> QueueItem | None:
        """Retrieve the currently playing song (lowest position) for a session.

        Args:
            session_id: The session whose current song to retrieve.

        Returns:
            The QueueItem at the lowest position, or None if queue is empty.
        """
        stmt = (
            select(QueueItem)
            .where(QueueItem.session_id == session_id)
            .order_by(*self._queue_order(session_id))
            .limit(1)
        )
        return self._session.exec(stmt).first()

    def get_by_session_and_position(
        self, session_id: UUID, position: int
    ) -> QueueItem | None:
        """Fetch a queue item at a specific position within a session.

        Args:
            session_id: The session to search within.
            position: The zero-based index of the item to retrieve.

        Returns:
            The QueueItem at the given position, or None if not found.
        """
        stmt = select(QueueItem).where(
            QueueItem.session_id == session_id,
            QueueItem.position == position,
        )
        return self._session.exec(stmt).first()

    def count_by_session(self, session_id: UUID) -> int:
        """Count all queue items belonging to a session.

        Args:
            session_id: The session whose queue to count items for.

        Returns:
            The total number of queue items in the session.
        """
        stmt = select(func.count()).where(QueueItem.session_id == session_id)
        return self._session.exec(stmt).one()
