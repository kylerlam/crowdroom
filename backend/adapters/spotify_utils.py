"""Shared utilities for Spotify API communication."""

import base64

import httpx

from backend.core.exceptions import InvalidPlatformCredentialsException
from backend.core.spotify_rate_limit import spotify_request


async def request_token(credentials: dict[str, str]) -> dict:
    """Request an access token via Spotify Client Credentials flow.

    Args:
        credentials: Dict with ``client_id`` and ``client_secret`` keys.

    Returns:
        Parsed JSON response containing ``access_token`` and ``expires_in``.

    Raises:
        InvalidPlatformCredentialsException: If keys are missing or Spotify
            rejects the credentials.
    """
    required = {"client_id", "client_secret"}
    missing = required - credentials.keys()
    if missing:
        raise InvalidPlatformCredentialsException(f"Missing keys: {missing}")

    client_id = credentials["client_id"]
    client_secret = credentials["client_secret"]

    authorization_str = f"{client_id}:{client_secret}"
    authorization_bytes = authorization_str.encode("utf-8")
    authorization_base64 = base64.b64encode(authorization_bytes).decode("utf-8")
    authorization_header = f"Basic {authorization_base64}"

    async with httpx.AsyncClient() as client:
        response = await spotify_request(
            client.post,
            client_id=client_id,
            url="https://accounts.spotify.com/api/token",
            headers={"Authorization": authorization_header},
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            raise InvalidPlatformCredentialsException("Spotify")
        return response.json()
