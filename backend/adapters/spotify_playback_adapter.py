"""Spotify playback control adapter for user-scoped API calls."""

from dataclasses import dataclass

import httpx


@dataclass
class SpotifyPlaybackState:
    """Current playback state from Spotify."""

    is_playing: bool
    track_id: str | None
    progress_ms: int | None
    duration_ms: int | None
    device_id: str | None
    track_name: str | None = None
    track_artist: str | None = None
    album_art_url: str | None = None


class SpotifyPlaybackAdapter:
    """Adapter for controlling Spotify playback via user-scoped OAuth tokens."""

    BASE_URL = "https://api.spotify.com/v1/me/player"

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def play(self, track_uri: str, device_id: str | None = None) -> str | None:
        """Start or resume playback of a track on Spotify.

        Args:
            track_uri: Spotify track URI (e.g., "spotify:track:abc123").
            device_id: Target device ID. Uses active device if None.

        Raises:
            httpx.HTTPStatusError: If Spotify rejects the request.
        """
        return await self._start_playback(device_id, track_uri)

    async def resume(self, device_id: str | None = None) -> str | None:
        """Resume playback without changing the current track.

        Args:
            device_id: Target device ID. Uses active device if None.

        Raises:
            httpx.HTTPStatusError: If Spotify rejects the request.
        """
        return await self._start_playback(device_id)

    async def get_devices(self) -> list[dict]:
        """Read currently available Spotify Connect devices without waking them."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url=self.BASE_URL + "/devices", headers=self._headers()
            )
            response.raise_for_status()
        return [
            {
                "id": device.get("id"),
                "name": device.get("name", "Spotify device"),
                "type": device.get("type", "unknown"),
                "is_active": bool(device.get("is_active")),
                "is_restricted": bool(device.get("is_restricted")),
            }
            for device in response.json().get("devices", [])
        ]

    async def transfer(self, device_id: str) -> None:
        """Select a device while preserving its current playing or paused state."""
        async with httpx.AsyncClient() as client:
            response = await client.put(
                url=self.BASE_URL,
                headers=self._headers(),
                json={"device_ids": [device_id], "play": False},
            )
            response.raise_for_status()

    async def _start_playback(
        self, device_id: str | None, track_uri: str | None = None
    ) -> str | None:
        """Retry one missing-device response using a fresh Connect device list."""
        async with httpx.AsyncClient() as client:
            for attempt in range(2):
                kwargs = {"json": {"uris": [track_uri]}} if track_uri else {}
                response = await client.put(
                    url=self.BASE_URL + "/play",
                    headers=self._headers(),
                    params={"device_id": device_id} if device_id else {},
                    **kwargs,
                )
                if response.status_code != 404 or attempt:
                    response.raise_for_status()
                    return device_id
                devices = [
                    device
                    for device in await self.get_devices()
                    if device["id"] and not device["is_restricted"]
                ]
                preferred = next(
                    (device for device in devices if device["id"] == device_id), None
                )
                active = next(
                    (device for device in devices if device["is_active"]), None
                )
                device = preferred or active or next(iter(devices), None)
                if not device:
                    response.raise_for_status()
                device_id = device["id"]
                await self.transfer(device_id)

    async def pause(self, device_id: str | None = None) -> None:
        """Pause the user's current playback.

        Raises:
            httpx.HTTPStatusError: If Spotify rejects the request.
        """
        async with httpx.AsyncClient() as client:
            kwargs = {"params": {"device_id": device_id}} if device_id else {}
            response = await client.put(
                url=self.BASE_URL + "/pause",
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()

    async def skip(self) -> None:
        """Skip to the next track in the user's Spotify queue.

        Raises:
            httpx.HTTPStatusError: If Spotify rejects the request.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url=self.BASE_URL + "/next",
                headers=self._headers(),
            )
            response.raise_for_status()

    async def get_current_playback(self) -> SpotifyPlaybackState | None:
        """Get current playback state from Spotify.

        Returns:
            SpotifyPlaybackState if active playback, None if no active device.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url=self.BASE_URL,
                headers=self._headers(),
            )
            if response.status_code == 204:
                return None
            response.raise_for_status()

        data = response.json()
        item = data.get("item")
        images = item.get("album", {}).get("images", []) if item else []
        return SpotifyPlaybackState(
            is_playing=data.get("is_playing", False),
            track_id=item["id"] if item else None,
            progress_ms=data.get("progress_ms"),
            duration_ms=item.get("duration_ms") if item else None,
            device_id=data.get("device", {}).get("id"),
            track_name=item.get("name") if item else None,
            track_artist=(
                item["artists"][0]["name"] if item and item.get("artists") else None
            ),
            album_art_url=images[0]["url"] if images else None,
        )
