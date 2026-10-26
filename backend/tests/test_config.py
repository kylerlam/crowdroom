"""Tests for application configuration validation."""

import logging
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from backend.core.config import Settings, validate_spotify_config


class TestValidateSpotifyConfig:
    def test_warns_when_client_id_missing(self, caplog):
        with patch("backend.core.config.settings") as mock_settings:
            mock_settings.SPOTIFY_CLIENT_ID = ""
            mock_settings.SPOTIFY_CLIENT_SECRET = "some-secret"
            with caplog.at_level(logging.WARNING):
                validate_spotify_config()
        assert "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET not set" in caplog.text

    def test_warns_when_client_secret_missing(self, caplog):
        with patch("backend.core.config.settings") as mock_settings:
            mock_settings.SPOTIFY_CLIENT_ID = "some-id"
            mock_settings.SPOTIFY_CLIENT_SECRET = ""
            with caplog.at_level(logging.WARNING):
                validate_spotify_config()
        assert "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET not set" in caplog.text

    def test_warns_when_redirect_uri_missing(self, caplog):
        with patch("backend.core.config.settings") as mock_settings:
            mock_settings.SPOTIFY_CLIENT_ID = "id"
            mock_settings.SPOTIFY_CLIENT_SECRET = "secret"
            mock_settings.SPOTIFY_REDIRECT_URI = ""
            with caplog.at_level(logging.WARNING):
                validate_spotify_config()
        assert "SPOTIFY_REDIRECT_URI not set" in caplog.text

    def test_no_warning_when_fully_configured(self, caplog):
        with patch("backend.core.config.settings") as mock_settings:
            mock_settings.SPOTIFY_CLIENT_ID = "id"
            mock_settings.SPOTIFY_CLIENT_SECRET = "secret"
            mock_settings.SPOTIFY_REDIRECT_URI = "http://localhost:8000/callback"
            with caplog.at_level(logging.WARNING):
                validate_spotify_config()
        assert caplog.text == ""

    def test_skips_redirect_check_when_credentials_missing(self, caplog):
        with patch("backend.core.config.settings") as mock_settings:
            mock_settings.SPOTIFY_CLIENT_ID = ""
            mock_settings.SPOTIFY_CLIENT_SECRET = ""
            mock_settings.SPOTIFY_REDIRECT_URI = ""
            with caplog.at_level(logging.WARNING):
                validate_spotify_config()
        assert "SPOTIFY_REDIRECT_URI not set" not in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("http://192.168.1.20:8000/", "http://192.168.1.20:8000"),
        ("https://music.example.com/", "https://music.example.com"),
        ("http://[fd00::1]:8000", "http://[fd00::1]:8000"),
    ],
)
def test_public_url_normalizes_share_origin_without_changing_oauth(value, expected):
    settings = Settings(
        _env_file=None,
        SECRET_KEY="test",
        ENCRYPTION_KEY="test",
        PUBLIC_URL=value,
        FRONTEND_URL="http://127.0.0.1:8000",
        SPOTIFY_REDIRECT_URI="http://127.0.0.1:8000/api/v1/auth/spotify/callback",
    )
    assert settings.PUBLIC_URL == expected
    assert settings.FRONTEND_URL == "http://127.0.0.1:8000"
    assert (
        settings.SPOTIFY_REDIRECT_URI
        == "http://127.0.0.1:8000/api/v1/auth/spotify/callback"
    )


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "ftp://192.168.1.20",
        "//192.168.1.20",
        "http://user:password@192.168.1.20",
        "http://192.168.1.20/room",
        "http://192.168.1.20?token=secret",
        "http://192.168.1.20#room",
        "http://192.168.1.20:invalid",
    ],
)
def test_public_url_rejects_non_origin_values(value):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, SECRET_KEY="test", ENCRYPTION_KEY="test", PUBLIC_URL=value
        )
