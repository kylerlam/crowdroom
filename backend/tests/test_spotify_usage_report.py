"""Offline request accounting must distinguish API families and redact raw data."""

import json
import runpy
from pathlib import Path

summarize = runpy.run_path(
    Path(__file__).resolve().parents[2] / "scripts" / "spotify_usage_report.py"
)["summarize"]


def event(at, **fields):
    return "prefix spotify_request " + json.dumps(
        {
            "event": "spotify_request",
            "at": at,
            "source": "poll",
            "endpoint": "/v1/me/player",
            "method": "GET",
            "status": 200,
            "reason": "unspecified",
            **fields,
        }
    )


def test_report_counts_real_events_and_never_echoes_unknown_fields():
    result = summarize(
        [
            "ordinary log message",
            "prefix spotify_request invalid JSON",
            'prefix spotify_request {"event": "unrelated"}',
            event("2026-10-26T03:00:00Z"),
            event(
                "2026-10-26T03:00:10Z",
                source="search",
                endpoint="/api/token",
                method="POST",
            ),
            event(
                "2026-10-26T03:01:00Z",
                source="search",
                endpoint="/v1/search",
                status=429,
                reason="QUOTA_EXCEEDED",
                query="secret query",
                authorization="secret token",
            ),
            event(
                "2026-10-26T03:01:01Z",
                source="secret source",
                endpoint="/v1/tracks/secret-id?token=secret-token",
                method="secret method",
                status="secret status",
                reason="secret reason",
            ),
        ]
    )
    assert result["requests"] == 4
    assert result["by_source"] == {"other": 1, "poll": 1, "search": 2}
    assert result["by_api_family"] == {"accounts": 1, "other": 1, "web_api": 2}
    assert result["by_reason"] == {"QUOTA_EXCEEDED": 1, "unspecified": 3}
    assert {
        "source": "search",
        "endpoint": "/v1/search",
        "method": "GET",
        "status": 429,
        "requests": 1,
    } in result["by_endpoint"]
    assert result["by_utc_minute"] == {
        "2026-10-26T03:00:00+00:00": 2,
        "2026-10-26T03:01:00+00:00": 2,
    }
    assert result["peak_rolling_30s_requests"] == 2
    assert result["peak_rolling_30s_web_api_requests"] == 1
    assert result["first_request_at"] == "2026-10-26T03:00:00+00:00"
    assert result["last_request_at"] == "2026-10-26T03:01:01+00:00"
    assert "secret" not in json.dumps(result)


def test_rolling_peak_sorts_timestamps_and_excludes_the_30_second_boundary():
    result = summarize(
        [
            event("2026-10-26T03:00:30Z"),
            event("2026-10-26T11:00:00+08:00"),
            event("2026-10-26T03:00:29.999Z"),
            event("invalid timestamp", status=0),
            "prefix spotify_request []",
        ]
    )
    assert result["requests"] == 4
    assert result["timestamped_requests"] == 3
    assert result["peak_rolling_30s_requests"] == 2
    assert {
        "source": "poll",
        "endpoint": "/v1/me/player",
        "method": "GET",
        "status": 0,
        "requests": 1,
    } in result["by_endpoint"]
    empty = summarize([])
    assert empty["peak_rolling_30s_requests"] == 0
    assert empty["peak_rolling_30s_web_api_requests"] == 0
    assert empty["first_request_at"] is empty["last_request_at"] is None
