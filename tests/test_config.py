"""Configuration loading and validation."""

from __future__ import annotations

import pytest

from src.config import ConfigError, InviteMode, VerificationMode, load_settings
from src.config.settings import (
    build_subscribe_url,
    extract_channel_handle,
    extract_channel_id,
)
from tests.conftest import VALID_TOKEN

MINIMAL_ENV = {
    "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
    "TELEGRAM_GROUP_INVITE_LINK": "https://t.me/+AbCdEfGh123456",
    "YOUTUBE_CHANNEL_URL": "https://www.youtube.com/@testchannel",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate every test from the developer's real .env and environment."""
    for key in [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_GROUP_INVITE_LINK",
        "TELEGRAM_GROUP_ID",
        "YOUTUBE_CHANNEL_URL",
        "YOUTUBE_CHANNEL_ID",
        "YOUTUBE_API_KEY",
        "VERIFICATION_MODE",
        "INVITE_MODE",
        "ADMIN_USER_IDS",
        "MIN_SECONDS_BEFORE_CONFIRM",
        "DATABASE_PATH",
        "LOG_LEVEL",
        "HEALTH_CHECK_PORT",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "OAUTH_PUBLIC_BASE_URL",
        "OAUTH_STATE_SECRET",
        "COMMUNITY_NAME",
        "WEB_HOST",
        "WEB_PORT",
        "PORT",
        "TELEGRAM_API_BASE_URL",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cfg.sqlite3"))


def load(monkeypatch, **env):
    merged = {**MINIMAL_ENV, **env}
    for key, value in merged.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # Pass a non-existent env file so only the real environment is read.
    return load_settings(env_file="does-not-exist.env")


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_minimal_valid_config(monkeypatch):
    settings = load(monkeypatch)
    assert settings.bot_token == VALID_TOKEN
    assert settings.verification_mode is VerificationMode.HONOR
    assert settings.invite_mode is InviteMode.STATIC
    assert settings.youtube_channel_handle == "@testchannel"


def test_secrets_are_masked_in_redacted_output(monkeypatch):
    settings = load(
        monkeypatch,
        **{**OAUTH_ENV, "YOUTUBE_API_KEY": "AIzaSy-super-secret-api-key"},
    )
    dumped = str(settings.redacted())

    # No secret appears in full anywhere in the diagnostic output.
    for secret in (
        VALID_TOKEN,
        "super-secret-value",
        "AIzaSy-super-secret-api-key",
        "a" * 32,
    ):
        assert secret not in dumped

    assert "..." in dumped  # ASCII only, so Windows consoles render it
    assert dumped.isascii()


def test_quotes_and_whitespace_are_stripped(monkeypatch):
    settings = load(monkeypatch, TELEGRAM_BOT_TOKEN=f'  "{VALID_TOKEN}"  ')
    assert settings.bot_token == VALID_TOKEN


def test_admin_ids_parsed(monkeypatch):
    settings = load(monkeypatch, ADMIN_USER_IDS="111, 222 ,333")
    assert settings.admin_user_ids == (111, 222, 333)
    assert settings.is_admin(222) is True
    assert settings.is_admin(444) is False


# ---------------------------------------------------------------------------
# validation failures
# ---------------------------------------------------------------------------


def test_missing_token_is_rejected_with_botfather_hint(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, TELEGRAM_BOT_TOKEN=None)
    assert "BotFather" in str(exc.value)


def test_malformed_token_is_rejected(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, TELEGRAM_BOT_TOKEN="not-a-real-token")
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_missing_invite_link_in_static_mode_is_rejected(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, TELEGRAM_GROUP_INVITE_LINK=None)
    assert "TELEGRAM_GROUP_INVITE_LINK" in str(exc.value)


def test_non_telegram_invite_link_is_rejected(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, TELEGRAM_GROUP_INVITE_LINK="https://example.com/join")
    assert "t.me" in str(exc.value)


def test_request_mode_requires_group_id(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, INVITE_MODE="request")
    assert "TELEGRAM_GROUP_ID" in str(exc.value)


def test_request_mode_with_group_id_is_accepted(monkeypatch):
    settings = load(monkeypatch, INVITE_MODE="request", TELEGRAM_GROUP_ID="-1001234567890")
    assert settings.invite_mode is InviteMode.REQUEST
    assert settings.group_chat_id == -1001234567890
    assert settings.needs_group_admin is True


def test_bad_invite_mode_is_rejected(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, INVITE_MODE="teleport")
    assert "static, unique, request" in str(exc.value)


def test_missing_youtube_url_is_rejected(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, YOUTUBE_CHANNEL_URL=None)
    assert "YOUTUBE_CHANNEL_URL" in str(exc.value)


def test_all_errors_are_reported_at_once(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, TELEGRAM_BOT_TOKEN=None, YOUTUBE_CHANNEL_URL=None)
    message = str(exc.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "YOUTUBE_CHANNEL_URL" in message
    assert "2 problem(s)" in message


def test_bad_admin_ids_are_rejected(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, ADMIN_USER_IDS="alice,bob")
    assert "ADMIN_USER_IDS" in str(exc.value)


# ---------------------------------------------------------------------------
# Phase 2 config
# ---------------------------------------------------------------------------


OAUTH_ENV = {
    "VERIFICATION_MODE": "youtube_oauth",
    "GOOGLE_CLIENT_ID": "abc.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "super-secret-value",
    "OAUTH_PUBLIC_BASE_URL": "https://gate.example.com",
    "OAUTH_STATE_SECRET": "a" * 32,
    "YOUTUBE_CHANNEL_ID": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
}


def test_oauth_mode_valid(monkeypatch):
    settings = load(monkeypatch, **OAUTH_ENV)
    assert settings.oauth_enabled is True
    assert settings.oauth_redirect_uri == "https://gate.example.com/oauth/callback"


def test_oauth_mode_requires_client_id(monkeypatch):
    env = {**OAUTH_ENV, "GOOGLE_CLIENT_ID": None}
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, **env)
    assert "GOOGLE_CLIENT_ID" in str(exc.value)


def test_oauth_mode_requires_channel_id(monkeypatch):
    env = {**OAUTH_ENV, "YOUTUBE_CHANNEL_ID": None}
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, **env)
    assert "YOUTUBE_CHANNEL_ID" in str(exc.value)


def test_oauth_mode_rejects_weak_state_secret(monkeypatch):
    env = {**OAUTH_ENV, "OAUTH_STATE_SECRET": "short"}
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, **env)
    assert "OAUTH_STATE_SECRET" in str(exc.value)


