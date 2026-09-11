"""Handing verified users into the EXISTING Telegram group.

Nothing in this module creates, renames, or modifies a group, and nothing
touches the current members. It only ever:

* reads a pre-made invite link out of the config (``static``), or
* asks Telegram to mint an ADDITIONAL invite link (``unique`` / ``request``),
* approves a pending join request (``request``).

Existing members and existing invite links are never revoked or altered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from src.config import InviteMode, Settings
from src.database import Database
from src.database.db import utcnow

logger = logging.getLogger(__name__)

#: Cached join-request link, so we do not mint a new one on every restart.
KV_JOIN_REQUEST_LINK = "join_request_invite_link"

#: How long a personal single-use link stays valid.
UNIQUE_LINK_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class InviteResult:
    ok: bool
    link: str | None = None
    #: True when the link is personal/single-use (changes the wording used).
    is_personal: bool = False
    #: True when tapping the link creates a pending join request.
    is_join_request: bool = False
    error: str = ""


class InviteService:
    """Produces the right kind of invite link for the configured mode."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.db = database

    async def get_invite(self, bot: Bot, telegram_user_id: int) -> InviteResult:
        mode = self.settings.invite_mode
        if mode is InviteMode.STATIC:
            return self._static_invite()
        if mode is InviteMode.UNIQUE:
            return await self._unique_invite(bot, telegram_user_id)
        return await self._join_request_invite(bot)

    # -- static ----------------------------------------------------------

    def _static_invite(self) -> InviteResult:
        link = self.settings.group_invite_link
        if not link:
            return InviteResult(ok=False, error="no_link_configured")
        return InviteResult(ok=True, link=link)

    # -- unique / single-use --------------------------------------------

    async def _unique_invite(self, bot: Bot, telegram_user_id: int) -> InviteResult:
        # Reuse the link we already made for this user, if it is still fresh.
        record = await self.db.get_user(telegram_user_id)
        if record and record.invite_link and record.invite_sent_at:
            if utcnow() - record.invite_sent_at < UNIQUE_LINK_TTL:
                return InviteResult(ok=True, link=record.invite_link, is_personal=True)

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=self.settings.group_chat_id,
                name=f"gate-{telegram_user_id}"[:32],
                member_limit=1,
                expire_date=utcnow() + UNIQUE_LINK_TTL,
            )
        except (BadRequest, Forbidden) as exc:
            logger.error(
                "Cannot create a single-use invite link: %s. Is the bot an admin "
                "of the group with 'Invite Users via Link'?",
                exc,
            )
            return self._fallback(str(exc))
        except TelegramError as exc:  # pragma: no cover - network shapes
            logger.error("Telegram error creating invite link: %s", exc)
            return self._fallback(str(exc))

        return InviteResult(ok=True, link=invite.invite_link, is_personal=True)

    # -- join request ----------------------------------------------------

    async def _join_request_invite(self, bot: Bot) -> InviteResult:
        cached = await self.db.kv_get(KV_JOIN_REQUEST_LINK)
        if cached:
            return InviteResult(ok=True, link=cached, is_join_request=True)

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=self.settings.group_chat_id,
                name="YouTube gate",
                creates_join_request=True,
            )
        except (BadRequest, Forbidden) as exc:
            logger.error(
                "Cannot create a join-request invite link: %s. Is the bot an admin "
                "of the group with 'Invite Users via Link'?",
                exc,
            )
            return self._fallback(str(exc))
        except TelegramError as exc:  # pragma: no cover - network shapes
            logger.error("Telegram error creating join-request link: %s", exc)
            return self._fallback(str(exc))

        await self.db.kv_set(KV_JOIN_REQUEST_LINK, invite.invite_link)
        return InviteResult(ok=True, link=invite.invite_link, is_join_request=True)

    # -- shared ----------------------------------------------------------

    def _fallback(self, error: str) -> InviteResult:
        """If minting a link fails, fall back to the static link if we have one."""
        if self.settings.group_invite_link:
            logger.warning("Falling back to the static invite link from .env")
            return InviteResult(ok=True, link=self.settings.group_invite_link)
        return InviteResult(ok=False, error=error)

    async def approve_join_request(self, bot: Bot, telegram_user_id: int) -> bool:
        """Approve a pending join request for a verified user."""
        if self.settings.group_chat_id is None:
            logger.warning(
                "Refusing to approve a join request: TELEGRAM_GROUP_ID is not set."
            )
            return False
        try:
            await bot.approve_chat_join_request(
                chat_id=self.settings.group_chat_id, user_id=telegram_user_id
            )
        except BadRequest as exc:
            # "USER_ALREADY_PARTICIPANT" / "HIDE_REQUESTER_MISSING" are benign.
            logger.info("Could not approve join request for %s: %s", telegram_user_id, exc)
            return False
        except TelegramError as exc:  # pragma: no cover - network shapes
            logger.error("Telegram error approving join request: %s", exc)
            return False
        await self.db.mark_joined(telegram_user_id)
        logger.info("Approved join request for verified user %s", telegram_user_id)
        return True


async def check_group_permissions(bot: Bot, settings: Settings) -> list[str]:
    """Return a list of human-readable problems with the bot's group setup.

    An empty list means everything needed for the configured INVITE_MODE is in
    place. Used at startup and by ``scripts/preflight.py``.
    """
    problems: list[str] = []
    if not settings.needs_group_admin:
        return problems
    if settings.group_chat_id is None:
        return ["TELEGRAM_GROUP_ID is not set."]

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(settings.group_chat_id, me.id)
    except Forbidden:
        return [
            "The bot is not a member of the group (Telegram said 'Forbidden'). "
            "Add the bot to your group first."
        ]
    except BadRequest as exc:
        return [
            f"Telegram rejected the group lookup: {exc}. Double-check "
            f"TELEGRAM_GROUP_ID={settings.group_chat_id} - it should look like "
            "-1001234567890."
        ]
    except TelegramError as exc:  # pragma: no cover - network shapes
        return [f"Could not talk to Telegram: {exc}"]
    except Exception as exc:  # noqa: BLE001
        # This is only a diagnostic. If Telegram returns a shape this library
        # version cannot parse, say so and let the bot start anyway - refusing
        # to boot over a failed *check* would be far worse than the check
        # being unavailable.
        logger.warning("Group permission check could not run: %r", exc)
        return [
            "Could not verify the bot's group permissions "
            f"({type(exc).__name__}). The bot will still start; if invites "
            "fail, check that it is an admin with 'Invite Users via Link'."
        ]

    if member.status != "administrator":
        problems.append(
            f"The bot is in the group but its status is '{member.status}', not "
            "'administrator'. INVITE_MODE="
            f"{settings.invite_mode.value} needs the bot to be an admin."
        )
        return problems

    if not getattr(member, "can_invite_users", False):
        problems.append(
            "The bot is an admin but the 'Invite Users via Link' permission is "
            "OFF. Turn it on in Group > Administrators > your bot."
        )
    return problems
