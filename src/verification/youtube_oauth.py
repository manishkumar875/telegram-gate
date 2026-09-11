"""Phase 2: GENUINE subscription verification via Google OAuth 2.0 + YouTube Data API v3.

What this actually does
-----------------------
1. The bot hands the user a one-time link to Google's official consent screen.
2. The user signs in *with Google, on Google's own domain* and grants the
   read-only scope ``https://www.googleapis.com/auth/youtube.readonly``.
3. Google redirects back to this app with a short-lived authorization code.
4. We exchange that code for an access token (Authorization Code flow + PKCE).
5. We call ``GET /youtube/v3/subscriptions?mine=true&forChannelId=<CHANNEL>``.
   A non-empty ``items`` array means that Google account really is subscribed.
6. We immediately **revoke** the access token and throw it away.

So the answer is genuinely from YouTube, not from the user.

Security / privacy properties
-----------------------------
* The bot NEVER sees the user's Google password — authentication happens
  entirely on accounts.google.com. We only ever receive an opaque code.
* We request the minimum scope, and it is read-only.
* We do not request ``offline`` access, so we never receive a refresh token.
* The access token is revoked and discarded straight after the single check.
  Nothing about the Google account is written to the database — only a
  boolean "passed" against the Telegram user id.
* CSRF/replay protection: the ``state`` parameter is an HMAC-SHA256-signed
  token whose payload is a random nonce. The nonce is stored server-side,
  expires in 15 minutes, and is *deleted on first use*, so a captured
  callback URL cannot be replayed.
* PKCE (RFC 7636, S256) protects the code exchange even if the redirect is
  intercepted.

Honest limitations (documented in the README too)
-------------------------------------------------
* Requires a Google Cloud project and a public HTTPS URL.
* Google restricts ``youtube.readonly`` as a *sensitive* scope. Until your
  OAuth consent screen is verified by Google, the app stays in "Testing" mode
  and only the test users you list (max 100) can complete the flow. Everyone
  else sees a "has not completed verification" warning.
* The YouTube Data API has a default quota of 10,000 units/day.
  ``subscriptions.list`` costs 1 unit, so ~10,000 checks/day.
* A user can subscribe, verify, then unsubscribe. This proves the
  subscription existed *at the moment of the check*, nothing more.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .base import VerificationOutcome, VerificationResult, Verifier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config import Settings
    from src.database import Database

logger = logging.getLogger(__name__)

# Official Google endpoints (documented, stable).
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
YOUTUBE_SUBSCRIPTIONS_ENDPOINT = "https://www.googleapis.com/youtube/v3/subscriptions"
YOUTUBE_CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"

#: Read-only. The narrowest scope that can answer "are you subscribed?".
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

NONCE_TTL_SECONDS = 900  # 15 minutes
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


# ---------------------------------------------------------------------------
# small crypto helpers
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    """URL-safe base64 without padding (what OAuth/PKCE expect)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE method S256."""
    verifier = _b64url(secrets.token_bytes(48))  # 64 chars, within 43..128
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def sign_state(nonce: str, secret: str) -> str:
    """``<nonce>.<hmac>`` - tamper-evident state parameter."""
    signature = hmac.new(
        secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).digest()
    return f"{nonce}.{_b64url(signature)}"


def verify_state(state: str, secret: str) -> str | None:
    """Return the nonce if ``state`` carries a valid signature, else ``None``."""
    if not state or "." not in state:
        return None
    nonce, _, signature = state.partition(".")
    if not nonce or not signature:
        return None
    expected = sign_state(nonce, secret).partition(".")[2]
    # Constant-time compare so we do not leak the signature byte by byte.
    if not hmac.compare_digest(expected, signature):
        return None
    return nonce


@dataclass(frozen=True)
class OAuthCallbackResult:
    """Outcome of processing Google's redirect back to us."""

    telegram_user_id: int | None
    result: VerificationResult


