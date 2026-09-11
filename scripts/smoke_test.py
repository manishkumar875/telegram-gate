#!/usr/bin/env python3
"""Prove the bot really works - without a Telegram account or a real token.

    python scripts/smoke_test.py

What it does:
  1. Starts a fake api.telegram.org on a spare local port.
  2. Launches the real bot (run.py) pointed at that fake server, using a
     throwaway database and throwaway settings. Your own .env and your real
     data are not touched.
  3. Feeds the bot a scripted conversation:
         /start  ->  tap "I've Subscribed"  ->  tap "I've Subscribed" again
  4. Captures every message the bot tried to send and checks that the user
     would have seen the right thing, in the right order, with the right
     buttons - and that the group link is never handed out too early.

Exit code 0 means the whole user journey works on this machine.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

#: Launching in its own process group is what makes a graceful stop possible
#: on Windows: CTRL_BREAK_EVENT is delivered to a whole group, so the bot must
#: not share ours (or we would interrupt this script too).
NEW_GROUP_FLAGS = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {}


def stop_gracefully(process: subprocess.Popen, timeout: float = 20.0) -> None:
    """Ask a child to stop the way Ctrl+C would, then escalate if it refuses.

    ``Popen.terminate()`` on Windows is TerminateProcess - an unblockable
    kill that gives the program no chance to run its shutdown code. To see
    whether the bot shuts down cleanly we have to send a real console
    interrupt instead.
    """
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
    except (OSError, ValueError):
        process.terminate()

    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FAKE_TOKEN = "123456789:AAFakeTokenUsedOnlyByTheSmokeTest_abcd"
GROUP_LINK = "https://t.me/+SmokeTestInviteLink"
YOUTUBE_URL = "https://www.youtube.com/@smoketestchannel"

PASS = "  [PASS]"
FAIL = "  [FAIL]"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def outgoing_messages(log_path: Path) -> list[dict]:
    """Parse the messages the bot sent out of the mock server's log."""
    messages: list[dict] = []
    if not log_path.exists():
        return messages
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        marker = "OUTGOING "
        if marker not in line:
            continue
        try:
            payload = json.loads(line.split(marker, 1)[1])
        except json.JSONDecodeError:
            continue
        if payload.get("text"):
            messages.append(payload)
    return messages


def buttons(message: dict) -> list[tuple[str, str]]:
    markup = message.get("reply_markup")
    if not markup:
        return []
    if isinstance(markup, str):
        markup = json.loads(markup)
    return [
        (button["text"], button.get("url") or f"callback:{button.get('callback_data')}")
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


class Checks:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, condition: bool, description: str) -> None:
        if condition:
            print(f"{PASS} {description}")
        else:
            self.failures += 1
            print(f"{FAIL} {description}")


