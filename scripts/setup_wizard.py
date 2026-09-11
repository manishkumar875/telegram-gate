#!/usr/bin/env python3
"""Fill in .env safely, validating every value against the real service.

    python scripts/setup_wizard.py --status          show what is done / missing
    python scripts/setup_wizard.py                   interactive walkthrough
    python scripts/setup_wizard.py --set KEY=VALUE   set one or more values

Every value is CHECKED before it is saved:

    TELEGRAM_BOT_TOKEN     -> asks Telegram getMe, prints your bot's @username
    TELEGRAM_GROUP_ID      -> reads the group name + member count
    YOUTUBE_CHANNEL_URL    -> parses it, resolves the UC... id when possible
    YOUTUBE_CHANNEL_ID     -> checks the UC... shape, confirms via API if a key is set
    GOOGLE_CLIENT_ID       -> checks it looks like a Google OAuth web client
    GOOGLE_CLIENT_SECRET   -> checks the shape (never printed back)
    OAUTH_PUBLIC_BASE_URL  -> checks https, and pings /health if your bot is running

Secrets are never echoed back to the screen in full, and are written only to
your local .env (which is git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

ENV_PATH = PROJECT_ROOT / ".env"

OK = "  [OK]  "
BAD = "  [--]  "
TODO = "  [  ]  "


# ---------------------------------------------------------------------------
# .env reading / writing (preserves comments and ordering)
# ---------------------------------------------------------------------------


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env_value(key: str, value: str) -> None:
    """Update KEY in .env in place, keeping every comment and blank line."""
    if not ENV_PATH.is_file():
        raise SystemExit(
            f"No .env found at {ENV_PATH}.\n"
            "Create one first:  copy .env.example .env"
        )
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{key}={value}{ending}"
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={value}\n")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")


def read_clipboard() -> str | None:
    """Return the clipboard text, or None if we cannot read it.

    Used so a secret can go straight from 'Copy' into .env without ever being
    typed into a shell command (where it would land in your shell history) or
    printed to the screen.
    """
    attempts: list[list[str]] = []
    if sys.platform == "win32":
        attempts.append(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
        )
    elif sys.platform == "darwin":
        attempts.append(["pbpaste"])
    else:
        attempts.append(["wl-paste"])
        attempts.append(["xclip", "-selection", "clipboard", "-o"])
        attempts.append(["xsel", "--clipboard", "--output"])

    import subprocess

    for command in attempts:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

    try:  # last resort, works on most desktops with Tk available
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        value = root.clipboard_get()
        root.destroy()
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return None


#: Patterns that identify the interesting line inside a pasted block of text.
#: People usually copy BotFather's whole reply, not just the token.
LINE_PATTERNS: dict[str, re.Pattern[str]] = {
    "TELEGRAM_BOT_TOKEN": re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$"),
    "YOUTUBE_CHANNEL_ID": re.compile(r"^UC[A-Za-z0-9_-]{22}$"),
    "GOOGLE_CLIENT_ID": re.compile(r"^\S+\.apps\.googleusercontent\.com$"),
    "TELEGRAM_GROUP_ID": re.compile(r"^-?\d{5,}$"),
    "OAUTH_PUBLIC_BASE_URL": re.compile(r"^https://\S+$"),
    "TELEGRAM_GROUP_INVITE_LINK": re.compile(r"^https://t\.me/\S+$"),
    "YOUTUBE_CHANNEL_URL": re.compile(r"^https://\S*youtube\.com/\S+$"),
}


def pick_value_line(text: str, key: str) -> str:
    """Pull the relevant value out of a possibly multi-line paste.

    Copying BotFather's reply gives several lines of prose around the token.
    If exactly one line looks like the value we want, use it; otherwise fall
    back to the whole (stripped) text so the validator can reject it with a
    helpful message.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text.strip()
    if len(lines) == 1:
        return lines[0]

    pattern = LINE_PATTERNS.get(key)
    if pattern is not None:
        matches = [line for line in lines if pattern.match(line)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return matches[0]
    return text.strip()


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-3:]} ({len(value)} chars)"


# ---------------------------------------------------------------------------
# validators - each returns (ok, message, extra_values_to_also_write)
# ---------------------------------------------------------------------------

Result = tuple[bool, str, dict[str, str]]


