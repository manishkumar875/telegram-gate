"""All Telegram update handlers.

Input-safety rules followed here
--------------------------------
* Callback data is parsed through a strict allow-list (``Action.parse``);
  anything unrecognised is answered politely and dropped.
* Nothing the user types is ever echoed back into a message, so there is no
  HTML-injection surface. Values that *are* interpolated (the community name
  from .env) are HTML-escaped anyway.
* Every user is rate-limited in memory.
* Every handler tolerates a missing ``effective_user`` / ``effective_message``.
"""

from __future__ import annotations

import html
import logging
import time
from collections import defaultdict, deque
from typing import Deque

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from src.config import InviteMode, Settings
from src.database import Database, UserRecord
from src.verification import VerificationOutcome, Verifier
from src.verification.honor import HonorVerifier

from . import copy
from .invites import InviteService
from .keyboards import (
    Action,
    confirm_keyboard,
    gate_keyboard,
    join_keyboard,
    oauth_keyboard,
    restart_keyboard,
    retry_keyboard,
)

logger = logging.getLogger(__name__)

#: Max button/command actions accepted per user per window.
RATE_LIMIT_MAX_ACTIONS = 12
RATE_LIMIT_WINDOW_SECONDS = 20.0


def esc(text: str) -> str:
    """Escape text before putting it inside an HTML-parsed message."""
    return html.escape(str(text), quote=False)