class YouTubeOAuthVerifier(Verifier):
    method_name = "youtube_oauth"
    provides_real_verification = True

    def __init__(self, settings: "Settings", database: "Database") -> None:
        super().__init__(settings, database)
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Lazily create so the verifier also works in one-off scripts.
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        return self._client

    # -- step 1: send the user to Google ---------------------------------

    async def verify(self, telegram_user_id: int) -> VerificationResult:
        """Return VERIFIED if already checked, else a consent URL to visit."""
        record = await self.db.get_user(telegram_user_id)
        if record and record.verification_method == self.method_name and record.is_verified:
            return VerificationResult(
                outcome=VerificationOutcome.VERIFIED,
                was_technically_verified=True,
                method=self.method_name,
                detail="previously verified against the YouTube Data API",
            )

        url = await self.build_authorization_url(telegram_user_id)
        return VerificationResult(
            outcome=VerificationOutcome.NEEDS_USER_ACTION,
            was_technically_verified=False,
            method=self.method_name,
            action_url=url,
            detail="awaiting Google consent",
        )

    async def build_authorization_url(self, telegram_user_id: int) -> str:
        """Create a single-use consent URL bound to this Telegram user."""
        nonce = secrets.token_urlsafe(24)
        code_verifier, code_challenge = make_pkce_pair()
        await self.db.create_oauth_nonce(
            nonce=nonce,
            telegram_user_id=telegram_user_id,
            code_verifier=code_verifier,
            ttl_seconds=NONCE_TTL_SECONDS,
        )
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.oauth_redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_SCOPE,
            "state": sign_state(nonce, self.settings.oauth_state_secret),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # No offline access: we deliberately never get a refresh token.
            "access_type": "online",
            "include_granted_scopes": "false",
            # Always show the picker so multi-account users choose correctly.
            "prompt": "select_account consent",
        }
        return str(httpx.URL(GOOGLE_AUTH_ENDPOINT, params=params))

    # -- step 2: handle Google's redirect --------------------------------

    async def handle_callback(
        self, *, code: str | None, state: str | None, error: str | None = None
    ) -> OAuthCallbackResult:
        """Process ``/oauth/callback``. Never raises; always returns a result."""
        if error:
            logger.info("Google returned an OAuth error: %s", error)
            nonce = verify_state(state or "", self.settings.oauth_state_secret)
            user_id = None
            if nonce:
                consumed = await self.db.consume_oauth_nonce(nonce)
                user_id = consumed[0] if consumed else None
            return OAuthCallbackResult(
                telegram_user_id=user_id,
                result=VerificationResult(
                    outcome=VerificationOutcome.ERROR,
                    method=self.method_name,
                    detail=f"user denied or google error: {error}",
                ),
            )

        nonce = verify_state(state or "", self.settings.oauth_state_secret)
        if nonce is None:
            logger.warning("Rejected OAuth callback with invalid state signature")
            return OAuthCallbackResult(
                telegram_user_id=None,
                result=VerificationResult(
                    outcome=VerificationOutcome.ERROR,
                    method=self.method_name,
                    detail="invalid or forged state parameter",
                ),
            )

        consumed = await self.db.consume_oauth_nonce(nonce)
        if consumed is None:
            logger.info("OAuth nonce was expired, unknown, or already used")
            return OAuthCallbackResult(
                telegram_user_id=None,
                result=VerificationResult(
                    outcome=VerificationOutcome.ERROR,
                    method=self.method_name,
                    detail="expired or already-used verification link",
                ),
            )

        telegram_user_id, code_verifier = consumed
        if not code:
            return OAuthCallbackResult(
                telegram_user_id=telegram_user_id,
                result=VerificationResult(
                    outcome=VerificationOutcome.ERROR,
                    method=self.method_name,
                    detail="google did not return an authorization code",
                ),
            )

        access_token = await self._exchange_code(code, code_verifier)
        if access_token is None:
            return OAuthCallbackResult(
                telegram_user_id=telegram_user_id,
                result=VerificationResult(
                    outcome=VerificationOutcome.ERROR,
                    method=self.method_name,
                    detail="token exchange failed",
                ),
            )

        try:
            subscribed = await self._is_subscribed(access_token)
        finally:
            # Always give the token back, even if the check blew up.
            await self._revoke_token(access_token)

        if subscribed is None:
            return OAuthCallbackResult(
                telegram_user_id=telegram_user_id,
                result=VerificationResult(
                    outcome=VerificationOutcome.ERROR,
                    method=self.method_name,
                    detail="youtube api call failed",
                ),
            )

        if not subscribed:
            return OAuthCallbackResult(
                telegram_user_id=telegram_user_id,
                result=VerificationResult(
                    outcome=VerificationOutcome.NOT_SUBSCRIBED,
                    was_technically_verified=True,  # we really did check
                    method=self.method_name,
                    detail="youtube reports no subscription to this channel",
                ),
            )

        await self.db.mark_confirmed(telegram_user_id, self.method_name)
        logger.info(
            "User %s genuinely verified as subscribed via YouTube Data API",
            telegram_user_id,
        )
        return OAuthCallbackResult(
            telegram_user_id=telegram_user_id,
            result=VerificationResult(
                outcome=VerificationOutcome.VERIFIED,
                was_technically_verified=True,
                method=self.method_name,
                detail="youtube confirmed an active subscription",
            ),
        )

    # -- google plumbing -------------------------------------------------

    async def _exchange_code(self, code: str, code_verifier: str) -> str | None:
        payload = {
            "code": code,
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret,
            "redirect_uri": self.settings.oauth_redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        try:
            response = await self.client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
        except httpx.HTTPError as exc:
            logger.error("Network error exchanging OAuth code: %s", exc)
            return None

        if response.status_code != 200:
            # Body can contain the client_secret echo in some error shapes;
            # log only the coarse error field.
            detail = _safe_error(response)
            logger.error(
                "Token exchange failed (HTTP %s): %s", response.status_code, detail
            )
            return None

        token = response.json().get("access_token")
        if not token:
            logger.error("Token exchange succeeded but no access_token was returned")
            return None
        return token

    async def _is_subscribed(self, access_token: str) -> bool | None:
        """``True``/``False``, or ``None`` when the API call itself failed."""
        params = {
            "part": "snippet",
            "mine": "true",
            "forChannelId": self.settings.youtube_channel_id,
            "maxResults": "1",
        }
        try:
            response = await self.client.get(
                YOUTUBE_SUBSCRIPTIONS_ENDPOINT,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            logger.error("Network error calling the YouTube API: %s", exc)
            return None

        if response.status_code == 403:
            logger.error(
                "YouTube API returned 403 - check that the YouTube Data API v3 is "
                "enabled and that you have quota left. Detail: %s",
                _safe_error(response),
            )
            return None
        if response.status_code != 200:
            logger.error(
                "YouTube API error (HTTP %s): %s",
                response.status_code,
                _safe_error(response),
            )
            return None

        data: dict[str, Any] = response.json()
        items = data.get("items") or []
        return len(items) > 0

    async def _revoke_token(self, access_token: str) -> None:
        """Best-effort revocation. Failure here must never break the flow."""
        try:
            await self.client.post(
                GOOGLE_REVOKE_ENDPOINT,
                data={"token": access_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            logger.debug("Google access token revoked after the subscription check")
        except httpx.HTTPError as exc:  # pragma: no cover - best effort
            logger.warning("Could not revoke Google access token: %s", exc)


def _safe_error(response: httpx.Response) -> str:
    """Extract a short error string without dumping the whole response body."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - any parse failure is fine here
        return response.text[:200]
    if isinstance(body, dict):
        if "error_description" in body:
            return str(body["error_description"])[:300]
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))[:300]
        if error:
            return str(error)[:300]
    return str(body)[:200]


# ---------------------------------------------------------------------------
# helper used by scripts/tools (API key only, no OAuth needed)
# ---------------------------------------------------------------------------


#: The channel id is embedded in the public HTML of every channel page.
_EXTERNAL_ID_RE = re.compile(r'"(?:externalId|channelId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"')
_CANONICAL_RE = re.compile(r'https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})')

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


async def resolve_channel_id_from_page(channel_url: str) -> str | None:
    """Read the UC... id straight off the public channel page.

    No API key, no OAuth, no Google account - the id is public information
    embedded in the page HTML. This is the fallback used when the owner has
    not set up a YouTube Data API key yet.
    """
    if not channel_url:
        return None
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS
    ) as client:
        try:
            response = await client.get(channel_url)
        except httpx.HTTPError as exc:
            logger.error("Could not fetch the channel page: %s", exc)
            return None

    if response.status_code != 200:
        logger.error("Channel page returned HTTP %s", response.status_code)
        return None

    for pattern in (_EXTERNAL_ID_RE, _CANONICAL_RE):
        match = pattern.search(response.text)
        if match:
            return match.group(1)
    logger.error("Could not find a channel id in the page HTML")
    return None


async def resolve_channel_id(
    *, api_key: str, handle: str | None = None, username: str | None = None
) -> str | None:
    """Look up a UC... channel id from an @handle using an API key.

    Used by ``python -m src.tools.resolve_channel`` so the owner does not have
    to dig through YouTube's page source.
    """
    if not api_key:
        return None
    params: dict[str, str] = {"part": "id", "key": api_key}
    if handle:
        params["forHandle"] = handle if handle.startswith("@") else f"@{handle}"
    elif username:
        params["forUsername"] = username
    else:
        return None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            response = await client.get(YOUTUBE_CHANNELS_ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            logger.error("Network error resolving channel id: %s", exc)
            return None

    if response.status_code != 200:
        logger.error(
            "Channel lookup failed (HTTP %s): %s",
            response.status_code,
            _safe_error(response),
        )
        return None

    items = response.json().get("items") or []
    return items[0]["id"] if items else None
