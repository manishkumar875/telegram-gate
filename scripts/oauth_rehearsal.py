#!/usr/bin/env python3
"""Rehearse the whole Phase 2 (Google OAuth) flow WITHOUT Google.

    python scripts/oauth_rehearsal.py

Setting up Google Cloud takes 30-45 minutes. This script proves the code on
your side already works before you spend that time, and afterwards it tells
you whether your own .env values are wired together correctly.

What it does
------------
* Starts the REAL web server on a real local port.
* Builds a REAL Google consent URL from your settings and checks every
  parameter (scope, redirect URI, PKCE, signed state).
* Stands in for Google: pretends the user consented, then answers the
  YouTube API as if they ARE subscribed, and then as if they are NOT.
* Makes a REAL http request to /oauth/callback, exactly as the user's
  browser would after Google redirects them.
* Shows the page the user sees and the messages pushed back into Telegram.
* Checks the security properties: replayed links, forged state, expired
  links, and a denied consent must all fail closed.

Nothing here contacts Google or Telegram. No real credentials are needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PASS = "  [PASS]"
FAIL = "  [FAIL]"
INFO = "  [INFO]"

DEMO_USER = 424242


class Checks:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, description: str) -> None:
        print(f"{PASS if ok else FAIL} {description}")
        if not ok:
            self.failures += 1


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)))


class RecordingBot:
    """Stands in for telegram.Bot and remembers what the user was sent."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((text, kwargs.get("reply_markup")))

    async def create_chat_invite_link(self, **kwargs):
        class _Link:
            invite_link = "https://t.me/+rehearsalGeneratedLink"

        return _Link()

    async def get_me(self):
        class _Me:
            username = "YourBot"
            id = 1

        return _Me()