class RateLimiter:
    """Tiny in-memory sliding-window limiter. No dependencies, no storage."""

    def __init__(
        self,
        max_actions: int = RATE_LIMIT_MAX_ACTIONS,
        window: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self.max_actions = max_actions
        self.window = window
        self._hits: dict[int, Deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[user_id]
        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.max_actions:
            return False
        hits.append(now)
        return True

    def prune(self, *, now: float | None = None) -> None:
        """Drop idle users so the dict cannot grow without bound."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        for user_id in list(self._hits):
            hits = self._hits[user_id]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if not hits:
                del self._hits[user_id]


class GateHandlers:
    """Bundles the dependencies every handler needs."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        verifier: Verifier,
        invites: InviteService,
    ) -> None:
        self.settings = settings
        self.db = database
        self.verifier = verifier
        self.invites = invites
        self.limiter = RateLimiter()

    # ------------------------------------------------------------------
    # text helpers
    # ------------------------------------------------------------------

    @property
    def community(self) -> str:
        return esc(self.settings.community_name)

    @property
    def channel_label(self) -> str:
        return esc(
            self.settings.youtube_channel_handle
            or self.settings.community_name
            or "our channel"
        )

    def welcome_text(self) -> str:
        return copy.WELCOME.format(community_name=self.community)

    # ------------------------------------------------------------------
    # low-level send/edit helpers (these are what stop repeated messages)
    # ------------------------------------------------------------------

    async def _edit_or_send(
        self,
        update: Update,
        text: str,
        reply_markup=None,
        *,
        prefer_edit: bool = True,
    ) -> None:
        """Edit the message the button lives on when we can; otherwise send.

        Editing in place is what keeps the chat from filling up with near
        identical copies of the same prompt.
        """
        query = update.callback_query
        if prefer_edit and query is not None and query.message is not None:
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                # "Message is not modified" means the user tapped a button that
                # would produce identical content. That is a no-op, not a bug.
                if "not modified" in str(exc).lower():
                    return
                logger.debug("Falling back from edit to send: %s", exc)
            except TelegramError as exc:  # pragma: no cover - network shapes
                logger.debug("Edit failed (%s); sending a new message", exc)

        chat = update.effective_chat
        if chat is None:
            return
        try:
            await chat.send_message(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            logger.info("User %s has blocked the bot", getattr(update.effective_user, "id", "?"))
        except TelegramError as exc:  # pragma: no cover - network shapes
            logger.error("Could not send message: %s", exc)

    async def _guard(self, update: Update) -> int | None:
        """Common entry checks. Returns the user id, or ``None`` to stop."""
        user = update.effective_user
        if user is None or user.is_bot:
            return None
        if not self.limiter.allow(user.id):
            if update.callback_query is not None:
                try:
                    await update.callback_query.answer(
                        copy.RATE_LIMITED, show_alert=False
                    )
                except TelegramError:  # pragma: no cover
                    pass
            logger.info("Rate-limited user %s", user.id)
            return None
        return user.id

    # ------------------------------------------------------------------
    # /start
    # ------------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        record = await self.db.touch_user(user_id)

        # Deep-link payloads, e.g. t.me/YourBot?start=verified after OAuth.
        payload = (context.args[0].strip().lower() if context.args else "")
        if payload not in {"", "verified", "start", "gate"}:
            logger.debug("Ignoring unknown /start payload from %s", user_id)

        if record.is_verified:
            await self.deliver_invite(update, user_id, already_verified=True)
            return

        await self._edit_or_send(
            update,
            self.welcome_text(),
            gate_keyboard(self.settings.subscribe_url),
            prefer_edit=False,
        )

    # ------------------------------------------------------------------
    # /help  /status  /privacy  /forgetme
    # ------------------------------------------------------------------

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        await self._send_help(update)

    async def _send_help(self, update: Update) -> None:
        text = copy.HELP.format(
            community_name=self.community,
            support_line=copy.SUPPORT_LINE_DEFAULT,
        )
        await self._edit_or_send(update, text, restart_keyboard())

    async def privacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        line = (
            copy.PRIVACY_OAUTH_LINE
            if self.verifier.provides_real_verification
            else copy.PRIVACY_NO_OAUTH_LINE
        )
        await self._edit_or_send(
            update, copy.PRIVACY.format(oauth_privacy_line=line), None, prefer_edit=False
        )

    async def forget_me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        await self.db.delete_user(user_id)
        logger.info("Erased all stored data for user %s on request", user_id)
        await self._edit_or_send(update, copy.FORGOTTEN, None, prefer_edit=False)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        record = await self.db.get_user(user_id)
        await self._edit_or_send(
            update, self._status_text(record), restart_keyboard(), prefer_edit=False
        )

    def _status_text(self, record: UserRecord | None) -> str:
        def tick(done: bool) -> str:
            return "✅" if done else "⬜"

        started = record is not None
        reached_confirm = bool(record and record.clicked_subscribe_at)
        confirmed = bool(record and record.is_verified)
        invited = bool(record and record.has_invite)

        method = (record.verification_method if record else None) or ""
        if confirmed:
            if method == "youtube_oauth":
                confirm_label = "Subscription verified with YouTube"
            else:
                confirm_label = "Subscription confirmed (honour system)"
        else:
            confirm_label = "Subscription confirmed"

        steps = "\n".join(
            [
                f"{tick(started)} Started the bot",
                f"{tick(reached_confirm)} Reached the confirmation step",
                f"{tick(confirmed)} {confirm_label}",
                f"{tick(invited)} Received the group link",
            ]
        )

        if not started:
            next_step = copy.STATUS_NEXT_START
        elif invited:
            next_step = copy.STATUS_NEXT_DONE
        elif reached_confirm:
            next_step = copy.STATUS_NEXT_CONFIRM
        else:
            next_step = copy.STATUS_NEXT_SUBSCRIBE

        return copy.STATUS_TEMPLATE.format(steps=steps, next_step=next_step)

    # ------------------------------------------------------------------
    # /stats (owner only)
    # ------------------------------------------------------------------

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        if not self.settings.is_admin(user_id):
            await self._edit_or_send(update, copy.ADMIN_ONLY, None, prefer_edit=False)
            return
        data = await self.db.stats()
        total = data.get("total_users", 0) or 0
        invites = data.get("invites_sent", 0) or 0
        conversion = round((invites / total) * 100, 1) if total else 0.0
        await self._edit_or_send(
            update,
            copy.STATS_TEMPLATE.format(conversion=conversion, **data),
            None,
            prefer_edit=False,
        )

    # ------------------------------------------------------------------
    # button presses
    # ------------------------------------------------------------------

    async def on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None:
            return

        action = Action.parse(query.data)
        if action is None:
            # Unknown/stale/crafted payload - acknowledge so the spinner stops.
            logger.info("Ignoring unrecognised callback data from %s", query.from_user.id)
            try:
                await query.answer()
            except TelegramError:  # pragma: no cover
                pass
            return

        user_id = await self._guard(update)
        if user_id is None:
            return
        try:
            await query.answer()
        except TelegramError:  # pragma: no cover - stale query ids are common
            pass

        await self.db.touch_user(user_id)

        if action is Action.HELP:
            await self._send_help(update)
        elif action is Action.RESTART:
            await self._edit_or_send(
                update, self.welcome_text(), gate_keyboard(self.settings.subscribe_url)
            )
        elif action is Action.SUBSCRIBE_CLICKED:
            await self._handle_first_confirm(update, user_id)
        elif action in {Action.CONFIRM, Action.RECHECK}:
            await self._run_verification(update, user_id)

    async def _handle_first_confirm(self, update: Update, user_id: int) -> None:
        """First tap of "I've Subscribed".

        With the honour-system verifier this is where we tell the truth about
        what is (not) being checked, then ask for one more deliberate tap.
        With real verification we go straight to the Google flow.
        """
        await self.db.mark_subscribe_clicked(user_id)

        if self.verifier.provides_real_verification:
            await self._run_verification(update, user_id)
            return

        await self._edit_or_send(
            update,
            copy.HONOR_DISCLOSURE,
            confirm_keyboard(self.settings.subscribe_url),
        )

    async def _run_verification(self, update: Update, user_id: int) -> None:
        attempts = await self.db.increment_confirm_attempts(user_id)
        if attempts > 60:  # absurd volume: stop doing work for this user
            logger.warning("User %s exceeded the confirmation attempt cap", user_id)
            await self._edit_or_send(update, copy.RATE_LIMITED, restart_keyboard())
            return

        result = await self.verifier.verify(user_id)

        if result.outcome is VerificationOutcome.VERIFIED:
            if not result.was_technically_verified:
                await self.db.mark_confirmed(user_id, result.method)
            await self.deliver_invite(update, user_id)
            return

        if result.outcome is VerificationOutcome.NEEDS_USER_ACTION:
            if result.action_url:
                await self._edit_or_send(
                    update,
                    copy.OAUTH_INTRO.format(channel=self.channel_label),
                    oauth_keyboard(result.action_url),
                )
            else:
                # Honour mode: they were too fast, ask them to wait a moment.
                record = await self.db.get_user(user_id)
                remaining = 5
                if isinstance(self.verifier, HonorVerifier):
                    remaining = max(
                        1,
                        self.verifier.remaining_seconds(
                            record.first_seen_at if record else None
                        ),
                    )
                await self._edit_or_send(
                    update,
                    copy.TOO_FAST.format(seconds=remaining),
                    confirm_keyboard(self.settings.subscribe_url),
                )
            return

        if result.outcome is VerificationOutcome.NOT_SUBSCRIBED:
            await self._edit_or_send(
                update,
                copy.OAUTH_NOT_SUBSCRIBED.format(channel=self.channel_label),
                retry_keyboard(self.settings.subscribe_url),
            )
            return

        logger.warning("Verification error for %s: %s", user_id, result.detail)
        await self._edit_or_send(
            update, copy.OAUTH_FAILED, retry_keyboard(self.settings.subscribe_url)
        )

    # ------------------------------------------------------------------
    # delivering the group link
    # ------------------------------------------------------------------

    async def deliver_invite(
        self, update: Update, user_id: int, *, already_verified: bool = False
    ) -> None:
        bot = update.get_bot()
        record = await self.db.get_user(user_id)

        if record and record.joined_at:
            await self._edit_or_send(update, copy.ALREADY_IN_GROUP, None)
            return

        invite = await self.invites.get_invite(bot, user_id)
        if not invite.ok or not invite.link:
            message = (
                copy.NO_LINK_CONFIGURED
                if invite.error == "no_link_configured"
                else copy.INVITE_FAILED
            )
            logger.error("Could not produce an invite for %s: %s", user_id, invite.error)
            await self._edit_or_send(update, message, restart_keyboard())
            return

        await self.db.mark_invite_sent(user_id, invite.link)

        if already_verified:
            text = copy.ALREADY_VERIFIED
        elif invite.is_join_request:
            text = copy.SUCCESS_JOIN_REQUEST
        elif invite.is_personal:
            text = copy.SUCCESS_UNIQUE_LINK
        else:
            text = copy.SUCCESS

        await self._edit_or_send(update, text, join_keyboard(invite.link))

    async def notify_verified(self, bot, user_id: int) -> None:
        """Push a message to the user after an out-of-band (OAuth) success.

        Called by the web server once Google redirects back, so the user sees
        the group link in Telegram without tapping anything else.
        """
        try:
            await bot.send_message(
                chat_id=user_id,
                text=copy.OAUTH_SUCCESS.format(channel=self.channel_label),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            logger.info("Cannot notify %s - they blocked the bot", user_id)
            return
        except TelegramError as exc:  # pragma: no cover
            logger.error("Could not notify %s: %s", user_id, exc)
            return

        invite = await self.invites.get_invite(bot, user_id)
        if not invite.ok or not invite.link:
            await bot.send_message(chat_id=user_id, text=copy.INVITE_FAILED)
            return
        await self.db.mark_invite_sent(user_id, invite.link)

        if invite.is_join_request:
            text = copy.SUCCESS_JOIN_REQUEST
        elif invite.is_personal:
            text = copy.SUCCESS_UNIQUE_LINK
        else:
            text = copy.SUCCESS
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=join_keyboard(invite.link),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:  # pragma: no cover
            logger.error("Could not deliver invite to %s: %s", user_id, exc)

    async def notify_failed(self, bot, user_id: int, outcome: VerificationOutcome) -> None:
        """Tell the user an OAuth attempt did not succeed."""
        if outcome is VerificationOutcome.NOT_SUBSCRIBED:
            text = copy.OAUTH_NOT_SUBSCRIBED.format(channel=self.channel_label)
        else:
            text = copy.OAUTH_FAILED
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=retry_keyboard(self.settings.subscribe_url),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:  # pragma: no cover
            logger.info("Could not notify %s about the failed check: %s", user_id, exc)

    # ------------------------------------------------------------------
    # join requests (INVITE_MODE=request)
    # ------------------------------------------------------------------

    async def on_join_request(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        request = update.chat_join_request
        if request is None:
            return
        user_id = request.from_user.id

        # Only ever act on join requests for the one group we are configured
        # for. Without a configured group id we cannot verify which chat this
        # is, so we do nothing rather than approve someone into a chat the
        # owner never told us about.
        if self.settings.group_chat_id is None:
            logger.info(
                "Ignoring a join request from chat %s: TELEGRAM_GROUP_ID is not set, "
                "so this bot cannot confirm which group it should be gating.",
                request.chat.id,
            )
            return
        if request.chat.id != self.settings.group_chat_id:
            logger.info("Ignoring join request for an unrelated chat %s", request.chat.id)
            return

        record = await self.db.get_user(user_id)
        if record and record.is_verified:
            approved = await self.invites.approve_join_request(context.bot, user_id)
            if approved:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=copy.JOIN_REQUEST_APPROVED.format(
                            community_name=self.community
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass  # they may not have started the bot; approval still landed
            return

        # Not verified: leave the request pending and nudge them through the gate.
        logger.info("Join request from unverified user %s left pending", user_id)
        await self.db.touch_user(user_id)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=copy.JOIN_REQUEST_PENDING,
                parse_mode=ParseMode.HTML,
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=self.welcome_text(),
                reply_markup=gate_keyboard(self.settings.subscribe_url),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.info(
                "Could not DM %s about their pending join request (%s). They must "
                "start the bot first.",
                user_id,
                exc,
            )

    # ------------------------------------------------------------------
    # fallbacks
    # ------------------------------------------------------------------

    async def on_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        await self._edit_or_send(
            update, copy.UNKNOWN_COMMAND, restart_keyboard(), prefer_edit=False
        )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = await self._guard(update)
        if user_id is None:
            return
        record = await self.db.get_user(user_id)
        if record and record.is_verified and record.has_invite:
            # Don't nag verified people; just re-offer the link once.
            await self.deliver_invite(update, user_id, already_verified=True)
            return
        await self._edit_or_send(
            update, copy.UNKNOWN_TEXT, gate_keyboard(self.settings.subscribe_url),
            prefer_edit=False,
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled error while processing an update", exc_info=context.error)
        if not isinstance(update, Update):
            return
        chat = update.effective_chat
        if chat is None:
            return
        try:
            await chat.send_message(copy.GENERIC_ERROR)
        except TelegramError:  # pragma: no cover
            pass
