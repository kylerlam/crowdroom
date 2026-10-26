"""Search functionality routes for the API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette import status

from backend.api.auth.dependencies import get_current_user
from backend.api.rooms.dependencies import get_room_service
from backend.api.search.dependencies import (
    get_music_service,
    get_session_repo,
    get_song_service,
)
from backend.api.websocket import manager
from backend.core.exceptions import EntityNotFoundException, SpotifyRateLimitedException
from backend.db.models.enum import StreamingPlatforms
from backend.db.models.song import Song
from backend.db.models.user import User
from backend.repositories.session_repo import SessionRepository
from backend.schemas.song import CreateSong
from backend.schemas.song_metadata import ReadSongMetadata
from backend.services.music_service import MusicService
from backend.services.room_service import RoomService
from backend.services.song_service import SongService

router = APIRouter(prefix="/search", tags=["search"])


def _saved_metadata(song: Song) -> ReadSongMetadata:
    """Return stored fields in the same shape as online search results."""
    return ReadSongMetadata(
        title=song.title,
        artist=song.artist,
        artists=[song.artist],
        duration_ms=int(song.duration * 1000),
        external_id=song.external_id,
        platform=song.platform,
        album_art_url=song.album_art_url,
        is_explicit=bool(song.is_explicit),
    )


def _room_platform(
    room_id: UUID,
    user: User,
    room_service: RoomService,
    session_repo: SessionRepository,
) -> StreamingPlatforms:
    """Require real membership before resolving the room's search platform."""
    room_service.assert_member(room_id, user.id)
    session = session_repo.get_by_room(room_id)
    if session is None:
        raise EntityNotFoundException("Session", room_id)
    return session.current_platform


def _persist_song(metadata: ReadSongMetadata, song_service: SongService) -> None:
    if metadata.external_id is None or metadata.platform is None:
        return

    song_data = CreateSong(
        external_id=metadata.external_id,
        title=metadata.title,
        artist=metadata.artist,
        platform=metadata.platform,
        duration=metadata.duration_ms / 1000 if metadata.duration_ms else 0.0,
        album_art_url=metadata.album_art_url,
        is_explicit=metadata.is_explicit,
    )
    song_service.get_or_create_song(song_data)


@router.get(
    "/",
    response_model=list[ReadSongMetadata],
    status_code=status.HTTP_200_OK,
)
async def search_tracks(
    room_id: UUID,
    q: str = Query(..., min_length=1, max_length=255),
    current_user: User = Depends(get_current_user),
    music_service: MusicService = Depends(get_music_service),
    song_service: SongService = Depends(get_song_service),
    room_service: RoomService = Depends(get_room_service),
    session_repo: SessionRepository = Depends(get_session_repo),
) -> list[ReadSongMetadata]:
    """Search tracks on the room's streaming platform.

    Args:
        room_id: Room whose platform connection to use.
        q: Search query string.
        current_user: Authenticated user from JWT.
        music_service: Service for platform search.
        song_service: Service for song persistence.
        room_service: Enforces current room membership.
        session_repo: Resolves the room's platform without an external request.

    Returns:
        List of matching track metadata.

    Raises:
        HTTPException: 404 if room or platform connection not found.
    """
    try:
        platform = _room_platform(room_id, current_user, room_service, session_repo)
        results = await music_service.search(room_id, q)
    except SpotifyRateLimitedException as exc:
        await manager.broadcast(
            {
                "type": "spotify_rate_limited",
                "room_id": str(room_id),
                "retry_at": exc.retry_at.isoformat(),
                "retry_after": exc.retry_after,
            },
            str(room_id),
        )
        return [
            _saved_metadata(song)
            for song in song_service.search_saved(q, platform.value)
        ]
    except EntityNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    for metadata in results:
        _persist_song(metadata, song_service)

    return results


@router.get(
    "/{external_id}",
    response_model=ReadSongMetadata,
    status_code=status.HTTP_200_OK,
)
async def get_track_metadata(
    external_id: str,
    room_id: UUID,
    current_user: User = Depends(get_current_user),
    music_service: MusicService = Depends(get_music_service),
    song_service: SongService = Depends(get_song_service),
    room_service: RoomService = Depends(get_room_service),
    session_repo: SessionRepository = Depends(get_session_repo),
) -> ReadSongMetadata:
    """Retrieve metadata for a track by its external platform ID.

    Args:
        external_id: Platform-specific track identifier.
        room_id: Room whose platform connection to use.
        current_user: Authenticated user from JWT.
        music_service: Service for platform metadata lookup.
        song_service: Service for song persistence.
        room_service: Enforces current room membership.
        session_repo: Resolves the room's platform without an external request.

    Returns:
        Track metadata from the streaming platform.

    Raises:
        HTTPException: 404 if room/connection not found or track not found.
    """
    try:
        platform = _room_platform(room_id, current_user, room_service, session_repo)
        saved = song_service.get_by_external_id(external_id, platform.value)
        if saved is not None:
            return _saved_metadata(saved)
        metadata = await music_service.get_metadata(room_id, external_id)
    except EntityNotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    if metadata is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Track '{external_id}' not found on platform.",
        )

    _persist_song(metadata, song_service)

    return metadata