async def run() -> int:
    from src.console import force_utf8_console

    force_utf8_console()

    print("=" * 66)
    print("  PHASE 2 REHEARSAL - Google OAuth flow, without Google")
    print("=" * 66)

    try:
        import httpx
        import respx
    except ImportError:
        print(
            f"\n{FAIL} This tool needs the dev dependencies:\n"
            "      pip install -r requirements-dev.txt\n"
        )
        return 1

    from src.bot.handlers import GateHandlers
    from src.bot.invites import InviteService
    from src.config import ConfigError, VerificationMode, load_settings
    from src.database import Database
    from src.verification import VerificationOutcome
    from src.verification.youtube_oauth import (
        GOOGLE_REVOKE_ENDPOINT,
        GOOGLE_TOKEN_ENDPOINT,
        YOUTUBE_SCOPE,
        YOUTUBE_SUBSCRIPTIONS_ENDPOINT,
        YouTubeOAuthVerifier,
        sign_state,
    )
    from src.web import build_app

    checks = Checks()

    # ---- 1. configuration ------------------------------------------------
    section("Your configuration")
    real_config = True
    try:
        settings = load_settings()
        if settings.verification_mode is not VerificationMode.YOUTUBE_OAUTH:
            print(f"{INFO} VERIFICATION_MODE is '{settings.verification_mode.value}', "
                  "not 'youtube_oauth'.")
            print(f"{INFO} Rehearsing with placeholder Google values instead.")
            real_config = False
    except ConfigError as exc:
        print(f"{INFO} Your .env is not ready for Phase 2 yet:")
        for line in str(exc).splitlines():
            if line.strip().startswith(tuple("123456789")):
                print(f"         {line.strip()}")
        print(f"\n{INFO} Rehearsing with placeholder values so you can still")
        print(f"{INFO} see the flow working. Fill in the items above for a real run.")
        real_config = False

    if not real_config:
        # Build placeholder settings inline rather than importing from tests/,
        # so this script still works from a deployment that ships src/ only.
        import tempfile

        from src.config import InviteMode, Settings

        settings = Settings(
            bot_token="123456789:AAFakeTokenUsedOnlyByTheRehearsal_0000",
            api_base_url="https://api.telegram.org/bot",
            group_invite_link="https://t.me/+rehearsalGroupLink",
            group_chat_id=None,
            invite_mode=InviteMode.STATIC,
            admin_user_ids=(),
            youtube_channel_url="https://www.youtube.com/@rehearsal",
            youtube_channel_id="UC_x5XG1OV2P6uZZ5FSM9Ttw",
            youtube_channel_handle="@rehearsal",
            youtube_api_key="",
            verification_mode=VerificationMode.YOUTUBE_OAUTH,
            min_seconds_before_confirm=0,
            google_client_id="REHEARSAL.apps.googleusercontent.com",
            google_client_secret="REHEARSAL-secret",
            oauth_public_base_url="https://rehearsal.example.com",
            oauth_state_secret="R" * 40,
            web_host="127.0.0.1",
            web_port=8080,
            database_path=Path(tempfile.mkdtemp()) / "rehearsal.sqlite3",
            log_level="WARNING",
            health_check_port=None,
            community_name="your community",
        )
    else:
        print(f"{PASS} Using YOUR real .env values")

    print(f"{INFO} Channel to check : {settings.youtube_channel_id}")
    print(f"{INFO} Redirect URI     : {settings.oauth_redirect_uri}")
    print(f"{INFO}   ^ this EXACT string must be in your Google Cloud")
    print(f"{INFO}     'Authorised redirect URIs' list.")

    # ---- 2. wire up the real components ----------------------------------
    import tempfile

    db_path = Path(tempfile.mkdtemp()) / "rehearsal.sqlite3"
    db = Database(db_path)
    await db.connect()
    verifier = YouTubeOAuthVerifier(settings, db)
    await verifier.start()
    bot = RecordingBot()
    handlers = GateHandlers(settings, db, verifier, InviteService(settings, db))
    await db.touch_user(DEMO_USER)

    app = build_app(settings, handlers, bot)
    app["verifier"] = verifier
    app["bot"] = bot
    app["bot_username"] = "YourBot"

    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # ---- 3. the consent URL ------------------------------------------
        section("Step 1: the link the bot gives the user")
        auth_url = await verifier.build_authorization_url(DEMO_USER)
        parsed = urlparse(auth_url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        state = params["state"]

        print(f"{INFO} {auth_url[:96]}...")
        checks.check(parsed.netloc == "accounts.google.com",
                     "points at Google's real consent page")
        checks.check(params["scope"] == YOUTUBE_SCOPE,
                     "asks for ONLY the read-only YouTube scope")
        checks.check(params["redirect_uri"] == settings.oauth_redirect_uri,
                     "redirect URI matches your configuration")
        checks.check(params["code_challenge_method"] == "S256", "uses PKCE (S256)")
        checks.check(params["access_type"] == "online",
                     "never requests offline access (no refresh token)")
        checks.check("client_secret" not in params,
                     "client secret is NOT exposed in the browser URL")

        # ---- 4. happy path -----------------------------------------------
        section("Step 2: user consents AND is subscribed")

        with respx.mock:
            respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"access_token": "ya29.rehearsal"})
            )
            respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"items": [{"snippet": {}}]})
            )
            revoke = respx.post(GOOGLE_REVOKE_ENDPOINT).mock(
                return_value=httpx.Response(200)
            )

            response = await client.get(
                "/oauth/callback", params={"code": "fake-code", "state": state}
            )
            page = await response.text()

            checks.check(response.status == 200, "callback page returns 200 OK")
            checks.check("Verified" in page, "browser shows a success page")
            checks.check("t.me/YourBot" in page, "page offers a link back to Telegram")
            checks.check(revoke.called,
                         "Google access token was REVOKED right after the check")

        record = await db.get_user(DEMO_USER)
        checks.check(bool(record and record.is_verified), "user is marked verified")
        checks.check(record.verification_method == "youtube_oauth",
                     "stored as a genuine YouTube verification")

        print(f"\n  What the user receives in Telegram:")
        for text, markup in bot.messages:
            print(f"    - {text.splitlines()[0]}")
            if markup:
                for row in markup.inline_keyboard:
                    for button in row:
                        print(f"        [{button.text}] -> {button.url}")
        checks.check(
            any(m for _, m in bot.messages if m), "the group link is delivered"
        )

        # ---- 5. not subscribed -------------------------------------------
        section("Step 3: user consents but is NOT subscribed")
        await db.reset_user(DEMO_USER)
        bot.messages.clear()
        state2 = parse_qs(
            urlparse(await verifier.build_authorization_url(DEMO_USER)).query
        )["state"][0]

        with respx.mock:
            respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"access_token": "ya29.x"})
            )
            respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"items": []})
            )
            respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))

            response = await client.get(
                "/oauth/callback", params={"code": "c", "state": state2}
            )
            page = await response.text()

        checks.check("Not subscribed" in page, "browser shows 'not subscribed'")
        record = await db.get_user(DEMO_USER)
        checks.check(not record.is_verified, "user is NOT marked verified")
        checks.check(
            not any("t.me/+" in str(m) for _, m in bot.messages),
            "the group link is NOT handed out",
        )

        # ---- 6. security --------------------------------------------------
        section("Step 4: attacks must fail closed")

        replay = await client.get(
            "/oauth/callback", params={"code": "c", "state": state2}
        )
        checks.check(replay.status == 400, "replaying a used link is rejected")

        forged = sign_state("attacker", "a-secret-they-do-not-have")
        with respx.mock:
            token_route = respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"access_token": "x"})
            )
            forged_response = await client.get(
                "/oauth/callback", params={"code": "c", "state": forged}
            )
            checks.check(forged_response.status == 400, "forged state is rejected")
            checks.check(
                not token_route.called,
                "forged state never reaches Google (rejected before any call)",
            )

        denied_state = parse_qs(
            urlparse(await verifier.build_authorization_url(DEMO_USER)).query
        )["state"][0]
        denied = await client.get(
            "/oauth/callback", params={"error": "access_denied", "state": denied_state}
        )
        checks.check(denied.status == 400, "user refusing consent fails closed")
        checks.check(
            not (await db.get_user(DEMO_USER)).is_verified,
            "refusing consent does not grant access",
        )

        await db.create_oauth_nonce("expired", DEMO_USER, "v", ttl_seconds=-1)
        expired = await client.get(
            "/oauth/callback",
            params={"code": "c", "state": sign_state("expired", settings.oauth_state_secret)},
        )
        checks.check(expired.status == 400, "expired link is rejected")

        with respx.mock:
            respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"access_token": "x"})
            )
            respx.get(YOUTUBE_SUBSCRIPTIONS_ENDPOINT).mock(
                return_value=httpx.Response(403, json={"error": {"message": "quotaExceeded"}})
            )
            respx.post(GOOGLE_REVOKE_ENDPOINT).mock(return_value=httpx.Response(200))
            quota_state = parse_qs(
                urlparse(await verifier.build_authorization_url(DEMO_USER)).query
            )["state"][0]
            quota = await client.get(
                "/oauth/callback", params={"code": "c", "state": quota_state}
            )
        checks.check(quota.status == 400, "a YouTube API failure fails closed")
        checks.check(
            not (await db.get_user(DEMO_USER)).is_verified,
            "an API error never grants access by mistake",
        )

    finally:
        await client.close()
        await verifier.stop()
        await db.close()

    # ---- summary ----------------------------------------------------------
    print("\n" + "=" * 66)
    if checks.failures:
        print(f"  RESULT: {checks.failures} check(s) FAILED")
        print("=" * 66)
        return 1
    print("  RESULT: the Phase 2 OAuth flow works correctly.")
    print("=" * 66)
    if not real_config:
        print("\n  That run used placeholder Google values.")
        print("  Fill in the .env items listed above, then run this again to")
        print("  validate YOUR configuration.")
    else:
        print("\n  Your .env is wired correctly. Remember the redirect URI above")
        print("  must be registered in Google Cloud, character for character.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
