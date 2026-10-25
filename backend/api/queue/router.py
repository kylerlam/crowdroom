"""Queue management routes for the API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.auth.dependencies import get_current_user
from backend.api.playback.dependencies import get_playback_control_service
from backend.api.queue.dependencies import (
    get_queue_history_repo,
    get_queue_service,
    get_session_repo,
)
from backend.api.rooms.dependencies import get_room_service
from backend.api.session.dependencies import get_queue_vote_service
from backend.core.exceptions import EntityNotFoundException
from backend.db.models.enum import ItemStatus
from backend.db.models.session import Session
from backend.db.models.user import User
from backend.repositories.session_repo import SessionRepository
from backend.schemas.queue_history import ReadQueueHistory
from backend.schemas.queue_item import (
    CreateQueueItem,
    ReadQueueItem,
    ReadQueueItemDetail,
    UpdateQueuePin,
)
from backend.schemas.queue_vote import CreateQueueVote, ReadQueueVote
from backend.services.playback_control_service import (
    PlaybackControlService,
    _room_playback_lock,
)
from backend.services.queue_service import QueueService
from backend.services.queue_vote_service import QueueVoteService
from backend.services.room_service import RoomService

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get(
    "/current",
    response_model=ReadQueueItemDetail | None,
    status_code=status.HTTP_200_OK,
)
def get_current_song(
    session_id: UUID,
    queue_service: QueueService = Depends(get_queue_service),
    session_repo: SessionRepository = Depends(get_session_repo),
) -> ReadQueueItemDetail | None:
    """Return the queue item that is actually being played.

    Returns the queue item at position 0 only when its song's external_id
    matches the session's current_song_id (i.e. the queue owns the
    currently-playing track). When an external/off-script track is playing
    while a queued song waits at position 0, returns None so callers know
    to resolve the playing track elsewhere.

    Args:
        session_id: The session whose current song to retrieve.
        queue_service: Dependency-injected queue service instance.
        session_repo: Dependency-injected session repository.

    Returns:
        ReadQueueItemDetail when the first queue item is the playing
        track, otherwise None.
    """
    item = queue_service.get_current_song(session_id)
    if item is None or item.song is None:
        return None
    session = session_repo.get_by_id(session_id)
    if session is None or item.song.external_id != session.current_song_id:
        return None
    return ReadQueueItemDetail.model_validate(item)


@router.get(
    "/", response_model=list[ReadQueueItemDetail], status_code=status.HTTP_200_OK
)
def get_queue(
    session_id: UUID,
    queue_service: QueueService = Depends(get_queue_service),
) -> list[ReadQueueItemDetail]:
    """Retrieve the full queue for a session, ordered by position.

    Returns all queued items sorted by group priority (manual first) then
    ascending position number, with nested song and user data.

    Args:
        session_id: The session whose queue to retrieve.
        queue_service: Dependency-injected queue service instance.

    Returns:
        A list of ReadQueueItemDetail schemas ordered by playback position.
    """
    items = queue_service.get_queue(session_id)
    return [ReadQueueItemDetail.model_validate(item) for item in items]


@router.post("/", response_model=ReadQueueItem, status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    data: CreateQueueItem,
    queue_service: QueueService = Depends(get_queue_service),
    current_user: User = Depends(get_current_user),
    playback_control: PlaybackControlService = Depends(get_playback_control_service),
) -> ReadQueueItem:
    """Add a song to the end of a specific queue group.

    Uses atomic repository logic to prevent race conditions when multiple
    users add songs simultaneously. The song is placed at position max+1
    within the specified group.

    Args:
        data: Schema containing session_id, song_id, and group name.
        queue_service: Dependency-injected queue service instance.
        current_user: Authenticated user adding the song.
        playback_control: Starts the queue using the creator's Spotify if idle.

    Returns:
        ReadQueueItem schema for the newly added queue item.
    """
    item = await queue_service.add_to_queue(
        session_id=data.session_id,
        song_id=data.song_id,
        added_by_user_id=current_user.id,
        group=data.group,
    )
    await playback_control.start_if_idle(data.session_id)
    return ReadQueueItem.model_validate(item)


def _assert_queue_manager(
    queue_item_id: UUID,
    queue_service: QueueService,
    session_repo: SessionRepository,
    room_service: RoomService,
    current_user: User,
) -> Session:
    """Require the owner or a room administrator for queue management."""
    item = queue_service.get_queue_item(queue_item_id)
    session = session_repo.get_by_id(item.session_id)
    if session is None:
        raise EntityNotFoundException("Session", item.session_id)
    room_service.assert_manager(session.room_id, current_user.id)
    return session


@router.post("/{queue_item_id}/jump", response_model=ReadQueueItemDetail)
async def jump_queue_item(
    queue_item_id: UUID,
    queue_service: QueueService = Depends(get_queue_service),
    session_repo: SessionRepository = Depends(get_session_repo),
    room_service: RoomService = Depends(get_room_service),
    current_user: User = Depends(get_current_user),
) -> ReadQueueItemDetail:
    """Move a pending song to the front of its pin or ordinary group."""
    _assert_queue_manager(
        queue_item_id, queue_service, session_repo, room_service, current_user
    )
    try:
        item = await queue_service.prioritize_item(queue_item_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReadQueueItemDetail.model_validate(item)


@router.patch("/{queue_item_id}/pin", response_model=ReadQueueItemDetail)
async def pin_queue_item(
    queue_item_id: UUID,
    data: UpdateQueuePin,
    queue_service: QueueService = Depends(get_queue_service),
    session_repo: SessionRepository = Depends(get_session_repo),
    room_service: RoomService = Depends(get_room_service),
    current_user: User = Depends(get_current_user),
) -> ReadQueueItemDetail:
    """Pin at most five pending songs, or return one to the ordinary head."""
    _assert_queue_manager(
        queue_item_id, queue_service, session_repo, room_service, current_user
    )
    try:
        item = await queue_service.prioritize_item(queue_item_id, data.is_pinned)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReadQueueItemDetail.model_validate(item)


@router.post("/vote", response_model=ReadQueueVote, status_code=status.HTTP_201_CREATED)
async def vote_skip(
    data: CreateQueueVote,
    queue_vote_service: QueueVoteService = Depends(get_queue_vote_service),
    current_user: User = Depends(get_current_user),
    playback_control: PlaybackControlService = Depends(get_playback_control_service),
) -> ReadQueueVote:
    """Cast a skip vote for a specific queue item.

    A user may only vote once per queue item. If the vote count reaches
    the room's skip threshold (configurable via room settings), the
    song will automatically advance to the next track. The voting user
    is taken from the authenticated request.

    Args:
        data: Schema containing the queue_item_id to vote on.
        queue_vote_service: Dependency-injected queue vote service instance.
        current_user: Authenticated user casting the vote.
        playback_control: Starts the next queued song after a threshold skip.

    Returns:
        ReadQueueVote schema for the newly cast vote.

    Raises:
        EntityExistsException: If the user has already voted on this item.
    """
    vote = await queue_vote_service.cast_vote(
        queue_item_id=data.queue_item_id,
        user_id=current_user.id,
        playback_control=playback_control,
    )
    return ReadQueueVote.model_validate(vote)


@router.delete("/vote", status_code=status.HTTP_204_NO_CONTENT)
async def remove_vote_skip(
    queue_item_id: UUID,
    queue_vote_service: QueueVoteService = Depends(get_queue_vote_service),
    current_user: User = Depends(get_current_user),
) -> None:
    """Retract a previously cast skip vote (undo).

    Args:
        queue_item_id: UUID of the queue item to retract the vote from.
        queue_vote_service: Dependency-injected queue vote service instance.
        current_user: Authenticated user retracting the vote.

    Raises:
        EntityNotFoundException: If no vote exists for this user/item pair.
    """
    await queue_vote_service.remove_vote(
        queue_item_id=queue_item_id,
        user_id=current_user.id,
    )


@router.delete("/{queue_item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_queue(
    queue_item_id: UUID,
    queue_service: QueueService = Depends(get_queue_service),
    session_repo: SessionRepository = Depends(get_session_repo),
    room_service: RoomService = Depends(get_room_service),
    current_user: User = Depends(get_current_user),
) -> None:
    """Remove a pending song from the queue.

    Deletes the specified queue item and renumbers subsequent items to
    fill the gap. Requires the item to exist.

    Args:
        queue_item_id: The unique identifier of the queue item to remove.
        queue_service: Dependency-injected queue service instance.
        session_repo: Resolves the queue item's room.
        room_service: Checks room management permissions.
        current_user: Authenticated owner or room administrator.

    Raises:
        EntityNotFoundException: If no queue item exists with the given ID.
    """
    session = _assert_queue_manager(
        queue_item_id, queue_service, session_repo, room_service, current_user
    )
    session_id = session.id
    async with _room_playback_lock(session.room_id):
        session = session_repo.get_by_id(session_id, fresh=True)
        if session is None:
            raise EntityNotFoundException("Session", session_id)
        current = queue_service.get_current_song(session.id)
        if (
            current is not None
            and current.id == queue_item_id
            and current.song is not None
            and current.song.external_id == session.current_song_id
            and session.playback_status
            in {
                ItemStatus.NOW_PLAYING,
                ItemStatus.PLAYING,
                ItemStatus.PAUSED,
                ItemStatus.BUFFERING,
            }
        ):
            raise HTTPException(
                status_code=409,
                detail="This song is already playing. Use Skip to remove it.",
            )
        await queue_service.remove_from_queue(queue_item_id)


@router.get(
    "/history/",
    response_model=list[ReadQueueHistory],
    status_code=status.HTTP_200_OK,
)
def get_history(
    session_id: UUID,
    limit: int = 15,
    queue_history_repo=Depends(get_queue_history_repo),
) -> list[ReadQueueHistory]:
    """Retrieve recent playback history for a session.

    Returns the most recently played songs, ordered by play time
    descending, up to the specified limit.

    Args:
        session_id: The session whose history to retrieve.
        limit: Maximum number of history entries to return (default 15).
        queue_history_repo: Dependency-injected queue history repository.

    Returns:
        A list of ReadQueueHistory schemas ordered by play time descending.
    """
    entries = queue_history_repo.get_by_session(session_id, limit=limit)
    return [ReadQueueHistory.model_validate(entry) for entry in entries]
