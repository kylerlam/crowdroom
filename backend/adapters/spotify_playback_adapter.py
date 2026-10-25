"""Spotify playback control adapter for user-scoped API calls."""

import asyncio
from dataclasses import dataclass

import httpx

from backend.core.exceptions import SpotifyUpstreamException


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

    async def play(
        self,
        track_uri: str,
        device_id: str | None = None,
        position_ms: int | None = None,
    ) -> str | None:
        """Start or resume playback of a track on Spotify.

        Args:
            track_uri: Spotify track URI (e.g., "spotify:track:abc123").
            device_id: Target device ID. Uses active device if None.
            position_ms: Saved playback position when restoring a track on a device.

        Raises:
            httpx.HTTPStatusError: If Spotify rejects the request.
        """
        return await self._start_playback(device_id, track_uri, position_ms)

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

    async def prepare_device(
        self,
        device_id: str | None = None,
        *,
        force_transfer: bool = False,
        transfer_if_inactive: bool = True,
    ) -> str:
        """Select an available device before sending an explicit playback command.

        Args:
            device_id: Selected target. Never silently substitutes another device.
            force_transfer: Re-activate once after an explicit missing-device error.
            transfer_if_inactive: Transfer during explicit selection, not initial Play.

        Returns:
            The available device ID. Actual playback is confirmed after Play.
        """
        devices = [
            device
            for device in await self.get_devices()
            if device["id"] and not device["is_restricted"]
        ]
        if device_id:
            device = next((item for item in devices if item["id"] == device_id), None)
        else:
            device = next((item for item in devices if item["is_active"]), None)
            device = device or next(iter(devices), None)
        if not device:
            raise SpotifyUpstreamException(
                status_code=404,
                detail="The selected Spotify device is unavailable. "
                "Open Spotify on that device and refresh the device list.",
            )
        device_id = device["id"]
        if not force_transfer and (device["is_active"] or not transfer_if_inactive):
            return device_id
        await self.transfer(device_id)
        # With no playback context, transfer(play=False) may leave an available
        # phone inactive. The explicit URI Play creates its context; only that
        # command's resulting playback state can prove it started successfully.
        return device_id

    async def _confirm_playback(self, device_id: str, track_uri: str | None) -> None:
        """Require real playback on the requested device after Spotify accepts Play."""
        track_id = track_uri.rsplit(":", 1)[-1] if track_uri else None
        for attempt in range(5):
            try:
                state = await self.get_current_playback()
            except httpx.HTTPStatusError as error:
                # Play was already accepted. Do not let a failed confirmation read
                # trigger another Play that could reset the song's position.
                raise SpotifyUpstreamException(
                    status_code=error.response.status_code,
                    detail="Spotify accepted Play but playback could not be verified. "
                    "Refresh playback status before trying again.",
                ) from error
            if (
                state
                and state.is_playing
                and state.device_id == device_id
                and (track_id is None or state.track_id == track_id)
            ):
                return
            if attempt < 4:
                await asyncio.sleep(0.4)
        raise SpotifyUpstreamException(
            status_code=502,
            detail="Spotify has not started the requested song on the selected device. "
            "Open Spotify there and play a song once, "
            "then return here and press Resume.",
        )

    async def _start_playback(
        self,
        device_id: str | None,
        track_uri: str | None = None,
        position_ms: int | None = None,
    ) -> str | None:
        """Prepare the target, issue Play once, and verify the real track and device."""
        device_id = await self.prepare_device(device_id, transfer_if_inactive=False)
        async with httpx.AsyncClient() as client:
            for attempt in range(2):
                body = {"uris": [track_uri]} if track_uri else {}
                if position_ms is not None:
                    body["position_ms"] = max(0, position_ms)
                kwargs = {"json": body} if body else {}
                response = await client.put(
                    url=self.BASE_URL + "/play",
                    headers=self._headers(),
                    params={"device_id": device_id} if device_id else {},
                    **kwargs,
                )
                if response.status_code != 404 or attempt:
                    response.raise_for_status()
                    await self._confirm_playback(device_id, track_uri)
                    return device_id
                device_id = await self.prepare_device(device_id, force_transfer=True)

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