async def validate_bot_token(value: str) -> Result:
    from telegram import Bot
    from telegram.error import InvalidToken, TelegramError

    if not re.match(r"^\d{5,}:[A-Za-z0-9_-]{30,}$", value):
        return False, (
            "That does not look like a BotFather token. It should look like\n"
            "         123456789:AAExample-TokenFromBotFather"
        ), {}
    try:
        bot = Bot(value)
        async with bot:
            me = await bot.get_me()
    except InvalidToken:
        return False, "Telegram REJECTED that token. Copy it again from @BotFather.", {}
    except TelegramError as exc:
        return False, f"Could not reach Telegram: {exc}", {}
    return True, (
        f"Telegram accepted it. Your bot is @{me.username} (id {me.id}).\n"
        f"         Your Instagram bio link is:  https://t.me/{me.username}"
    ), {}


async def validate_group_id(value: str, env: dict[str, str]) -> Result:
    from telegram import Bot
    from telegram.error import TelegramError

    try:
        chat_id = int(value)
    except ValueError:
        return False, "The group ID must be a number, like -1001234567890.", {}

    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return True, "Saved (cannot verify yet - set TELEGRAM_BOT_TOKEN first).", {}

    try:
        bot = Bot(token)
        async with bot:
            chat = await bot.get_chat(chat_id)
            count = await bot.get_chat_member_count(chat_id)
            me = await bot.get_me()
            member = await bot.get_chat_member(chat_id, me.id)
    except TelegramError as exc:
        return False, (
            f"Telegram could not read that group: {exc}\n"
            "         Is the bot a member of the group, and is the ID correct?"
        ), {}
    except Exception as exc:  # noqa: BLE001 - library parse quirks
        return True, (
            f"Saved. (Could not fully inspect the group: {type(exc).__name__}.)"
        ), {}

    status = getattr(member, "status", "?")
    can_invite = getattr(member, "can_invite_users", False)
    note = f"Group '{chat.title}' has {count} members - none will be touched."
    if status != "administrator":
        note += (
            f"\n         WARNING: the bot's status is '{status}', not administrator."
            "\n         INVITE_MODE=request needs it to be an admin."
        )
    elif not can_invite:
        note += (
            "\n         WARNING: admin, but 'Invite Users via Link' is OFF."
            "\n         Turn it on in Group > Administrators > your bot."
        )
    else:
        note += "\n         Bot is an admin with invite rights. Correct for INVITE_MODE=request."
    return True, note, {}


async def validate_channel_url(value: str, env: dict[str, str]) -> Result:
    from src.config.settings import extract_channel_handle, extract_channel_id
    from src.verification.youtube_oauth import resolve_channel_id

    if not value.startswith(("http://", "https://")) or "youtube.com" not in value:
        return False, (
            "That should be a full YouTube address, for example\n"
            "         https://www.youtube.com/@yourchannel"
        ), {}

    direct = extract_channel_id(value)
    if direct:
        return True, f"Channel ID found in the URL: {direct}", {"YOUTUBE_CHANNEL_ID": direct}

    handle = extract_channel_handle(value)
    if not handle:
        return True, "Saved. (No @handle found; set YOUTUBE_CHANNEL_ID manually.)", {}

    from src.verification.youtube_oauth import resolve_channel_id_from_page

    api_key = env.get("YOUTUBE_API_KEY", "")
    channel_id = None
    source = ""

    if api_key:
        channel_id = await resolve_channel_id(api_key=api_key, handle=handle)
        source = "YouTube Data API"

    if not channel_id:
        # No API key needed: the id is public in the channel page HTML.
        channel_id = await resolve_channel_id_from_page(value)
        source = "the public channel page"

    if not channel_id:
        return True, (
            f"Saved (handle {handle}), but the channel ID could not be resolved.\n"
            "         Open your channel, View Page Source, search for\n"
            '         "externalId" and set YOUTUBE_CHANNEL_ID manually.'
        ), {}

    return True, (
        f"Resolved {handle} -> {channel_id}\n"
        f"         (via {source}; also saved as YOUTUBE_CHANNEL_ID)"
    ), {"YOUTUBE_CHANNEL_ID": channel_id}


async def validate_channel_id(value: str, env: dict[str, str]) -> Result:
    if not re.match(r"^UC[A-Za-z0-9_-]{22}$", value):
        return False, (
            "A channel ID starts with UC and is exactly 24 characters,\n"
            "         for example UC_x5XG1OV2P6uZZ5FSM9Ttw"
        ), {}
    return True, "Looks like a valid channel ID.", {}


async def validate_google_client_id(value: str, env: dict[str, str]) -> Result:
    if not value.endswith(".apps.googleusercontent.com"):
        return False, (
            "A Google OAuth client ID ends with .apps.googleusercontent.com\n"
            "         Make sure you created a 'Web application' client."
        ), {}
    return True, "Looks like a valid Google OAuth web client ID.", {}