def main() -> int:
    from src.console import force_utf8_console

    force_utf8_console()

    print("=" * 66)
    print("  SMOKE TEST - full user journey against a fake Telegram")
    print("=" * 66)

    api_port = free_port()
    python = sys.executable
    temp_dir = Path(tempfile.mkdtemp(prefix="gate-smoke-"))
    mock_log = temp_dir / "mock.log"
    bot_log = temp_dir / "bot.log"

    mock_process = None
    bot_process = None
    checks = Checks()

    try:
        # 1. fake Telegram --------------------------------------------------
        print(f"\n  Starting fake Telegram API on port {api_port} ...")
        with mock_log.open("w", encoding="utf-8") as log_file:
            mock_process = subprocess.Popen(
                [python, "-u", str(PROJECT_ROOT / "scripts" / "mock_telegram_api.py"),
                 str(api_port), "full"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
            )
            if not wait_for_port(api_port):
                print(f"{FAIL} the fake Telegram server did not start")
                return 1
            print(f"{PASS} fake Telegram server is up")

            # 2. the real bot ------------------------------------------------
            env = os.environ.copy()
            env.update(
                {
                    # These win over any real .env, because load_dotenv() does
                    # not override variables already present in the process.
                    "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
                    "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{api_port}/bot",
                    "TELEGRAM_GROUP_INVITE_LINK": GROUP_LINK,
                    "YOUTUBE_CHANNEL_URL": YOUTUBE_URL,
                    "COMMUNITY_NAME": "the Smoke Test Crew",
                    "INVITE_MODE": "static",
                    "VERIFICATION_MODE": "honor",
                    "MIN_SECONDS_BEFORE_CONFIRM": "0",
                    "DATABASE_PATH": str(temp_dir / "smoke.sqlite3"),
                    "HEALTH_CHECK_PORT": "",
                    "LOG_LEVEL": "INFO",
                    "PYTHONIOENCODING": "utf-8",
                }
            )

            print("  Launching the real bot (run.py) ...")
            with bot_log.open("w", encoding="utf-8") as bot_log_file:
                bot_process = subprocess.Popen(
                    [python, "-u", str(PROJECT_ROOT / "run.py")],
                    stdout=bot_log_file,
                    stderr=subprocess.STDOUT,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    **NEW_GROUP_FLAGS,
                )

                # 3. let the scripted conversation play out -------------------
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if len(outgoing_messages(mock_log)) >= 3:
                        break
                    if bot_process.poll() is not None:
                        break
                    time.sleep(0.5)
                time.sleep(1.0)
    finally:
        # Interrupt the bot the way a human pressing Ctrl+C would, so we can
        # observe whether it releases its resources properly.
        if bot_process is not None:
            stop_gracefully(bot_process)
        if mock_process is not None and mock_process.poll() is None:
            mock_process.terminate()
            try:
                mock_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                mock_process.kill()

    # 4. assertions ---------------------------------------------------------
    bot_output = bot_log.read_text(encoding="utf-8", errors="replace")
    messages = outgoing_messages(mock_log)

    print("\n--- Startup ---")
    checks.check("Connected to Telegram as" in bot_output, "bot connected to Telegram")
    checks.check("Bot is running" in bot_output, "bot reached the running state")
    checks.check("Traceback" not in bot_output, "no unhandled exception during startup")
    checks.check(
        "VERIFICATION_MODE=honor" in bot_output,
        "honour-mode warning was logged for the operator",
    )

    print("\n--- Conversation ---")
    checks.check(len(messages) >= 3, f"bot sent all 3 messages (got {len(messages)})")
    if len(messages) < 3:
        print("\n  Bot log:\n" + bot_output[-3000:])
        return 1

    welcome, disclosure, success = messages[0], messages[1], messages[2]

    welcome_buttons = buttons(welcome)
    checks.check("Welcome" in welcome["text"], "message 1 welcomes the user")
    checks.check(
        "the Smoke Test Crew" in welcome["text"],
        "message 1 uses the configured community name",
    )
    checks.check(
        any("youtube.com" in url for _, url in welcome_buttons),
        "message 1 has a real YouTube link button",
    )
    checks.check(
        any("sub_confirmation=1" in url for _, url in welcome_buttons),
        "the YouTube link opens the Subscribe dialog directly",
    )
    checks.check(
        any(text.startswith("✅") for text, _ in welcome_buttons),
        "message 1 has an 'I've Subscribed' button",
    )
    checks.check(
        GROUP_LINK not in json.dumps(welcome),
        "message 1 does NOT leak the group link",
    )

    checks.check(
        "honour system" in disclosure["text"],
        "message 2 discloses that this is the honour system",
    )
    checks.check(
        "not</b> technically" in disclosure["text"],
        "message 2 states plainly that nothing was technically checked",
    )
    checks.check(
        GROUP_LINK not in json.dumps(disclosure),
        "message 2 does NOT leak the group link",
    )

    success_buttons = buttons(success)
    checks.check("Thanks" in success["text"], "message 3 thanks the user")
    checks.check(
        any(url == GROUP_LINK for _, url in success_buttons),
        "message 3 finally hands over the group invite link",
    )
    checks.check(
        any("JOIN" in text.upper() for text, _ in success_buttons),
        "message 3 has a clear JOIN button",
    )

    print("\n--- Shutdown ---")
    checks.check("Shutdown complete" in bot_output, "bot shut down cleanly")

    print("\n" + "=" * 66)
    if checks.failures:
        print(f"  RESULT: {checks.failures} check(s) FAILED")
        print("=" * 66)
        print("\n  Bot log tail:\n" + bot_output[-2000:])
        return 1
    print("  RESULT: the complete user journey works on this machine.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
