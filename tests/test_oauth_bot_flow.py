"""The Phase 2 journey as the *user* experiences it inside Telegram."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from src.bot import copy
from src.bot.handlers import GateHandlers
from src.bot.invites import InviteService
from src.bot.keyboards import Action
from src.config import VerificationMode
from src.verification.youtube_oauth import (
    GOOGLE_REVOKE_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    YOUTUBE_SUBSCRIPTIONS_ENDPOINT,
    YouTubeOAuthVerifier,
)
from tests.conftest import Conversation, make_settings

CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"


@pytest.fixture
def oauth_settings(tmp_path):
    return make_settings(
        tmp_path,
        verification_mode=VerificationMode.YOUTUBE_OAUTH,
        google_client_id="cid",
        google_client_secret="csecret",
        oauth_public_base_url="https://gate.example.com",
        oauth_state_secret="o" * 40,
        youtube_channel_id=CHANNEL_ID,
        youtube_channel_url="https://www.youtube.com/@testchannel",
    )


@pytest.fixture
async def oauth_gate(oauth_settings, db, recorder, fake_bot):
    verifier = YouTubeOAuthVerifier(oauth_settings, db)
    await verifier.start()
    handlers = GateHandlers(
        oauth_settings, db, verifier, InviteService(oauth_settings, db)
    )
    try:
        yield Conversation(handlers, fake_bot, recorder), verifier
    finally:
        await verifier.stop()


def _google_ok(subscribed: bool = True):
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.x"})
    )
    respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"items": [{"snippet": {}}] if subscribed else []}
        )
    )
    respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))


async def test_oauth_mode_shows_google_button_not_honor_disclosure(oauth_gate):
    gate, _ = oauth_gate

    welcome = await gate.send_command("start")
    assert copy.BTN_SUBSCRIBE in welcome.button_texts

    step = await gate.tap(Action.SUBSCRIBE_CLICKED.value)

    # No honour-system wording anywhere in real-verification mode.
    assert "honour system" not in step.text
    assert copy.BTN_VERIFY_GOOGLE in step.button_texts
    google_url = next(u for u in step.button_urls if "accounts.google.com" in u)
    assert "youtube.readonly" in google_url


async def test_oauth_intro_explains_what_is_accessed(oauth_gate):
    gate, _ = oauth_gate
    await gate.send_command("start")
    step = await gate.tap(Action.SUBSCRIBE_CLICKED.value)

    assert "read-only" in step.text
    assert "password" in step.text
    assert "revoked" in step.text


async def test_no_link_before_google_confirms(oauth_gate):
    gate, _ = oauth_gate
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.RECHECK.value)
    await gate.tap(Action.CONFIRM.value)

    assert not any("TestInviteLink" in text for text in gate.recorder.texts)


@respx.mock
async def test_full_oauth_journey(oauth_gate, db):
    gate, verifier = oauth_gate

    # 1. User starts and is sent to Google.
    await gate.send_command("start")
    step = await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    google_url = next(u for u in step.button_urls if "accounts.google.com" in u)
    state = parse_qs(urlparse(google_url).query)["state"][0]

    # 2. They consent; Google redirects to our callback.
    _google_ok(subscribed=True)
    outcome = await verifier.handle_callback(code="the-code", state=state)
    assert outcome.result.was_technically_verified is True

    # 3. The bot pushes the good news + the group link into the chat.
    gate.recorder.clear()
    await gate.handlers.notify_verified(gate.bot, gate.user.id)

    assert any("Verified" in text for text in gate.recorder.texts)
    assert any(
        "TestInviteLink" in url
        for message in gate.recorder.messages
        for url in message.button_urls
    )

    record = await db.get_user(gate.user.id)
    assert record.verification_method == "youtube_oauth"


@respx.mock
async def test_recheck_after_subscribing_succeeds(oauth_gate, db):
    gate, verifier = oauth_gate
    await gate.send_command("start")
    step = await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    state = parse_qs(
        urlparse(next(u for u in step.button_urls if "google" in u)).query
    )["state"][0]

    # First attempt: not subscribed yet.
    _google_ok(subscribed=False)
    first = await verifier.handle_callback(code="c1", state=state)
    assert first.result.outcome.value == "not_subscribed"

    # They subscribe, then tap "Check again" -> a brand new consent link.
    retry = await gate.tap(Action.RECHECK.value)
    new_state = parse_qs(
        urlparse(next(u for u in retry.button_urls if "google" in u)).query
    )["state"][0]
    assert new_state != state

    _google_ok(subscribed=True)
    second = await verifier.handle_callback(code="c2", state=new_state)
    assert second.result.outcome.value == "verified"
    assert (await db.get_user(gate.user.id)).is_verified


async def test_not_subscribed_notification_offers_retry(oauth_gate):
    gate, _ = oauth_gate
    from src.verification import VerificationOutcome

    await gate.handlers.notify_failed(
        gate.bot, gate.user.id, VerificationOutcome.NOT_SUBSCRIBED
    )
    message = gate.recorder.last
    assert "isn't subscribed" in message.text
    assert copy.BTN_RECHECK in message.button_texts


async def test_error_notification_offers_retry(oauth_gate):
    gate, _ = oauth_gate
    from src.verification import VerificationOutcome

    await gate.handlers.notify_failed(
        gate.bot, gate.user.id, VerificationOutcome.ERROR
    )
    assert "didn't complete" in gate.recorder.last.text


async def test_privacy_text_mentions_oauth_handling(oauth_gate):
    gate, _ = oauth_gate
    message = await gate.send_command("privacy")
    assert "revoked immediately" in message.text
    assert "never store Google tokens" in message.text


@respx.mock
async def test_verified_user_returning_gets_link_without_google_again(oauth_gate, db):
    gate, verifier = oauth_gate
    await db.touch_user(gate.user.id)
    await db.mark_confirmed(gate.user.id, "youtube_oauth")

    message = await gate.send_command("start")
    assert "Welcome back" in message.text
    assert "TestInviteLink" in message.button_urls[0]


async def test_status_shows_real_verification_wording(oauth_gate, db):
    gate, _ = oauth_gate
    await db.touch_user(gate.user.id)
    await db.mark_confirmed(gate.user.id, "youtube_oauth")

    message = await gate.send_command("status")
    assert "Subscription verified with YouTube" in message.text
    assert "honour" not in message.text