def test_oauth_mode_requires_https_base_url(monkeypatch):
    env = {**OAUTH_ENV, "OAUTH_PUBLIC_BASE_URL": "not-a-url"}
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, **env)
    assert "OAUTH_PUBLIC_BASE_URL" in str(exc.value)


def test_honor_mode_does_not_require_google_settings(monkeypatch):
    settings = load(monkeypatch, VERIFICATION_MODE="honor")
    assert settings.oauth_enabled is False


# ---------------------------------------------------------------------------
# hosting: the $PORT convention
# ---------------------------------------------------------------------------


def test_platform_port_is_used_for_health_checks(monkeypatch):
    """Free hosts inject $PORT; binding it should need no extra config."""
    settings = load(monkeypatch, PORT="10000")
    assert settings.health_check_port == 10000
    assert settings.web_port == 10000


def test_explicit_settings_beat_platform_port(monkeypatch):
    settings = load(monkeypatch, PORT="10000", WEB_PORT="9000", HEALTH_CHECK_PORT="9001")
    assert settings.web_port == 9000
    assert settings.health_check_port == 9001


def test_no_port_anywhere_means_no_health_server(monkeypatch):
    settings = load(monkeypatch)
    assert settings.health_check_port is None
    assert settings.web_port == 8080


def test_non_numeric_platform_port_is_reported(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        load(monkeypatch, PORT="not-a-port")
    assert "PORT" in str(exc.value)


def test_api_base_url_defaults_to_telegram(monkeypatch):
    settings = load(monkeypatch)
    assert settings.api_base_url == "https://api.telegram.org/bot"


def test_api_base_url_can_be_overridden(monkeypatch):
    settings = load(monkeypatch, TELEGRAM_API_BASE_URL="http://127.0.0.1:9/bot")
    assert settings.api_base_url == "http://127.0.0.1:9/bot"


# ---------------------------------------------------------------------------
# YouTube URL helpers
# ---------------------------------------------------------------------------


def test_extract_channel_id_from_channel_url():
    url = "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert extract_channel_id(url) == "UC_x5XG1OV2P6uZZ5FSM9Ttw"


def test_extract_channel_id_returns_none_for_handle():
    assert extract_channel_id("https://www.youtube.com/@somebody") is None


def test_extract_handle():
    assert extract_channel_handle("https://www.youtube.com/@some.body-1") == "@some.body-1"
    assert extract_channel_handle("https://www.youtube.com/channel/UCabc") is None


def test_subscribe_url_uses_sub_confirmation_with_channel_id():
    url = build_subscribe_url(
        "https://www.youtube.com/@x", "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    )
    assert url == (
        "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw?sub_confirmation=1"
    )


def test_subscribe_url_appends_param_without_channel_id():
    assert build_subscribe_url("https://www.youtube.com/@x", None) == (
        "https://www.youtube.com/@x?sub_confirmation=1"
    )


def test_subscribe_url_is_not_double_appended():
    url = "https://www.youtube.com/@x?sub_confirmation=1"
    assert build_subscribe_url(url, None) == url


def test_channel_id_is_auto_extracted_from_url(monkeypatch):
    settings = load(
        monkeypatch,
        YOUTUBE_CHANNEL_URL="https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw",
    )
    assert settings.youtube_channel_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert "sub_confirmation=1" in settings.subscribe_url
