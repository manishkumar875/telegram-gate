#!/usr/bin/env python3
"""Check everything BEFORE you run the bot for real.

    python scripts/preflight.py

It verifies, in order:
  1. Python version
  2. Dependencies are installed
  3. .env exists and every value is valid
  4. The database file can be created and written to
  5. Telegram accepts your bot token (getMe)
  6. The bot's group permissions match your INVITE_MODE
  7. Phase 2 only: your OAuth settings are internally consistent

Exit code 0 means "you are good to go".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = "  [PASS]"
FAIL = "  [FAIL]"
WARN = "  [WARN]"
INFO = "  [INFO]"


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        print(f"{PASS} {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"{FAIL} {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"{WARN} {message}")

    def info(self, message: str) -> None:
        print(f"{INFO} {message}")


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 52 - len(title)))


async def run() -> int:
    from src.console import force_utf8_console  # stdlib only, safe this early

    force_utf8_console()
    report = Report()
    print("=" * 62)
    print("  PREFLIGHT CHECK")
    print("=" * 62)

    # 1. Python -----------------------------------------------------------
    section("Python")
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        report.ok(f"Python {major}.{minor} is supported")
    else:
        report.fail(f"Python {major}.{minor} is too old - this project needs 3.10+")
        return 1

    # 2. Dependencies -----------------------------------------------------
    section("Dependencies")
    missing = []
    for module, package in [
        ("telegram", "python-telegram-bot"),
        ("aiosqlite", "aiosqlite"),
        ("dotenv", "python-dotenv"),
        ("httpx", "httpx"),
        ("aiohttp", "aiohttp"),
    ]:
        try:
            __import__(module)
            report.ok(f"{package} is installed")
        except ImportError:
            missing.append(package)
            report.fail(f"{package} is NOT installed")
    if missing:
        print("\n  Fix it with:\n      pip install -r requirements.txt\n")
        return 1

    # 3. Configuration ----------------------------------------------------
    section("Configuration (.env)")
    from src.config import ConfigError, InviteMode, VerificationMode, load_settings
    from src.config.settings import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        report.fail(f"No .env file found at {env_path}")
        print(
            "\n  Create one by copying the example:\n"
            "      copy .env.example .env      (Windows)\n"
            "      cp .env.example .env        (macOS / Linux)\n"
        )
        return 1
    report.ok(f".env found at {env_path}")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc))
        return 1
    report.ok("Every configuration value is valid")

    for key, value in settings.redacted().items():
        report.info(f"{key:24s} = {value}")

    # 4. Database ---------------------------------------------------------
    section("Database")
    from src.database import Database

    try:
        db = Database(settings.database_path)
        await db.connect()
        await db.touch_user(1)
        record = await db.get_user(1)
        assert record is not None
        await db.delete_user(1)
        await db.close()
        report.ok(f"SQLite is writable at {settings.database_path}")
    except Exception as exc:  # noqa: BLE001
        report.fail(f"Could not use the database: {exc}")

    # 5. Telegram ---------------------------------------------------------
    section("Telegram")
    from telegram import Bot
    from telegram.error import InvalidToken, TelegramError

    bot_username = ""
    try:
        bot = Bot(settings.bot_token)
        async with bot:
            me = await bot.get_me()
            bot_username = me.username or ""
            report.ok(f"Token works - the bot is @{me.username} (id {me.id})")

            # 6. Group ----------------------------------------------------
            section("Telegram group")
            if settings.invite_mode is InviteMode.STATIC:
                report.ok(
                    "INVITE_MODE=static - the bot does not need to be a group admin"
                )
                report.info(f"Users will be sent: {settings.group_invite_link}")
                report.warn(
                    "A static link can be forwarded to people who never subscribed. "
                    "Use INVITE_MODE=request for a stronger gate."
                )
            else:
                from src.bot.invites import check_group_permissions

                problems = await check_group_permissions(bot, settings)
                if problems:
                    for problem in problems:
                        report.fail(problem)
                else:
                    report.ok(
                        f"The bot is an admin with invite rights - INVITE_MODE="
                        f"{settings.invite_mode.value} will work"
                    )
                    try:
                        chat = await bot.get_chat(settings.group_chat_id)
                        report.info(f"Group: {chat.title} (id {chat.id})")
                        count = await bot.get_chat_member_count(settings.group_chat_id)
                        report.info(f"Current members: {count} - none will be touched")
                    except TelegramError as exc:
                        report.warn(f"Could not read group details: {exc}")
    except InvalidToken:
        report.fail(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Get a fresh copy from "
            "@BotFather (/mybots > your bot > API Token)."
        )
    except TelegramError as exc:
        report.fail(f"Could not reach Telegram: {exc}")

    # 7. Phase 2 ----------------------------------------------------------
    section("Verification mode")
    if settings.verification_mode is VerificationMode.HONOR:
        report.ok("VERIFICATION_MODE=honor")
        report.warn(
            "Nothing about YouTube is technically verified in this mode. "
            "The bot says so honestly to every user."
        )
    else:
        report.ok("VERIFICATION_MODE=youtube_oauth - real verification is on")
        report.info(f"Channel checked : {settings.youtube_channel_id}")
        report.info(f"Redirect URI    : {settings.oauth_redirect_uri}")
        report.warn(
            "This EXACT redirect URI must be listed under 'Authorised redirect "
            "URIs' in your Google Cloud OAuth client, character for character."
        )
        report.warn(
            "Until Google verifies your OAuth consent screen, only the accounts "
            "listed as Test Users (max 100) can complete verification."
        )

    if bot_username:
        section("Your links")
        report.info(f"Put this in your Instagram bio: https://t.me/{bot_username}")

    # Summary ------------------------------------------------------------
    print("\n" + "=" * 62)
    if report.failures:
        print(f"  RESULT: {report.failures} problem(s) must be fixed.")
        print("=" * 62)
        return 1
    if report.warnings:
        print(f"  RESULT: ready to run ({report.warnings} thing(s) worth knowing).")
    else:
        print("  RESULT: ready to run.")
    print("=" * 62)
    print("\n  Start the bot with:   python run.py\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(1)
