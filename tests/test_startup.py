"""Startup / lifecycle tests.

The important one here is ``test_post_init_opens_everything``: an earlier
version of this project drove the Application by hand, which silently skipped
python-telegram-bot's ``post_init`` hook, so the web server never started and
group permissions were never checked. These tests lock that behaviour down.
"""

from __future__ import annotations

import asyncio

import pytest
from telegram.ext import (
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    MessageHandler,
)

from src.bot.app import BOT_COMMANDS, GateBot, configure_logging
from src.config import InviteMode
from tests.conftest import make_settings


@pytest.fixture
def gate_bot(tmp_path):
    return GateBot(make_settings(tmp_path))


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_build_registers_every_handler(gate_bot):
    application = gate_bot.build()
    handlers = application.handlers[0]

    commands: set[str] = set()
    kinds = {"callback": 0, "join_request": 0, "message": 0}
    for handler in handlers:
        if isinstance(handler, CommandHandler):
            commands |= set(handler.commands)
        elif isinstance(handler, CallbackQueryHandler):
            kinds["callback"] += 1
        elif isinstance(handler, ChatJoinRequestHandler):
            kinds["join_request"] += 1
        elif isinstance(handler, MessageHandler):
            kinds["message"] += 1

    assert commands == {"start", "help", "status", "privacy", "forgetme", "stats"}
    assert kinds["callback"] == 1
    assert kinds["join_request"] == 1
    assert kinds["message"] == 2  # unknown command + free text
    assert application.error_handlers


def test_build_wires_the_lifecycle_hooks(gate_bot):
    """run_polling only calls these if the builder was given them."""
    application = gate_bot.build()
    assert application.post_init is not None
    assert application.post_shutdown is not None


def test_advertised_commands_match_registered_commands(gate_bot):
    application = gate_bot.build()
    registered = {
        command
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    for command in BOT_COMMANDS:
        assert command.command in registered


def test_build_does_not_open_the_database(gate_bot):
    """Opening it here would leak a non-daemon thread when startup fails."""
    gate_bot.build()
    with pytest.raises(RuntimeError):
        _ = gate_bot.db.conn


def test_configure_logging_is_safe_to_call():
    configure_logging("DEBUG")
    configure_logging("INFO")


# ---------------------------------------------------------------------------
# post_init / post_shutdown
# ---------------------------------------------------------------------------


class _StubBot:
    def __init__(self) -> None:
        self.commands_set = None

    async def get_me(self):
        from tests.conftest import FakeUser

        return FakeUser(id=1, is_bot=True, username="MockGateBot")

    async def set_my_commands(self, commands):
        self.commands_set = commands
        return True


class _StubApplication:
    def __init__(self) -> None:
        self.bot = _StubBot()
        self.job_queue = None


async def test_post_init_opens_everything_and_post_shutdown_closes_it(tmp_path):
    settings = make_settings(tmp_path, health_check_port=0, web_host="127.0.0.1")
    bot = GateBot(settings)
    bot.build()
    application = _StubApplication()

    await bot._post_init(application)

    # Database is open and usable.
    await bot.db.touch_user(1)
    assert await bot.db.get_user(1) is not None
    # The command menu was published.
    assert application.bot.commands_set == BOT_COMMANDS
    # The web server was started (this was the silently-skipped step).
    assert bot.web is not None and bot.web.app is not None

    await bot._post_shutdown(application)

    with pytest.raises(RuntimeError):
        _ = bot.db.conn


async def test_post_shutdown_closes_the_db_even_if_web_stop_fails(tmp_path):
    settings = make_settings(tmp_path, health_check_port=0, web_host="127.0.0.1")
    bot = GateBot(settings)
    bot.build()
    application = _StubApplication()
    await bot._post_init(application)

    async def explode() -> None:
        raise RuntimeError("web server refused to stop")

    bot.web.stop = explode  # type: ignore[method-assign]
    await bot._post_shutdown(application)

    # The database was still closed - otherwise the process would hang.
    with pytest.raises(RuntimeError):
        _ = bot.db.conn


async def test_no_web_server_when_not_needed(tmp_path):
    bot = GateBot(make_settings(tmp_path))
    bot.build()
    application = _StubApplication()
    await bot._post_init(application)
    assert bot.web is not None
    assert bot.web.enabled is False
    assert bot.web.app is None  # never bound a socket
    await bot._post_shutdown(application)


async def test_post_init_warns_but_continues_on_bad_group_permissions(tmp_path, caplog):
    """A misconfigured group must not stop the bot from booting."""
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_id=-1001234567890
    )
    bot = GateBot(settings)
    bot.build()

    class _NotAdminBot(_StubBot):
        async def get_chat_member(self, chat_id, user_id):
            from tests.conftest import FakeChatMember

            return FakeChatMember(status="member", can_invite_users=False)

    application = _StubApplication()
    application.bot = _NotAdminBot()

    with caplog.at_level("WARNING"):
        await bot._post_init(application)

    assert any("GROUP SETUP" in record.message for record in caplog.records)
    await bot._post_shutdown(application)