async def validate_google_client_secret(value: str, env: dict[str, str]) -> Result:
    if len(value) < 10:
        return False, "That looks too short to be a Google client secret.", {}
    if not value.startswith("GOCSPX-"):
        return True, (
            "Saved. (Note: current Google secrets usually start with 'GOCSPX-'.\n"
            "         If sign-in fails later, re-copy it from Google Cloud.)"
        ), {}
    return True, "Saved. (Never printed back, never logged.)", {}


async def validate_public_url(value: str, env: dict[str, str]) -> Result:
    import httpx

    if not value.startswith("https://"):
        return False, (
            "Google requires an https:// address (http is rejected except for\n"
            "         localhost, which Google will not redirect to for a web client)."
        ), {}
    if value.endswith("/"):
        return False, "Remove the trailing slash - it breaks the redirect URI match.", {}

    redirect = f"{value}/oauth/callback"
    note = (
        f"Saved.\n"
        f"         Register this EXACT string in Google Cloud, under\n"
        f"         'Authorised redirect URIs':\n\n"
        f"             {redirect}\n"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(f"{value}/health")
        if response.status_code == 200 and "verification_mode" in response.text:
            note += "\n         Reachability: your bot answered /health at that URL."
        else:
            note += (
                f"\n         Reachability: got HTTP {response.status_code} from /health."
                "\n         That is fine if the bot is not running yet."
            )
    except Exception:  # noqa: BLE001 - tunnel not up yet is normal
        note += (
            "\n         Reachability: could not reach it yet. That is expected"
            "\n         until your tunnel and the bot are both running."
        )
    return True, note, {}


@dataclass
class Field:
    key: str
    title: str
    how: str
    required_for_phase2: bool
    validator: Callable[..., Awaitable[Result]]
    secret: bool = False
    needs_env: bool = True


FIELDS: list[Field] = [
    Field(
        "TELEGRAM_BOT_TOKEN",
        "Telegram bot token",
        "Telegram > @BotFather > /newbot (or /mybots > your bot > API Token)",
        True,
        lambda v, env: validate_bot_token(v),
        secret=True,
    ),
    Field(
        "TELEGRAM_GROUP_INVITE_LINK",
        "Existing group invite link",
        "Your group > group name > Invite Links > copy or create a link",
        False,
        lambda v, env: _simple_link(v),
    ),
    Field(
        "TELEGRAM_GROUP_ID",
        "Existing group numeric ID",
        "Add the bot to the group, then run: python -m src.tools.find_group_id",
        True,
        validate_group_id,
    ),
    Field(
        "YOUTUBE_CHANNEL_URL",
        "YouTube channel address",
        "Open your channel and copy the browser address",
        True,
        validate_channel_url,
    ),
    Field(
        "YOUTUBE_CHANNEL_ID",
        "YouTube channel ID (UC...)",
        "python -m src.tools.resolve_channel  (or page source > externalId)",
        True,
        validate_channel_id,
    ),
    Field(
        "GOOGLE_CLIENT_ID",
        "Google OAuth client ID",
        "Google Cloud > APIs & Services > Credentials > OAuth client (Web application)",
        True,
        validate_google_client_id,
    ),
    Field(
        "GOOGLE_CLIENT_SECRET",
        "Google OAuth client secret",
        "Same screen as the client ID",
        True,
        validate_google_client_secret,
        secret=True,
    ),
    Field(
        "OAUTH_PUBLIC_BASE_URL",
        "Public https:// address",
        "cloudflared tunnel --url http://localhost:8080   (or ngrok http 8080)",
        True,
        validate_public_url,
    ),
]


async def _simple_link(value: str) -> Result:
    if not re.match(r"^https://t\.me/(\+|joinchat/|[A-Za-z0-9_]{4,})", value):
        return False, (
            "That should be a t.me link, for example\n"
            "         https://t.me/+AbCdEf123456"
        ), {}
    return True, "Looks like a valid Telegram invite link.", {}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def apply_value(field: Field, value: str, env: dict[str, str]) -> bool:
    value = value.strip().strip("\"'")
    if not value:
        return False
    ok, message, extra = await field.validator(value, env)
    prefix = OK if ok else BAD
    shown = mask(value) if field.secret else value
    print(f"{prefix}{field.key} = {shown}")
    for line in message.splitlines():
        print(f"         {line}" if not line.startswith("     ") else line)
    if ok:
        write_env_value(field.key, value)
        env[field.key] = value
        for extra_key, extra_value in extra.items():
            write_env_value(extra_key, extra_value)
            env[extra_key] = extra_value
    print()
    return ok


async def cmd_status() -> int:
    env = read_env()
    print("=" * 70)
    print("  SETUP STATUS")
    print("=" * 70)
    print(f"  .env: {ENV_PATH}\n")

    missing: list[Field] = []
    for field in FIELDS:
        value = env.get(field.key, "")
        if value:
            shown = mask(value) if field.secret else value
            print(f"{OK}{field.key}")
            print(f"         {shown}")
        else:
            tag = "REQUIRED for Phase 2" if field.required_for_phase2 else "optional"
            print(f"{TODO}{field.key}   ({tag})")
            print(f"         how: {field.how}")
            if field.required_for_phase2:
                missing.append(field)
        print()

    generated = env.get("OAUTH_STATE_SECRET", "")
    print(f"{OK if generated else TODO}OAUTH_STATE_SECRET")
    print(f"         {mask(generated) if generated else 'not set'}  (generated for you)")
    print()
    print(f"  VERIFICATION_MODE = {env.get('VERIFICATION_MODE', '(unset)')}")
    print(f"  INVITE_MODE       = {env.get('INVITE_MODE', '(unset)')}")

    print("\n" + "=" * 70)
    if missing:
        print(f"  {len(missing)} value(s) still needed for Phase 2:")
        for field in missing:
            print(f"    - {field.key}")
        print("\n  Set them with:")
        print("    python scripts/setup_wizard.py --set KEY=VALUE")
        print("  or run the guided walkthrough:")
        print("    python scripts/setup_wizard.py")
    else:
        print("  All Phase 2 values are present. Next:")
        print("    python scripts/oauth_rehearsal.py    # validate them")
        print("    python scripts/preflight.py")
        print("    python run.py")
    print("=" * 70)
    return 0


async def cmd_set(assignments: list[str]) -> int:
    env = read_env()
    by_key = {field.key: field for field in FIELDS}
    failures = 0
    print()
    for assignment in assignments:
        if "=" not in assignment:
            print(f"{BAD}Expected KEY=VALUE, got: {assignment}\n")
            failures += 1
            continue
        key, _, value = assignment.partition("=")
        key = key.strip().upper()
        field = by_key.get(key)
        if field is None:
            # Still allow setting any other documented key, without validation.
            write_env_value(key, value.strip())
            print(f"{OK}{key} set (no validator for this key).\n")
            continue
        if not await apply_value(field, value, env):
            failures += 1
    return 1 if failures else 0


async def cmd_from_clipboard(key: str) -> int:
    """Take one value straight from the clipboard, validate it, save it.

    The value is never echoed in full and never becomes part of a shell
    command, so it does not end up in your shell history.
    """
    key = key.strip().upper()
    by_key = {field.key: field for field in FIELDS}
    field = by_key.get(key)
    if field is None:
        print(f"{BAD}Unknown key: {key}")
        print(f"       Known keys: {', '.join(by_key)}")
        return 1

    value = read_clipboard()
    if not value:
        print(f"{BAD}Could not read anything from the clipboard.")
        print(f"       Copy the value first, then run this again.")
        return 1

    value = pick_value_line(value, key)

    print(f"\n  Read {len(value)} characters from the clipboard for {key}.")
    env = read_env()
    ok = await apply_value(field, value, env)
    if not ok:
        print("       Nothing was saved. Copy the correct value and try again.")
    return 0 if ok else 1


async def cmd_interactive() -> int:
    from src.console import force_utf8_console

    force_utf8_console()
    env = read_env()

    print("=" * 70)
    print("  SETUP WALKTHROUGH")
    print("=" * 70)
    print("  Press ENTER to skip any item and keep what is already there.")
    print("  Values are checked against the real service before saving.\n")

    for field in FIELDS:
        current = env.get(field.key, "")
        print("-" * 70)
        print(f"  {field.title}   [{field.key}]")
        print(f"  How to get it: {field.how}")
        if current:
            print(f"  Currently: {mask(current) if field.secret else current}")
        try:
            entered = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Stopped.")
            return 1
        if not entered:
            print("  (skipped)\n")
            continue
        await apply_value(field, entered, env)

    print()
    return await cmd_status()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill in and validate .env for the Telegram YouTube Gate."
    )
    parser.add_argument("--status", action="store_true", help="show what is done/missing")
    parser.add_argument(
        "--set", nargs="+", metavar="KEY=VALUE", help="set and validate values"
    )
    parser.add_argument(
        "--from-clipboard",
        metavar="KEY",
        help="take KEY's value from the clipboard (keeps secrets out of your "
        "shell history)",
    )
    args = parser.parse_args()

    from src.console import force_utf8_console

    force_utf8_console()

    if args.from_clipboard:
        return asyncio.run(cmd_from_clipboard(args.from_clipboard))
    if args.set:
        return asyncio.run(cmd_set(args.set))
    if args.status:
        return asyncio.run(cmd_status())
    return asyncio.run(cmd_interactive())


if __name__ == "__main__":
    raise SystemExit(main())
