"""Session management routes for the API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.api.auth.dependencies import get_current_user
from backend.api.rooms.dependencies import get_room_service
from backend.api.session.dependencies import get_playback_service, get_session_service
from backend.core.exceptions import EntityNotFoundException
from backend.db.models.user import User
from backend.schemas.playback import FinishResponse
from backend.schemas.session import CreateSession, ReadSession, UpdateSession
from backend.services.playback_service import PlaybackService
from backend.services.room_service import RoomService
from backend.services.session_service import SessionService

router = APIRouter(prefix="/session", tags=["session"])


@router.get("/", response_model=list[ReadSession], status_code=status.HTTP_200_OK)
async def get_sessions(
    session_service: SessionService = Depends(get_session_service),
) -> list[ReadSession]:
    """Retrieves a list of all sessions.

    Args:
        session_service (SessionService): The injected session service.

    Returns:
        list[ReadSession]: A list of session schemas.
    """
    sessions = session_service.get_all_sessions()
    return [ReadSession.model_validate(s) for s in sessions]


@router.get(
    "/by-room/{room_id}", response_model=ReadSession, status_code=status.HTTP_200_OK
)
async def get_session_by_room(
    room_id: UUID,
    session_service: SessionService = Depends(get_session_service),
) -> ReadSession:
    """Retrieves the session for a given room.

    Args:
        room_id: The unique identifier of the room.
        session_service: The injected session service.

    Returns:
        ReadSession: The session schema.
    """
    try:
        session = session_service.get_session_by_room(room_id)
    except EntityNotFoundException as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ReadSession.model_validate(session)


@router.get("/{session_id}", response_model=ReadSession, status_code=status.HTTP_200_OK)
async def get_session(
    session_id: UUID,
    session_service: SessionService = Depends(get_session_service),
) -> ReadSession:
    """Retrieves a specific session by its ID.

    Args:
        session_id (UUID): The unique identifier of the session.
        session_service (SessionService): The injected session service.

    Returns:
        ReadSession: The session schema.
    """
    session = session_service.get_session(session_id)
    return ReadSession.model_validate(session)


@router.post("/", response_model=ReadSession, status_code=status.HTTP_201_CREATED)
async def create_session(
    session_data: CreateSession,
    current_user: User = Depends(get_current_user),
    room_service: RoomService = Depends(get_room_service),
    session_service: SessionService = Depends(get_session_service),
) -> ReadSession:
    """Creates a new session.

    Args:
        session_data (CreateSession): The schema containing session creation details.
        current_user: The authenticated room manager.
        room_service: Service enforcing room management permission.
        session_service (SessionService): The injected session service.

    Returns:
        ReadSession: The newly created session schema.
    """
    room_service.assert_manager(session_data.room_id, current_user.id)
    session = session_service.create_session(session_data)
    return ReadSession.model_validate(session)


@router.patch(
    "/{session_id}", response_model=ReadSession, status_code=status.HTTP_200_OK
)
async def update_session(
    session_id: UUID,
    session_data: UpdateSession,
    current_user: User = Depends(get_current_user),
    room_service: RoomService = Depends(get_room_service),
    session_service: SessionService = Depends(get_session_service),
) -> ReadSession:
    """Updates an existing session.

    Args:
        session_id (UUID): The unique identifier of the session to update.
        session_data (UpdateSession): The schema containing the fields to update.
        current_user: The authenticated room manager.
        room_service: Service enforcing room management permission.
        session_service (SessionService): The injected session service.

    Returns:
        ReadSession: The updated session schema.
    """
    current_session = session_service.get_session(session_id)
    room_service.assert_manager(current_session.room_id, current_user.id)
    session = session_service.update_session(session_id, session_data)
    return ReadSession.model_validate(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    room_service: RoomService = Depends(get_room_service),
    session_service: SessionService = Depends(get_session_service),
) -> None:
    """Deletes a session from the system.

    Args:
        session_id (UUID): The unique identifier of the session to delete.
        request (Request): The incoming request (used to access app state).
        current_user: The authenticated room manager.
        room_service: Service enforcing room management permission.
        session_service (SessionService): The injected session service.
    """
    session = session_service.get_session(session_id)
    room_id = session.room_id
    room_service.assert_manager(room_id, current_user.id)
    session_service.delete_session(session_id)
    await request.app.state.playback_poller.stop_polling(room_id)


@router.post(
    "/{session_id}/finish",
    response_model=FinishResponse,
    status_code=status.HTTP_200_OK,
)
async def finish_current_song(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    room_service: RoomService = Depends(get_room_service),
    session_service: SessionService = Depends(get_session_service),
    playback_service: PlaybackService = Depends(get_playback_service),
) -> FinishResponse:
    """Advance to the next song by finishing the current one.

    Records the current song in history, removes it from the queue, and updates
    session status to STOPPED.

    Args:
        session_id (UUID): The unique identifier of the session.
        current_user: The authenticated room manager.
        room_service: Service enforcing room management permission.
        session_service: Service locating the session's room.
        playback_service (PlaybackService): The injected playback service.

    Returns:
        FinishResponse: Confirmation of the finished state.
    """
    session = session_service.get_session(session_id)
    room_service.assert_manager(session.room_id, current_user.id)
    new_status = await playback_service.finish_song(session_id)
    return FinishResponse(status=new_status)
