"""The aiohttp server: health checks and the OAuth callback page."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from aiohttp.test_utils import TestClient, TestServer

from src.bot.handlers import GateHandlers
from src.bot.invites import InviteService
from src.config import VerificationMode
from src.verification.youtube_oauth import (
    GOOGLE_REVOKE_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    YOUTUBE_SUBSCRIPTIONS_ENDPOINT,
    YouTubeOAuthVerifier,
    sign_state,
)
from src.web import build_app
from tests.conftest import make_settings

CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
STATE_SECRET = "w" * 40


@pytest.fixture
def oauth_settings(tmp_path):
    return make_settings(
        tmp_path,
        verification_mode=VerificationMode.YOUTUBE_OAUTH,
        google_client_id="cid",
        google_client_secret="csecret",
        oauth_public_base_url="https://gate.example.com",
        oauth_state_secret=STATE_SECRET,
        youtube_channel_id=CHANNEL_ID,
    )


# ---------------------------------------------------------------------------
# health endpoints
# ---------------------------------------------------------------------------


async def test_health_endpoints_in_honor_mode(settings, aiohttp_client):
    client = await aiohttp_client(build_app(settings))
    for path in ("/", "/health", "/healthz"):
        response = await client.get(path)
        assert response.status == 200
        body = await response.json()
        assert body["status"] == "ok"
        assert body["verification_mode"] == "honor"


async def test_oauth_route_absent_in_honor_mode(settings, aiohttp_client):
    """Phase 1 must not expose an OAuth surface at all."""
    client = await aiohttp_client(build_app(settings))
    response = await client.get("/oauth/callback")
    assert response.status == 404


async def test_health_reports_no_secrets(oauth_settings, aiohttp_client):
    client = await aiohttp_client(build_app(oauth_settings))
    text = await (await client.get("/health")).text()
    assert "csecret" not in text
    assert oauth_settings.bot_token not in text


# ---------------------------------------------------------------------------
# OAuth callback page
# ---------------------------------------------------------------------------


@pytest.fixture
async def oauth_client(oauth_settings, db, recorder, fake_bot, aiohttp_client):
    verifier = YouTubeOAuthVerifier(oauth_settings, db)
    await verifier.start()
    handlers = GateHandlers(
        oauth_settings, db, verifier, InviteService(oauth_settings, db)
    )
    app = build_app(oauth_settings, handlers, fake_bot)
    app["verifier"] = verifier
    app["bot"] = fake_bot
    app["bot_username"] = "TestGateBot"
    client = await aiohttp_client(app)
    try:
        yield client, verifier, db, recorder
    finally:
        await verifier.stop()


async def _issue_state(verifier, db, user_id: int = 42) -> str:
    await db.touch_user(user_id)
    url = await verifier.build_authorization_url(user_id)
    return parse_qs(urlparse(url).query)["state"][0]


@respx.mock
async def test_callback_success_page_and_telegram_push(oauth_client):
    client, verifier, db, recorder = oauth_client
    state = await _issue_state(verifier, db)

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.x"})
    )
    respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"items": [{"snippet": {}}]})
    )
    respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))

    response = await client.get("/oauth/callback", params={"code": "c", "state": state})
    body = await response.text()

    assert response.status == 200
    assert "Verified" in body
    assert "https://t.me/TestGateBot" in body

    # The user was messaged in Telegram with the group link, unprompted.
    assert any("Verified" in text for text in recorder.texts)
    assert any(
        "TestInviteLink" in (message.button_urls or [""])[0]
        for message in recorder.messages
        if message.button_urls
    )
    assert (await db.get_user(42)).is_verified


@respx.mock
async def test_callback_not_subscribed_page(oauth_client):
    client, verifier, db, recorder = oauth_client
    state = await _issue_state(verifier, db)

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.x"})
    )
    respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))

    response = await client.get("/oauth/callback", params={"code": "c", "state": state})
    body = await response.text()

    assert response.status == 200
    assert "Not subscribed" in body
    assert not (await db.get_user(42)).is_verified
    # No group link was pushed to Telegram.
    assert not any("TestInviteLink" in " ".join(m.button_urls) for m in recorder.messages)


async def test_callback_with_no_params_is_a_400(oauth_client):
    client, *_ = oauth_client
    response = await client.get("/oauth/callback")
    assert response.status == 400
    assert "didn't complete" in await response.text()


async def test_callback_with_forged_state_is_a_400(oauth_client):
    client, verifier, db, _ = oauth_client
    forged = sign_state("attacker", "the-wrong-secret")
    response = await client.get("/oauth/callback", params={"code": "c", "state": forged})
    assert response.status == 400


async def test_callback_with_google_error_is_handled(oauth_client):
    client, verifier, db, _ = oauth_client
    state = await _issue_state(verifier, db)
    response = await client.get(
        "/oauth/callback", params={"error": "access_denied", "state": state}
    )
    assert response.status == 400
    assert not (await db.get_user(42)).is_verified


@respx.mock
async def test_callback_never_leaks_secrets_into_the_page(oauth_client):
    client, verifier, db, _ = oauth_client
    state = await _issue_state(verifier, db)

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.SUPERSECRET"})
    )
    respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"items": [{}]})
    )
    respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))

    body = await (
        await client.get("/oauth/callback", params={"code": "c", "state": state})
    ).text()

    assert "ya29.SUPERSECRET" not in body
    assert "csecret" not in body
    assert state not in body


async def test_callback_page_has_no_reflected_input(oauth_client):
    """A crafted query string must not end up rendered in the HTML."""
    client, *_ = oauth_client
    payload = "<script>alert(1)</script>"
    body = await (
        await client.get("/oauth/callback", params={"code": payload, "state": payload})
    ).text()
    assert "<script>alert(1)</script>" not in body


async def test_health_still_works_alongside_oauth(oauth_client):
    client, *_ = oauth_client
    assert (await client.get("/health")).status == 200


# ---------------------------------------------------------------------------
# WebServer wiring
# ---------------------------------------------------------------------------


async def test_webserver_is_disabled_when_not_needed(settings):
    from src.web import WebServer

    server = WebServer(settings)
    assert server.enabled is False
    await server.start()  # no-op, must not raise
    await server.stop()


async def test_webserver_enabled_for_health_check_port(tmp_path):
    from src.web import WebServer

    settings = make_settings(tmp_path, health_check_port=9099)
    server = WebServer(settings)
    assert server.enabled is True
    assert server.port == 9099


async def test_health_check_port_zero_means_pick_any_free_port(tmp_path):
    """Regression: 0 is falsy, so an `or` fallback would silently use 8080."""
    from src.web import WebServer

    settings = make_settings(tmp_path, health_check_port=0, web_port=8080)
    server = WebServer(settings)
    assert server.enabled is True
    assert server.port == 0  # NOT 8080


async def test_health_check_port_zero_binds_an_ephemeral_port(tmp_path):
    """Actually bind it, and prove we did not land on web_port."""
    from src.web import WebServer

    settings = make_settings(tmp_path, health_check_port=0, web_host="127.0.0.1",
                             web_port=8080)
    server = WebServer(settings)
    await server.start(bot=None, bot_username="TestBot")
    try:
        sockets = server._runner.addresses  # type: ignore[union-attr]
        bound_port = sockets[0][1]
        assert bound_port != 0
        assert bound_port != 8080
    finally:
        await server.stop()


async def test_webserver_enabled_for_oauth(oauth_settings):
    from src.web import WebServer

    server = WebServer(oauth_settings)
    assert server.enabled is True
    assert server.port == oauth_settings.web_port


async def test_webserver_binds_and_serves(tmp_path):
    """Actually open a socket and hit it, to prove startup works."""
    from src.web import WebServer

    settings = make_settings(tmp_path, health_check_port=0, web_host="127.0.0.1")
    server = WebServer(settings)
    app = build_app(settings)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/health")).status == 200
