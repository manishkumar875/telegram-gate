"""Find your Telegram group's numeric ID and save it to .env.

    python -m src.tools.find_group_id

Add the bot to your group first, then either:
  * send any message in the group, or
  * change the bot's admin rights (add/remove any permission)

Either one makes Telegram tell the bot which chat it is in. The ID is then
written to .env as TELEGRAM_GROUP_ID, and the bot's permissions are checked.

This tool only READS. It never posts, never changes your group, and never
touches your members.
"""

from __future__ import annotations

import asyncio
import os
import sys

from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    ChatMemberHandler,
    MessageHandler,
    filters,
)

def instructions(bot_username: str, privacy_on: bool) -> str:
    """Tell the owner what actually works for THIS bot's privacy setting.

    Telegram's 'privacy mode' is ON by default, and a bot with it on never
    receives ordinary group chatter - so "send any message" is bad advice.
    Commands addressed to the bot are always delivered, so we ask for one.
    """
    mention = f"@{bot_username}" if bot_username else ""
    lines = [
        "",
        "=" * 60,
        "  FIND YOUR GROUP ID",
        "=" * 60,
        "",
        "  The bot is listening. In your group, do ONE of these:",
        "",
    ]
    if privacy_on:
        lines += [
            f"    A) Send this exact text in the group:",
            "",
            f"           /start{mention}",
            "",
            "       (Privacy mode is ON for this bot, so a plain 'hi' is",
            "        NOT delivered to it - it must be a command.)",
        ]
    else:
        lines += [
            "    A) Send any message in the group (privacy mode is OFF,",
            "       so anything works - even 'hi').",
        ]
    lines += [
        "",
        "    B) Or: Group > Administrators > your bot > toggle any",
        "       permission off and on again.",
        "",
        "  Either one is enough. You can delete the message afterwards.",
        "",
        "  Waiting...  (Ctrl+C to cancel)",
        "",
    ]
    return "\n".join(lines)


async def _describe(bot, chat_id: int) -> list[str]:
    """Read-only report on the bot's standing in the group."""
    notes: list[str] = []
    try:
        count = await bot.get_chat_member_count(chat_id)
        notes.append(f"Members: {count}  (none of them will be touched)")
    except TelegramError as exc:
        notes.append(f"Could not read the member count: {exc}")

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "?")
        if status != "administrator":
            notes.append(
                f"WARNING: the bot's status is '{status}', not administrator."
            )
            notes.append("         INVITE_MODE=request needs it to be an admin.")
        elif not getattr(member, "can_invite_users", False):
            notes.append("WARNING: bot is an admin but 'Invite Users via Link' is OFF.")
            notes.append("         Turn it on in Group > Administrators > your bot.")
        else:
            notes.append("Bot is an admin WITH 'Invite Users via Link'. Correct.")
    except TelegramError as exc:
        notes.append(f"Could not read the bot's permissions: {exc}")
    except Exception as exc:  # noqa: BLE001 - library parse quirks
        notes.append(f"Could not fully inspect permissions ({type(exc).__name__}).")
    return notes


def _save(chat_id: int) -> str:
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
        from scripts.setup_wizard import write_env_value

        write_env_value("TELEGRAM_GROUP_ID", str(chat_id))
        return "Saved to .env as TELEGRAM_GROUP_ID."
    except Exception as exc:  # noqa: BLE001
        return f"Could not write to .env ({exc}). Add it manually: TELEGRAM_GROUP_ID={chat_id}"


async def _run() -> int:
    from dotenv import load_dotenv

    from src.config.settings import PROJECT_ROOT

    load_dotenv(PROJECT_ROOT / ".env")
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip().strip("\"'")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set in .env.\n"
            "Get it from @BotFather, then run:\n"
            "    python scripts/setup_wizard.py --from-clipboard TELEGRAM_BOT_TOKEN",
            file=sys.stderr,
        )
        return 2

    found: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def report(chat, bot) -> None:
        if found.done() or chat is None or chat.type not in {"group", "supergroup"}:
            return
        print("\n" + "=" * 60)
        print(f"  GROUP NAME : {chat.title}")
        print(f"  GROUP ID   : {chat.id}")
        print(f"  GROUP TYPE : {chat.type}")
        print("=" * 60)
        for note in await _describe(bot, chat.id):
            print(f"  {note}")
        print()
        print(f"  {_save(chat.id)}")
        print()
        found.set_result(chat.id)

    async def on_message(update: Update, context) -> None:
        await report(update.effective_chat, context.bot)

    async def on_member(update: Update, context) -> None:
        member_update = update.my_chat_member or update.chat_member
        if member_update is not None:
            await report(member_update.chat, context.bot)

    application = ApplicationBuilder().token(token).build()
    application.add_handler(MessageHandler(filters.ALL, on_message))
    application.add_handler(
        ChatMemberHandler(on_member, ChatMemberHandler.ANY_CHAT_MEMBER)
    )

    async with application:
        me = await application.bot.get_me()
        print(
            instructions(
                me.username or "", privacy_on=not me.can_read_all_group_messages
            )
        )
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        try:
            await asyncio.wait_for(found, timeout=600)
        except asyncio.TimeoutError:
            print(
                "\nTimed out after 10 minutes.\n"
                "Is the bot actually in the group? If it is not an admin, also\n"
                "turn OFF Group Privacy: @BotFather > /mybots > your bot >\n"
                "Bot Settings > Group Privacy > Turn off.",
                file=sys.stderr,
            )
            return 1
        finally:
            await application.updater.stop()
            await application.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except InvalidToken:
        print("TELEGRAM_BOT_TOKEN was rejected by Telegram.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
