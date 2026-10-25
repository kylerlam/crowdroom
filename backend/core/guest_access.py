"""Persistent guest song-request deadlines, shared by API reads and writes."""

from datetime import datetime, timedelta, timezone

GUEST_FIRST_WAIT = timedelta(minutes=5)
GUEST_REQUEST_INTERVAL = timedelta(minutes=30)


def guest_request_available_at(user) -> datetime | None:
    """Return the next UTC request time; joining is required to start the wait."""
    if not user.is_guest or user.guest_first_joined_at is None:
        return None
    joined = user.guest_first_joined_at.replace(tzinfo=timezone.utc)
    available = joined + GUEST_FIRST_WAIT
    if user.guest_last_requested_at is not None:
        last_request = user.guest_last_requested_at.replace(tzinfo=timezone.utc)
        available = max(available, last_request + GUEST_REQUEST_INTERVAL)
    return available