async def test_housekeeping_purges_expired_nonces(tmp_path):
    bot = GateBot(make_settings(tmp_path))
    bot.build()
    application = _StubApplication()
    await bot._post_init(application)
    try:
        await bot.db.create_oauth_nonce("stale", 1, "v", ttl_seconds=-5)
        await bot.db.create_oauth_nonce("fresh", 1, "v", ttl_seconds=900)

        await bot._housekeeping(None)

        assert await bot.db.consume_oauth_nonce("stale") is None
        assert await bot.db.consume_oauth_nonce("fresh") is not None
    finally:
        await bot._post_shutdown(application)


def test_rate_limiter_prunes_only_idle_users():
    from src.bot.handlers import RateLimiter

    limiter = RateLimiter(max_actions=5, window=20.0)
    limiter.allow(1, now=100.0)
    limiter.allow(2, now=100.0)
    limiter.allow(2, now=115.0)  # user 2 is still active at t=115

    limiter.prune(now=118.0)  # 18s later: both still inside the window
    assert set(limiter._hits) == {1, 2}

    limiter.prune(now=130.0)  # user 1 went idle, user 2 did not
    assert set(limiter._hits) == {2}

    limiter.prune(now=200.0)  # everyone idle - the dict empties out
    assert limiter._hits == {}


def test_rate_limiter_blocks_bursts_then_recovers():
    from src.bot.handlers import RateLimiter

    limiter = RateLimiter(max_actions=3, window=10.0)
    assert [limiter.allow(7, now=0.0) for _ in range(4)] == [True, True, True, False]
    # Once the window has passed they are allowed again.
    assert limiter.allow(7, now=11.0) is True


async def test_database_thread_is_a_daemon(tmp_path):
    """A leaked connection must never stop the process from exiting."""
    import threading

    from src.database import Database

    database = Database(tmp_path / "daemon.sqlite3")
    await database.connect()
    try:
        assert isinstance(database.conn, threading.Thread)
        assert database.conn.daemon is True
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# group permission checking
# ---------------------------------------------------------------------------


async def test_permission_check_passes_for_admin_with_invite_rights(tmp_path, fake_bot):
    from src.bot.invites import check_group_permissions

    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_id=-1001234567890
    )
    assert await check_group_permissions(fake_bot, settings) == []


async def test_permission_check_flags_non_admin(tmp_path, fake_bot):
    from src.bot.invites import check_group_permissions
    from tests.conftest import FakeChatMember

    fake_bot.member = FakeChatMember(status="member", can_invite_users=False)
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_id=-1001234567890
    )
    problems = await check_group_permissions(fake_bot, settings)
    assert len(problems) == 1
    assert "administrator" in problems[0]


async def test_permission_check_flags_missing_invite_right(tmp_path, fake_bot):
    from src.bot.invites import check_group_permissions
    from tests.conftest import FakeChatMember

    fake_bot.member = FakeChatMember(status="administrator", can_invite_users=False)
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.UNIQUE, group_chat_id=-1001234567890
    )
    problems = await check_group_permissions(fake_bot, settings)
    assert "Invite Users via Link" in problems[0]


