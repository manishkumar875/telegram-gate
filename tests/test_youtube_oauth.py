"""Phase 2: Google OAuth + YouTube Data API verification.

Every Google/YouTube HTTP call is intercepted with respx, so these tests
exercise the real request-building and response-handling code without ever
touching the network or needing credentials.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from src.config import VerificationMode
from src.database import Database
from src.verification import VerificationOutcome
from src.verification.youtube_oauth import (
    GOOGLE_REVOKE_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    YOUTUBE_CHANNELS_ENDPOINT,
    YOUTUBE_SCOPE,
    YOUTUBE_SUBSCRIPTIONS_ENDPOINT,
    YouTubeOAuthVerifier,
    make_pkce_pair,
    resolve_channel_id,
    sign_state,
    verify_state,
)
from tests.conftest import make_settings

CHANNEL_ID = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
STATE_SECRET = "s" * 40


@pytest.fixture
def oauth_settings(tmp_path):
    return make_settings(
        tmp_path,
        verification_mode=VerificationMode.YOUTUBE_OAUTH,
        google_client_id="client-id.apps.googleusercontent.com",
        google_client_secret="client-secret-value",
        oauth_public_base_url="https://gate.example.com",
        oauth_state_secret=STATE_SECRET,
        youtube_channel_id=CHANNEL_ID,
        youtube_channel_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
    )


@pytest.fixture
async def verifier(oauth_settings, db: Database):
    instance = YouTubeOAuthVerifier(oauth_settings, db)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


# ---------------------------------------------------------------------------
# PKCE + state signing
# ---------------------------------------------------------------------------


def test_pkce_pair_matches_s256_spec():
    verifier_value, challenge = make_pkce_pair()
    assert 43 <= len(verifier_value) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier_value.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge  # unpadded, as the RFC requires


def test_pkce_pairs_are_unique():
    assert make_pkce_pair()[0] != make_pkce_pair()[0]


def test_state_round_trip():
    state = sign_state("nonce123", STATE_SECRET)
    assert verify_state(state, STATE_SECRET) == "nonce123"


def test_tampered_state_is_rejected():
    state = sign_state("nonce123", STATE_SECRET)
    nonce, _, signature = state.partition(".")
    assert verify_state(f"evil.{signature}", STATE_SECRET) is None
    assert verify_state(f"{nonce}.deadbeef", STATE_SECRET) is None


def test_state_signed_with_another_secret_is_rejected():
    state = sign_state("nonce123", "a-different-secret-entirely")
    assert verify_state(state, STATE_SECRET) is None


@pytest.mark.parametrize("bad", ["", "no-dot", ".", "x.", ".y", "..."])
def test_malformed_state_is_rejected(bad):
    assert verify_state(bad, STATE_SECRET) is None


# ---------------------------------------------------------------------------
# building the consent URL
# ---------------------------------------------------------------------------


async def test_authorization_url_is_correct(verifier, db: Database):
    await db.touch_user(42)
    url = await verifier.build_authorization_url(42)
    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert parsed.netloc == "accounts.google.com"
    assert params["client_id"] == "client-id.apps.googleusercontent.com"
    assert params["redirect_uri"] == "https://gate.example.com/oauth/callback"
    assert params["response_type"] == "code"
    assert params["scope"] == YOUTUBE_SCOPE
    assert params["code_challenge_method"] == "S256"
    # No refresh token is ever requested.
    assert params["access_type"] == "online"
    assert "client_secret" not in params  # never in a browser-visible URL

    nonce = verify_state(params["state"], STATE_SECRET)
    assert nonce is not None


async def test_authorization_url_requests_only_the_readonly_scope(verifier, db):
    await db.touch_user(42)
    url = await verifier.build_authorization_url(42)
    assert "youtube.readonly" in url
    for dangerous in ["youtube.force-ssl", "youtube.upload", "youtubepartner"]:
        assert dangerous not in url


async def test_each_user_gets_a_distinct_single_use_url(verifier, db):
    await db.touch_user(1)
    await db.touch_user(2)
    assert await verifier.build_authorization_url(1) != await verifier.build_authorization_url(2)


async def test_verify_returns_a_consent_url_for_new_users(verifier, db):
    await db.touch_user(7)
    result = await verifier.verify(7)
    assert result.outcome is VerificationOutcome.NEEDS_USER_ACTION
    assert result.was_technically_verified is False
    assert result.action_url.startswith("https://accounts.google.com/")


async def test_verify_short_circuits_for_already_verified_users(verifier, db):
    await db.touch_user(8)
    await db.mark_confirmed(8, "youtube_oauth")
    result = await verifier.verify(8)
    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.was_technically_verified is True


# ---------------------------------------------------------------------------
# the callback: the part that really talks to Google
# ---------------------------------------------------------------------------


def mock_token(status: int = 200, body: dict | None = None):
    return respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(status, json=body or {"access_token": "ya29.test"})
    )


def mock_subscriptions(subscribed: bool, status: int = 200):
    items = [{"snippet": {"title": "Test Channel"}}] if subscribed else []
    return respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
        return_value=httpx.Response(status, json={"items": items})
    )


def mock_revoke():
    return respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))


async def start_flow(verifier, db, user_id: int = 42) -> str:
    await db.touch_user(user_id)
    url = await verifier.build_authorization_url(user_id)
    params = parse_qs(urlparse(url).query)
    return params["state"][0]


@respx.mock
async def test_successful_verification(verifier, db):
    state = await start_flow(verifier, db)
    token_route = mock_token()
    subs_route = mock_subscriptions(True)
    revoke_route = mock_revoke()

    outcome = await verifier.handle_callback(code="auth-code", state=state)

    assert outcome.telegram_user_id == 42
    assert outcome.result.outcome is VerificationOutcome.VERIFIED
    assert outcome.result.was_technically_verified is True

    # Persisted as a genuine verification.
    record = await db.get_user(42)
    assert record.is_verified
    assert record.verification_method == "youtube_oauth"

    assert token_route.called and subs_route.called
    # The token was handed straight back to Google.
    assert revoke_route.called


@respx.mock
async def test_not_subscribed_is_reported_honestly(verifier, db):
    state = await start_flow(verifier, db)
    mock_token()
    mock_subscriptions(False)
    mock_revoke()

    outcome = await verifier.handle_callback(code="auth-code", state=state)

    assert outcome.result.outcome is VerificationOutcome.NOT_SUBSCRIBED
    assert outcome.result.was_technically_verified is True  # we really did check
    assert not (await db.get_user(42)).is_verified


@respx.mock
async def test_correct_channel_and_scope_are_queried(verifier, db):
    state = await start_flow(verifier, db)
    mock_token()
    subs_route = mock_subscriptions(True)
    mock_revoke()

    await verifier.handle_callback(code="auth-code", state=state)

    request = subs_route.calls[0].request
    params = parse_qs(urlparse(str(request.url)).query)
    assert params["forChannelId"] == [CHANNEL_ID]
    assert params["mine"] == ["true"]
    assert request.headers["Authorization"] == "Bearer ya29.test"


@respx.mock
async def test_pkce_verifier_is_sent_in_the_exchange(verifier, db):
    state = await start_flow(verifier, db)
    token_route = mock_token()
    mock_subscriptions(True)
    mock_revoke()

    await verifier.handle_callback(code="auth-code", state=state)

    body = parse_qs(token_route.calls[0].request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["auth-code"]
    assert len(body["code_verifier"][0]) >= 43
    assert body["redirect_uri"] == ["https://gate.example.com/oauth/callback"]


# ---------------------------------------------------------------------------
# callback failure modes
# ---------------------------------------------------------------------------


@respx.mock
async def test_replayed_callback_is_rejected(verifier, db):
    state = await start_flow(verifier, db)
    mock_token()
    mock_subscriptions(True)
    mock_revoke()

    first = await verifier.handle_callback(code="auth-code", state=state)
    assert first.result.outcome is VerificationOutcome.VERIFIED

    second = await verifier.handle_callback(code="auth-code", state=state)
    assert second.result.outcome is VerificationOutcome.ERROR
    assert "expired or already-used" in second.result.detail


@respx.mock
async def test_forged_state_never_reaches_google(verifier, db):
    token_route = mock_token()
    forged = sign_state("attacker-nonce", "wrong-secret")

    outcome = await verifier.handle_callback(code="auth-code", state=forged)

    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert outcome.telegram_user_id is None
    assert not token_route.called  # short-circuited before any network call


@respx.mock
async def test_valid_signature_but_unknown_nonce_is_rejected(verifier, db):
    token_route = mock_token()
    state = sign_state("never-issued", STATE_SECRET)

    outcome = await verifier.handle_callback(code="auth-code", state=state)

    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert not token_route.called


@respx.mock
async def test_expired_nonce_is_rejected(verifier, db):
    await db.touch_user(42)
    await db.create_oauth_nonce("expired-nonce", 42, "verifier", ttl_seconds=-1)
    token_route = mock_token()

    outcome = await verifier.handle_callback(
        code="auth-code", state=sign_state("expired-nonce", STATE_SECRET)
    )

    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert not token_route.called


@respx.mock
async def test_user_denying_consent_is_handled(verifier, db):
    state = await start_flow(verifier, db)
    outcome = await verifier.handle_callback(code=None, state=state, error="access_denied")

    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert outcome.telegram_user_id == 42  # so we can still message them
    assert not (await db.get_user(42)).is_verified


@respx.mock
async def test_token_exchange_failure_is_handled(verifier, db):
    state = await start_flow(verifier, db)
    mock_token(status=400, body={"error": "invalid_grant"})

    outcome = await verifier.handle_callback(code="bad-code", state=state)

    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert not (await db.get_user(42)).is_verified


@respx.mock
async def test_missing_access_token_is_handled(verifier, db):
    state = await start_flow(verifier, db)
    mock_token(body={"token_type": "Bearer"})  # no access_token

    outcome = await verifier.handle_callback(code="c", state=state)
    assert outcome.result.outcome is VerificationOutcome.ERROR


@respx.mock
async def test_youtube_quota_error_does_not_grant_access(verifier, db):
    state = await start_flow(verifier, db)
    mock_token()
    respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
        return_value=httpx.Response(
            403, json={"error": {"message": "quotaExceeded"}}
        )
    )
    revoke = mock_revoke()

    outcome = await verifier.handle_callback(code="c", state=state)

    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert not (await db.get_user(42)).is_verified
    assert revoke.called  # token still returned


@respx.mock
async def test_network_error_does_not_grant_access(verifier, db):
    state = await start_flow(verifier, db)
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))

    outcome = await verifier.handle_callback(code="c", state=state)
    assert outcome.result.outcome is VerificationOutcome.ERROR


@respx.mock
async def test_revoke_failure_does_not_break_verification(verifier, db):
    state = await start_flow(verifier, db)
    mock_token()
    mock_subscriptions(True)
    respx.post(GOOGLE_REVOKE_ENDPOINT).mock(side_effect=httpx.ConnectError("nope"))

    outcome = await verifier.handle_callback(code="c", state=state)
    assert outcome.result.outcome is VerificationOutcome.VERIFIED


@respx.mock
async def test_missing_code_is_handled(verifier, db):
    state = await start_flow(verifier, db)
    outcome = await verifier.handle_callback(code=None, state=state)
    assert outcome.result.outcome is VerificationOutcome.ERROR
    assert outcome.telegram_user_id == 42


async def test_no_google_tokens_are_ever_persisted(verifier, db):
    """Schema-level guarantee: nowhere to put a Google token even by accident."""
    async with db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cursor:
        tables = {row["name"] for row in await cursor.fetchall()}

    for table in tables:
        async with db.conn.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row["name"].lower() for row in await cursor.fetchall()}
        for forbidden in ("access_token", "refresh_token", "google_id", "email", "password"):
            assert forbidden not in columns, f"{table}.{forbidden} must not exist"


# ---------------------------------------------------------------------------
# channel-id resolution helper
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_channel_id_from_handle():
    respx.get(YOUTUBE_CHANNELS_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"items": [{"id": CHANNEL_ID}]})
    )
    assert await resolve_channel_id(api_key="key", handle="@test") == CHANNEL_ID


@respx.mock
async def test_resolve_channel_id_handles_no_result():
    respx.get(YOUTUBE_CHANNELS_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    assert await resolve_channel_id(api_key="key", handle="@nope") is None


@respx.mock
async def test_resolve_channel_id_handles_api_error():
    respx.get(YOUTUBE_CHANNELS_ENDPOINT).mock(
        return_value=httpx.Response(403, json={"error": {"message": "bad key"}})
    )
    assert await resolve_channel_id(api_key="key", handle="@x") is None


async def test_resolve_channel_id_needs_a_key():
    assert await resolve_channel_id(api_key="", handle="@x") is None
    assert await resolve_channel_id(api_key="k") is None


# ---------------------------------------------------------------------------
# resolving the channel id from the public page (no API key needed)
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_channel_id_from_page_via_external_id():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    html = f'{{"key":"v","externalId":"{CHANNEL_ID}","other":1}}'
    respx.get("https://www.youtube.com/@someone").mock(
        return_value=httpx.Response(200, text=html)
    )
    result = await resolve_channel_id_from_page("https://www.youtube.com/@someone")
    assert result == CHANNEL_ID


@respx.mock
async def test_resolve_channel_id_from_page_via_canonical_link():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    html = f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">'
    respx.get("https://www.youtube.com/@someone").mock(
        return_value=httpx.Response(200, text=html)
    )
    assert await resolve_channel_id_from_page(
        "https://www.youtube.com/@someone"
    ) == CHANNEL_ID


@respx.mock
async def test_resolve_channel_id_from_page_handles_missing_id():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    respx.get("https://www.youtube.com/@nobody").mock(
        return_value=httpx.Response(200, text="<html>nothing useful here</html>")
    )
    assert await resolve_channel_id_from_page("https://www.youtube.com/@nobody") is None


@respx.mock
async def test_resolve_channel_id_from_page_handles_http_error():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    respx.get("https://www.youtube.com/@gone").mock(
        return_value=httpx.Response(404, text="not found")
    )
    assert await resolve_channel_id_from_page("https://www.youtube.com/@gone") is None


@respx.mock
async def test_resolve_channel_id_from_page_handles_network_error():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    respx.get("https://www.youtube.com/@down").mock(
        side_effect=httpx.ConnectError("boom")
    )
    assert await resolve_channel_id_from_page("https://www.youtube.com/@down") is None


async def test_resolve_channel_id_from_page_rejects_empty_url():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    assert await resolve_channel_id_from_page("") is None


@respx.mock
async def test_resolve_channel_id_from_page_ignores_a_malformed_id():
    from src.verification.youtube_oauth import resolve_channel_id_from_page

    respx.get("https://www.youtube.com/@bad").mock(
        return_value=httpx.Response(200, text='"externalId":"NOTACHANNELID"')
    )
    assert await resolve_channel_id_from_page("https://www.youtube.com/@bad") is None
