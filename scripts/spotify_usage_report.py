"""Summarize sanitized Spotify request logs from stdin without network access.

Usage: docker logs crowdroom-app 2>&1 | python3 scripts/spotify_usage_report.py
The rolling peak counts timestamps in half-open windows [start, start + 30s).
The Web API peak excludes Accounts requests such as /api/token.
Counts cover the captured log sample, not Spotify's unpublished quota threshold.
"""

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

SOURCES = {"poll", "confirm", "search", "metadata", "interactive"}
METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
ENDPOINTS = {
    "/api/token",
    "/v1/search",
    "/v1/tracks/{id}",
    "/v1/me/player",
    "/v1/me/player/devices",
    "/v1/me/player/play",
    "/v1/me/player/pause",
    "/v1/me/player/next",
}


def _rolling_peak(timestamps):
    """Count the largest half-open 30-second window in sorted timestamps."""
    start = 0
    peak = 0
    for end, at in enumerate(timestamps):
        while at - timestamps[start] >= timedelta(seconds=30):
            start += 1
        peak = max(peak, end - start + 1)
    return peak


def summarize(lines):
    """Aggregate fixed fields only; never echo queries, headers, or log lines."""
    sources, endpoints, reasons, families = (Counter() for _ in range(4))
    timestamps = []
    web_api_timestamps = []
    total = 0
    for line in lines:
        _, marker, payload = line.partition("spotify_request ")
        if not marker:
            continue
        try:
            record = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("event") != "spotify_request":
            continue
        source = record.get("source")
        source = source if isinstance(source, str) and source in SOURCES else "other"
        endpoint = record.get("endpoint")
        endpoint = (
            endpoint if isinstance(endpoint, str) and endpoint in ENDPOINTS else "other"
        )
        method = record.get("method")
        method = method if isinstance(method, str) and method in METHODS else "other"
        status = record.get("status")
        if type(status) is not int or status != 0 and not 100 <= status <= 599:
            status = "other"
        reason = record.get("reason")
        reason = (
            reason if reason in ("QUOTA_EXCEEDED", "RATE_LIMITED") else "unspecified"
        )
        total += 1
        sources[source] += 1
        endpoints[source, endpoint, method, status] += 1
        reasons[reason] += 1
        family = (
            "accounts"
            if endpoint == "/api/token"
            else "web_api"
            if endpoint.startswith("/v1/")
            else "other"
        )
        families[family] += 1
        try:
            at = datetime.fromisoformat(record["at"])
            if at.tzinfo is not None:
                at = at.astimezone(timezone.utc)
                timestamps.append(at)
                if family == "web_api":
                    web_api_timestamps.append(at)
        except KeyError, TypeError, ValueError:
            pass

    timestamps.sort()
    web_api_timestamps.sort()
    minutes = Counter(
        at.replace(second=0, microsecond=0).isoformat() for at in timestamps
    )
    return {
        "requests": total,
        "by_source": dict(sorted(sources.items())),
        "by_api_family": dict(sorted(families.items())),
        "by_endpoint": [
            {
                "source": source,
                "endpoint": endpoint,
                "method": method,
                "status": status,
                "requests": count,
            }
            for (source, endpoint, method, status), count in sorted(
                endpoints.items(), key=lambda item: tuple(map(str, item[0]))
            )
        ],
        "by_reason": dict(sorted(reasons.items())),
        "timestamped_requests": len(timestamps),
        "first_request_at": timestamps[0].isoformat() if timestamps else None,
        "last_request_at": timestamps[-1].isoformat() if timestamps else None,
        "by_utc_minute": dict(sorted(minutes.items())),
        "peak_rolling_30s_requests": _rolling_peak(timestamps),
        "peak_rolling_30s_web_api_requests": _rolling_peak(web_api_timestamps),
        "rolling_window": "[start, start + 30 seconds)",
    }


if __name__ == "__main__":
    print(json.dumps(summarize(sys.stdin), indent=2, sort_keys=True))