async def test_permission_check_survives_an_unparseable_response(tmp_path, fake_bot):
    """Regression: a TypeError here used to crash the bot at startup.

    python-telegram-bot raises TypeError when Telegram returns a ChatMember
    shape it does not know how to build. That is a *diagnostic* failure and
    must never stop the bot from running.
    """
    from src.bot.invites import check_group_permissions

    async def unparseable(*_args, **_kwargs):
        raise TypeError(
            "ChatMemberAdministrator.__init__() missing 3 required positional "
            "arguments: 'can_post_stories', 'can_edit_stories', and "
            "'can_delete_stories'"
        )

    fake_bot.get_chat_member = unparseable  # type: ignore[method-assign]
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_id=-1001234567890
    )

    problems = await check_group_permissions(fake_bot, settings)

    assert len(problems) == 1
    assert "will still start" in problems[0]


async def test_post_init_still_boots_when_permission_check_explodes(tmp_path):
    """The bot must come up even if the group check throws."""
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_id=-1001234567890
    )
    bot = GateBot(settings)
    bot.build()

    class _ExplodingBot(_StubBot):
        async def get_chat_member(self, chat_id, user_id):
            raise TypeError("unparseable member payload")

    application = _StubApplication()
    application.bot = _ExplodingBot()

    await bot._post_init(application)  # must not raise
    try:
        await bot.db.touch_user(1)
        assert await bot.db.get_user(1) is not None  # bot is fully operational
    finally:
        await bot._post_shutdown(application)


async def test_permission_check_skipped_in_static_mode(tmp_path, fake_bot):
    from src.bot.invites import check_group_permissions

    assert await check_group_permissions(fake_bot, make_settings(tmp_path)) == []


async def test_permission_check_reports_bot_not_in_group(tmp_path, fake_bot):
    from telegram.error import Forbidden

    from src.bot.invites import check_group_permissions

    async def forbidden(*_args, **_kwargs):
        raise Forbidden("bot is not a member of the supergroup chat")

    fake_bot.get_chat_member = forbidden  # type: ignore[method-assign]
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_id=-1001234567890
    )
    problems = await check_group_permissions(fake_bot, settings)
    assert "not a member of the group" in problems[0]


# ---------------------------------------------------------------------------
# the mock Telegram API fixture itself
# ---------------------------------------------------------------------------


async def test_mock_telegram_api_answers_startup_calls(aiohttp_client):
    from scripts.mock_telegram_api import build_app

    client = await aiohttp_client(build_app())
    token = "123:abc"

    me = await (await client.post(f"/bot{token}/getMe")).json()
    assert me["ok"] and me["result"]["username"] == "MockGateBot"

    for method in ("getUpdates", "deleteWebhook", "setMyCommands"):
        body = await (await client.post(f"/bot{token}/{method}")).json()
        assert body["ok"], method

    unknown = await client.post(f"/bot{token}/someUnknownMethod")
    assert unknown.status == 400


async def test_mock_telegram_api_supports_join_request_methods(aiohttp_client):
    from scripts.mock_telegram_api import build_app

    client = await aiohttp_client(build_app())
    token = "123:abc"

    for method in ("createChatInviteLink", "approveChatJoinRequest"):
        response = await client.post(f"/bot{token}/{method}", json={"user_id": 42})
        body = await response.json()
        assert body["ok"], method


def test_request_script_exercises_both_join_outcomes():
    """The scripted 'request' run must cover an approval AND a rejection."""
    from scripts.mock_telegram_api import PRIVATE_USER, STRANGER, main
    import scripts.mock_telegram_api as mock

    pending = [
        mock.start_command_update(1),
        mock.callback_update("gate:sub", 2),
        mock.callback_update("gate:confirm", 3),
        mock.join_request_update(PRIVATE_USER, 4),
        mock.join_request_update(STRANGER, 5),
    ]
    join_requests = [u for u in pending if "chat_join_request" in u]
    assert len(join_requests) == 2
    # One user completes the gate first; the other never appears before.
    assert join_requests[0]["chat_join_request"]["from"]["id"] == PRIVATE_USER["id"]
    assert join_requests[1]["chat_join_request"]["from"]["id"] == STRANGER["id"]
    assert PRIVATE_USER["id"] != STRANGER["id"]
