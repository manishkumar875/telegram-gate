"""Wires everything together and runs the bot."""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any

from telegram import BotCommand, Update
from telegram.error import InvalidToken, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.config import ConfigError, Settings, VerificationMode, load_settings
from src.console import force_utf8_console
from src.database import Database
from src.verification import build_verifier
from src.verification.youtube_oauth import YouTubeOAuthVerifier
from src.web import WebServer

from .handlers import GateHandlers
from .invites import InviteService, check_group_permissions

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Get the group link"),
    BotCommand("help", "How this works"),
    BotCommand("status", "Where you are in the process"),
    BotCommand("privacy", "What data is stored"),
]


def configure_logging(level: str = "INFO") -> None:
    force_utf8_console()
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stdout,
    )
    # httpx logs every single Telegram poll at INFO; that is pure noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class GateBot:
    """Owns the lifecycle of every component."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_path)
        self.application: Application | None = None
        self.verifier: Any = None
        self.handlers: GateHandlers | None = None
        self.web: WebServer | None = None

    # -- setup -----------------------------------------------------------

    def build(self) -> Application:
        """Assemble everything synchronously.

        Async work (opening the database, HTTP clients, the web server) is
        deliberately deferred to :meth:`_post_init`, which python-telegram-bot
        runs *after* ``initialize()`` and guarantees to pair with
        :meth:`_post_shutdown` in a ``finally`` block. Doing it here instead
        would leak an open SQLite connection whenever startup fails - and
        because aiosqlite's worker thread is not a daemon, a leaked connection
        hangs the process on exit instead of quitting.

        ``GateHandlers`` only touches ``Database.conn`` when an update
        arrives, so handing it a not-yet-connected Database is safe.
        """
        self.verifier = build_verifier(self.settings, self.db)
        invites = InviteService(self.settings, self.db)
        self.handlers = GateHandlers(self.settings, self.db, self.verifier, invites)

        builder = (
            ApplicationBuilder()
            .token(self.settings.bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
        )
        # Only needed for a self-hosted Bot API server, or for tests that
        # point the bot at a local stand-in for api.telegram.org.
        if self.settings.api_base_url != "https://api.telegram.org/bot":
            builder = builder.base_url(self.settings.api_base_url)
        application = builder.build()
        self._register(application)
        self.application = application
        return application

    def _register(self, application: Application) -> None:
        handlers = self.handlers
        assert handlers is not None

        application.add_handler(CommandHandler("start", handlers.start))
        application.add_handler(CommandHandler("help", handlers.help_command))
        application.add_handler(CommandHandler("status", handlers.status))
        application.add_handler(CommandHandler("privacy", handlers.privacy))
        application.add_handler(CommandHandler("forgetme", handlers.forget_me))
        application.add_handler(CommandHandler("stats", handlers.stats))

        application.add_handler(CallbackQueryHandler(handlers.on_callback))
        application.add_handler(ChatJoinRequestHandler(handlers.on_join_request))

        # Anything else typed in a private chat.
        application.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.COMMAND, handlers.on_unknown_command
            )
        )
        application.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                handlers.on_text,
            )
        )

        application.add_error_handler(handlers.on_error)

    # -- lifecycle hooks -------------------------------------------------

    async def _post_init(self, application: Application) -> None:
        # Open the resources that need an event loop. Anything opened here is
        # closed again by _post_shutdown, which PTB always runs.
        await self.db.connect()
        await self.db.purge_expired_nonces()
        await self.verifier.start()

        me = await application.bot.get_me()
        logger.info("Connected to Telegram as @%s (id %s)", me.username, me.id)

        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
        except TelegramError as exc:
            logger.warning("Could not set the command menu: %s", exc)

        # Check group wiring early so the owner sees the problem in the logs
        # rather than when the first real user hits the gate. This is purely
        # informational, so no failure inside it may abort startup.
        try:
            problems = await check_group_permissions(application.bot, self.settings)
        except Exception:  # noqa: BLE001
            logger.exception("Group permission check failed; starting anyway")
            problems = []
        for problem in problems:
            logger.warning("GROUP SETUP: %s", problem)
        if not problems and self.settings.needs_group_admin:
            logger.info("Group permissions look correct for INVITE_MODE=%s",
                        self.settings.invite_mode.value)

        self.web = WebServer(
            self.settings,
            self.handlers,
            self.verifier if isinstance(self.verifier, YouTubeOAuthVerifier) else None,
        )
        await self.web.start(bot=application.bot, bot_username=me.username or "")

        if self.settings.verification_mode is VerificationMode.HONOR:
            logger.warning(
                "VERIFICATION_MODE=honor - YouTube subscriptions are NOT checked. "
                "The bot tells users this honestly. Set VERIFICATION_MODE="
                "youtube_oauth for real verification."
            )
        else:
            logger.info("Real YouTube verification is ACTIVE (OAuth + Data API v3)")

        # Housekeeping: drop expired OAuth nonces every 30 minutes.
        if application.job_queue is not None:
            application.job_queue.run_repeating(
                self._housekeeping, interval=1800, first=1800
            )

        logger.info("Bot is running. Open https://t.me/%s and send /start.", me.username)

    async def _housekeeping(self, _context: Any) -> None:
        removed = await self.db.purge_expired_nonces()
        if self.handlers is not None:
            self.handlers.limiter.prune()
        if removed:
            logger.debug("Purged %s expired OAuth nonce(s)", removed)

    async def _post_shutdown(self, _application: Application) -> None:
        """Release every resource, even if one of the steps fails.

        Closing the database matters most: aiosqlite's worker thread is not a
        daemon, so leaving it open would stop the process from ever exiting.
        """
        for name, closer in (
            ("web server", self.web.stop if self.web else None),
            ("verifier", self.verifier.stop if self.verifier else None),
            ("database", self.db.close),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001 - one failure must not skip the rest
                logger.exception("Error while shutting down the %s", name)
        logger.info("Shutdown complete")


def _install_windows_stop_handlers(application: Application) -> None:
    """Make Ctrl+C / Ctrl+Break shut the bot down gracefully on Windows.

    Two Windows-specific problems make this necessary:

    * python-telegram-bot only installs stop-signal handlers on non-Windows
      platforms, because asyncio's ``add_signal_handler`` is not implemented
      on Windows. So ``run_polling`` has no idea a stop was requested.
    * Python's *default* action for ``SIGBREAK`` (Ctrl+Break, and what a
      parent process sends as CTRL_BREAK_EVENT) is to kill the process
      outright, so shutdown hooks never run and the database is never closed.

    Asking the Application to stop instead lets run_polling fall through to
    its normal shutdown path, which runs our post_shutdown hook.
    """
    if sys.platform != "win32":
        return  # POSIX: python-telegram-bot already handles this itself

    def _request_stop(signum: int, _frame: object) -> None:
        logger.info("Stop signal received - shutting down gracefully...")
        try:
            application.stop_running()
        except RuntimeError:
            # No running loop yet; fall back to the standard interrupt so
            # python-telegram-bot's own except-clause deals with it.
            raise KeyboardInterrupt from None

    for signal_name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            signal.signal(signal_number, _request_stop)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            logger.debug("Could not install a handler for %s", signal_name)


def _banner(settings: Settings) -> None:
    print("=" * 62)
    print("  Telegram YouTube Gate")
    print("=" * 62)
    print(f"  Verification : {settings.verification_mode.value}")
    print(f"  Invite mode  : {settings.invite_mode.value}")
    print(f"  YouTube      : {settings.youtube_channel_url}")
    print(f"  Database     : {settings.database_path}")
    if settings.oauth_enabled:
        print(f"  OAuth callback: {settings.oauth_redirect_uri}")
    print("=" * 62)
    print("  Press Ctrl+C to stop.")
    print()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    _banner(settings)

    application = GateBot(settings).build()
    _install_windows_stop_handlers(application)

    try:
        # run_polling owns the whole lifecycle: initialize -> post_init ->
        # poll -> (on Ctrl+C) stop -> shutdown -> post_shutdown. Using it
        # instead of driving the Application by hand is what guarantees our
        # post_init/post_shutdown hooks actually run.
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
    except InvalidToken:
        print(
            "\nTELEGRAM_BOT_TOKEN was rejected by Telegram.\n"
            "Open @BotFather, send /mybots, pick your bot, choose 'API Token',\n"
            "and copy the token into your .env file again.\n",
            file=sys.stderr,
        )
        return 2
    except NetworkError as exc:
        print(
            f"\nCould not reach Telegram: {exc}\n"
            "Check your internet connection, then try again.\n",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, SystemExit):
        pass
    print("\nStopped.")
    return 0
