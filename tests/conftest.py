"""Shared fixtures and a small fake-Telegram harness.

The fakes let the whole conversation be driven in-process: no network, no bot
token, no real group. Every outgoing message is recorded so tests can assert
on exactly what the user would have seen.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import InviteMode, Settings, VerificationMode  # noqa: E402
from src.database import Database  # noqa: E402

VALID_TOKEN = "123456789:AAExampleTokenForTestsOnly_0123456789abc"


# ---------------------------------------------------------------------------
# settings / database
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Build a valid Settings object without touching the environment."""
    base: dict[str, Any] = dict(
        bot_token=VALID_TOKEN,
        api_base_url="https://api.telegram.org/bot",
        group_invite_links=("https://t.me/+TestInviteLink123",),
        group_chat_ids=(),
        invite_mode=InviteMode.STATIC,
        admin_user_ids=(999,),
        youtube_channel_url="https://www.youtube.com/@testchannel",
        youtube_channel_id=None,
        youtube_channel_handle="@testchannel",
        youtube_api_key="",
        verification_mode=VerificationMode.HONOR,
        min_seconds_before_confirm=0,
        google_client_id="",
        google_client_secret="",
        oauth_public_base_url="",
        oauth_state_secret="",
        web_host="127.0.0.1",
        web_port=8080,
        database_path=tmp_path / "test.sqlite3",
        log_level="INFO",
        health_check_port=None,
        community_name="the Test Crew",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# fake Telegram objects
# ---------------------------------------------------------------------------


@dataclass
class SentMessage:
    kind: str  # "send" | "edit"
    text: str
    reply_markup: Any = None
    chat_id: int | None = None

    # -- assertions helpers ------------------------------------------------

    @property
    def button_texts(self) -> list[str]:
        if self.reply_markup is None:
            return []
        return [
            button.text
            for row in self.reply_markup.inline_keyboard
            for button in row
        ]

    @property
    def button_urls(self) -> list[str]:
        if self.reply_markup is None:
            return []
        return [
            button.url
            for row in self.reply_markup.inline_keyboard
            for button in row
            if button.url
        ]

    @property
    def callback_data(self) -> list[str]:
        if self.reply_markup is None:
            return []
        return [
            button.callback_data
            for row in self.reply_markup.inline_keyboard
            for button in row
            if button.callback_data
        ]


class Recorder:
    """Collects everything the bot tried to send."""

    def __init__(self) -> None:
        self.messages: list[SentMessage] = []

    def add(self, message: SentMessage) -> None:
        self.messages.append(message)

    @property
    def last(self) -> SentMessage:
        assert self.messages, "the bot did not send anything"
        return self.messages[-1]

    @property
    def texts(self) -> list[str]:
        return [message.text for message in self.messages]

    def clear(self) -> None:
        self.messages.clear()


@dataclass
class FakeUser:
    id: int
    is_bot: bool = False
    username: str = "tester"
    first_name: str = "Test"


class FakeInviteLink:
    def __init__(self, link: str) -> None:
        self.invite_link = link


class FakeChatMember:
    def __init__(self, status: str = "administrator", can_invite_users: bool = True) -> None:
        self.status = status
        self.can_invite_users = can_invite_users


class FakeBot:
    """Stands in for telegram.Bot."""

    def __init__(self, recorder: Recorder, username: str = "TestGateBot") -> None:
        self.recorder = recorder
        self.username = username
        self.created_links: list[dict[str, Any]] = []
        self.approved: list[int] = []
        self.member = FakeChatMember()
        self.fail_create_link = False
        self.fail_approve = False

    async def get_me(self) -> FakeUser:
        return FakeUser(id=1, is_bot=True, username=self.username)

    async def get_chat_member(self, chat_id: int, user_id: int) -> FakeChatMember:
        return self.member

    async def create_chat_invite_link(self, **kwargs: Any) -> FakeInviteLink:
        if self.fail_create_link:
            from telegram.error import BadRequest

            raise BadRequest("not enough rights to manage chat invite link")
        self.created_links.append(kwargs)
        suffix = kwargs.get("name", "link")
        return FakeInviteLink(f"https://t.me/+generated_{suffix}")

    async def approve_chat_join_request(self, chat_id: int, user_id: int) -> bool:
        if self.fail_approve:
            from telegram.error import BadRequest

            raise BadRequest("USER_ALREADY_PARTICIPANT")
        self.approved.append(user_id)
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.recorder.add(
            SentMessage(
                kind="send",
                text=text,
                reply_markup=kwargs.get("reply_markup"),
                chat_id=chat_id,
            )
        )


class FakeChat:
    def __init__(self, chat_id: int, recorder: Recorder, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type
        self.title = "Test Group"
        self.recorder = recorder

    async def send_message(self, text: str, **kwargs: Any) -> None:
        self.recorder.add(
            SentMessage(
                kind="send",
                text=text,
                reply_markup=kwargs.get("reply_markup"),
                chat_id=self.id,
            )
        )


class FakeMessage:
    def __init__(self, chat: FakeChat) -> None:
        self.chat = chat
        self.message_id = 1


class FakeCallbackQuery:
    def __init__(self, data: str, user: FakeUser, chat: FakeChat, recorder: Recorder) -> None:
        self.data = data
        self.from_user = user
        self.message = FakeMessage(chat)
        self.recorder = recorder
        self.answers: list[str | None] = []
        #: set to an exception class to simulate a failing edit
        self.edit_error: Exception | None = None

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        if self.edit_error is not None:
            error = self.edit_error
            self.edit_error = None
            raise error
        self.recorder.add(
            SentMessage(
                kind="edit",
                text=text,
                reply_markup=kwargs.get("reply_markup"),
                chat_id=self.message.chat.id,
            )
        )


class FakeJoinRequest:
    def __init__(self, user: FakeUser, chat: FakeChat) -> None:
        self.from_user = user
        self.chat = chat


class FakeUpdate:
    """Quacks like telegram.Update for the attributes our handlers touch."""

    def __init__(
        self,
        *,
        user: FakeUser,
        chat: FakeChat,
        bot: FakeBot,
        callback_query: FakeCallbackQuery | None = None,
        chat_join_request: FakeJoinRequest | None = None,
    ) -> None:
        self.effective_user = user
        self.effective_chat = chat
        self.callback_query = callback_query
        self.chat_join_request = chat_join_request
        self._bot = bot

    def get_bot(self) -> FakeBot:
        return self._bot


@dataclass
class FakeContext:
    bot: FakeBot
    args: list[str] = field(default_factory=list)
    error: BaseException | None = None


# ---------------------------------------------------------------------------
# a ready-made conversation harness
# ---------------------------------------------------------------------------


class Conversation:
    """Drives one user through the bot and records what they'd see."""

    def __init__(self, handlers, bot: FakeBot, recorder: Recorder, user_id: int = 42) -> None:
        self.handlers = handlers
        self.bot = bot
        self.recorder = recorder
        self.user = FakeUser(id=user_id)
        self.chat = FakeChat(user_id, recorder)

    def _update(self, **kwargs: Any) -> FakeUpdate:
        return FakeUpdate(user=self.user, chat=self.chat, bot=self.bot, **kwargs)

    async def send_command(self, name: str, args: list[str] | None = None) -> SentMessage:
        method = {
            "start": self.handlers.start,
            "help": self.handlers.help_command,
            "status": self.handlers.status,
            "privacy": self.handlers.privacy,
            "stats": self.handlers.stats,
            "forgetme": self.handlers.forget_me,
        }[name]
        await method(self._update(), FakeContext(bot=self.bot, args=args or []))
        return self.recorder.last

    async def tap(self, callback_data: str) -> SentMessage:
        query = FakeCallbackQuery(callback_data, self.user, self.chat, self.recorder)
        update = self._update(callback_query=query)
        await self.handlers.on_callback(update, FakeContext(bot=self.bot))
        return self.recorder.last

    async def say(self, _text: str = "hello") -> SentMessage:
        await self.handlers.on_text(self._update(), FakeContext(bot=self.bot))
        return self.recorder.last

    async def unknown_command(self) -> SentMessage:
        await self.handlers.on_unknown_command(
            self._update(), FakeContext(bot=self.bot)
        )
        return self.recorder.last

    async def request_join(self, group_chat_id: int = -1001234567890) -> None:
        group = FakeChat(group_chat_id, self.recorder, chat_type="supergroup")
        update = self._update(chat_join_request=FakeJoinRequest(self.user, group))
        await self.handlers.on_join_request(update, FakeContext(bot=self.bot))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def fake_bot(recorder: Recorder) -> FakeBot:
    return FakeBot(recorder)


@pytest.fixture
async def gate(settings, db, recorder, fake_bot):
    """A GateHandlers wired with the honour verifier + a Conversation driver."""
    from src.bot.handlers import GateHandlers
    from src.bot.invites import InviteService
    from src.verification import build_verifier

    verifier = build_verifier(settings, db)
    handlers = GateHandlers(settings, db, verifier, InviteService(settings, db))
    return Conversation(handlers, fake_bot, recorder)


def build_gate(settings, database, recorder, bot, user_id: int = 42) -> Conversation:
    """Same as the ``gate`` fixture but for custom settings inside a test."""
    from src.bot.handlers import GateHandlers
    from src.bot.invites import InviteService
    from src.verification import build_verifier

    verifier = build_verifier(settings, database)
    handlers = GateHandlers(settings, database, verifier, InviteService(settings, database))
    return Conversation(handlers, bot, recorder, user_id=user_id)
