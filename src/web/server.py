"""A very small aiohttp server.

It exists for two reasons:

1. **Phase 2 OAuth callback.** Google must redirect the user's browser to a
   public HTTPS URL after they consent. ``/oauth/callback`` is that URL.
2. **Health checks.** Most free hosts (Render, Koyeb, Fly, ...) will only keep
   a service alive if it answers on an HTTP port. ``/health`` does that, so
   the bot can run as a "web service" on a free tier.

In Phase 1 (honour system) only the health endpoints are mounted, so the whole
OAuth surface simply does not exist.

Security notes
--------------
* ``/oauth/callback`` validates an HMAC-signed, single-use, expiring ``state``
  before doing anything else (see ``verification/youtube_oauth.py``).
* Responses are static HTML with no user-controlled interpolation, so there is
  no reflected-XSS surface. The only dynamic value is the bot's own username,
  which is URL-quoted.
* No secrets are ever rendered into a page or a URL.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiohttp import web

from src.verification import VerificationOutcome
from src.verification.youtube_oauth import YouTubeOAuthVerifier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from telegram import Bot

    from src.bot.handlers import GateHandlers
    from src.config import Settings

logger = logging.getLogger(__name__)


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; padding: 24px; background: #0f1115; color: #e8eaed; }}
  .card {{ max-width: 420px; width: 100%; text-align: center; background: #171a21;
          border: 1px solid #262b36; border-radius: 16px; padding: 32px 24px; }}
  .icon {{ font-size: 48px; line-height: 1; margin-bottom: 12px; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ font-size: 15px; line-height: 1.5; color: #a8b0bd; margin: 0 0 20px; }}
  a.btn {{ display: inline-block; background: #229ED9; color: #fff; text-decoration: none;
          font-weight: 600; padding: 12px 22px; border-radius: 10px; }}
</style></head>
<body><div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  {button}
</div></body></html>"""


def _render(
    *, icon: str, title: str, message: str, bot_username: str, status: int = 200
) -> web.Response:
    button = ""
    if bot_username:
        safe = quote(bot_username, safe="")
        button = f'<a class="btn" href="https://t.me/{safe}">← Back to Telegram</a>'
    html = _PAGE.format(icon=icon, title=title, message=message, button=button)
    return web.Response(text=html, content_type="text/html", status=status)


def build_app(
    settings: "Settings",
    handlers: "GateHandlers | None" = None,
    bot: "Bot | None" = None,
) -> web.Application:
    """Create the aiohttp application.

    Kept as a plain factory (no side effects) so tests can drive it with
    ``aiohttp.test_utils`` without starting a real socket.
    """
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "verification_mode": settings.verification_mode.value,
                "invite_mode": settings.invite_mode.value,
            }
        )

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)

    if not settings.oauth_enabled:
        return app

    async def oauth_callback(request: web.Request) -> web.Response:
        verifier = request.app["verifier"]
        bot_username = request.app.get("bot_username", "")

        outcome = await verifier.handle_callback(
            code=request.query.get("code"),
            state=request.query.get("state"),
            error=request.query.get("error"),
        )
        result = outcome.result
        user_id = outcome.telegram_user_id

        gate: "GateHandlers | None" = request.app.get("handlers")
        live_bot: "Bot | None" = request.app.get("bot")

        # Push the outcome into Telegram so the user does not have to tap again.
        if user_id is not None and gate is not None and live_bot is not None:
            try:
                if result.outcome is VerificationOutcome.VERIFIED:
                    await gate.notify_verified(live_bot, user_id)
                else:
                    await gate.notify_failed(live_bot, user_id, result.outcome)
            except Exception:  # noqa: BLE001 - never let the page 500
                logger.exception("Failed to notify user %s about OAuth result", user_id)

        if result.outcome is VerificationOutcome.VERIFIED:
            return _render(
                icon="✅",
                title="Verified!",
                message="YouTube confirmed your subscription. "
                "Head back to Telegram for your group link.",
                bot_username=bot_username,
            )
        if result.outcome is VerificationOutcome.NOT_SUBSCRIBED:
            return _render(
                icon="❌",
                title="Not subscribed yet",
                message="That Google account isn't subscribed to the channel. "
                "Subscribe, then tap “Check again” in Telegram.",
                bot_username=bot_username,
            )
        return _render(
            icon="⚠️",
            title="Verification didn't complete",
            message="That link may have expired or already been used. "
            "Go back to Telegram and request a fresh one.",
            bot_username=bot_username,
            status=400,
        )

    app.router.add_get("/oauth/callback", oauth_callback)

    if isinstance(handlers, object) and handlers is not None:
        app["handlers"] = handlers
    if bot is not None:
        app["bot"] = bot
    return app


class WebServer:
    """Runs :func:`build_app` alongside the Telegram bot in the same loop."""

    def __init__(
        self,
        settings: "Settings",
        handlers: "GateHandlers | None" = None,
        verifier: YouTubeOAuthVerifier | None = None,
    ) -> None:
        self.settings = settings
        self.handlers = handlers
        self.verifier = verifier
        self._runner: web.AppRunner | None = None
        self.app: web.Application | None = None

    @property
    def enabled(self) -> bool:
        """Only run a socket when we actually need one."""
        return self.settings.oauth_enabled or self.settings.health_check_port is not None

    @property
    def port(self) -> int:
        if self.settings.oauth_enabled:
            return self.settings.web_port
        # Compare against None, not truthiness: port 0 is a legitimate value
        # meaning "let the OS pick a free port", and `or` would discard it.
        if self.settings.health_check_port is not None:
            return self.settings.health_check_port
        return self.settings.web_port

    async def start(self, bot: "Bot | None" = None, bot_username: str = "") -> None:
        if not self.enabled:
            logger.info("Web server not needed for this configuration; skipping")
            return

        app = build_app(self.settings, self.handlers, bot)
        app["bot_username"] = bot_username
        if self.verifier is not None:
            app["verifier"] = self.verifier
        self.app = app

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.web_host, self.port)
        await site.start()
        logger.info(
            "Web server listening on http://%s:%s", self.settings.web_host, self.port
        )
        if self.settings.oauth_enabled:
            logger.info(
                "OAuth redirect URI (must match Google Cloud exactly): %s",
                self.settings.oauth_redirect_uri,
            )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Web server stopped")
