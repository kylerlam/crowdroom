"""Bound signup and room-entry attempts without treating IPs as identities."""

import threading
import time
from collections import OrderedDict, deque

from backend.core.exceptions import TooManyRequestsException


class RequestRateLimiter:
    """Keep bounded sliding windows for this deployment's single app worker."""

    def __init__(self) -> None:
        self._windows: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, bucket: str, identity: str, limit: int, seconds: int) -> None:
        """Consume one attempt or reject it until its oldest attempt expires."""
        now = time.monotonic()
        key = (bucket, identity)
        with self._lock:
            attempts = self._windows.setdefault(key, deque())
            while attempts and attempts[0] <= now - seconds:
                attempts.popleft()
            self._windows.move_to_end(key)
            if len(attempts) >= limit:
                raise TooManyRequestsException(attempts[0] + seconds - now)
            attempts.append(now)
            # ponytail: bounded local cache; use a shared store with multiple workers.
            while len(self._windows) > 10000:
                self._windows.popitem(last=False)


request_limiter = RequestRateLimiter()


def limit_signup_from_source(source: str) -> None:
    """Apply both short bursts and an hourly account-creation ceiling."""
    request_limiter.check("signup-minute", source, 5, 60)
    request_limiter.check("signup-hour", source, 30, 3600)


def limit_guest_creation_from_source(source: str) -> None:
    """Bound new guest identities while letting people on one Wi-Fi join normally."""
    request_limiter.check("guest-minute", source, 10, 60)
    request_limiter.check("guest-hour", source, 60, 3600)
