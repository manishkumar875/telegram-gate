"""Inline keyboards.

Callback data is a short, fixed vocabulary (see :class:`Action`). The handler
layer rejects anything that is not one of these exact strings, so a crafted
callback payload cannot steer the bot.
"""

from __future__ import annotations

from enum import Enum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import copy


class Action(str, Enum):
    """The complete set of allowed callback_data values."""

    SUBSCRIBE_CLICKED = "gate:sub"
    CONFIRM = "gate:confirm"
    RECHECK = "gate:recheck"
    HELP = "gate:help"
    RESTART = "gate:restart"

    @classmethod
    def parse(cls, raw: str | None) -> "Action | None":
        """Strict allow-list parse. Unknown data returns ``None``."""
        if not raw:
            return None
        try:
            return cls(raw)
        except ValueError:
            return None


def gate_keyboard(subscribe_url: str, *, include_help: bool = True) -> InlineKeyboardMarkup:
    """The main two-button gate shown by /start."""
    rows = [
        [InlineKeyboardButton(copy.BTN_SUBSCRIBE, url=subscribe_url)],
        [
            InlineKeyboardButton(
                copy.BTN_CONFIRMED, callback_data=Action.SUBSCRIBE_CLICKED.value
            )
        ],
    ]
    if include_help:
        rows.append(
            [InlineKeyboardButton(copy.BTN_HELP, callback_data=Action.HELP.value)]
        )
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(subscribe_url: str) -> InlineKeyboardMarkup:
    """Shown after the honour-system disclosure: subscribe again, or proceed."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(copy.BTN_SUBSCRIBE, url=subscribe_url)],
            [
                InlineKeyboardButton(
                    copy.BTN_CONFIRMED, callback_data=Action.CONFIRM.value
                )
            ],
        ]
    )


def join_keyboard(group_links: tuple[str, ...]) -> InlineKeyboardMarkup:
    """The payoff button(s)."""
    if not group_links:
        return InlineKeyboardMarkup([])
        
    if len(group_links) == 1:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(copy.BTN_JOIN_GROUP, url=group_links[0])]]
        )

    rows = []
    for i, link in enumerate(group_links, start=1):
        rows.append([InlineKeyboardButton(f"{copy.BTN_JOIN_GROUP} {i}", url=link)])
    return InlineKeyboardMarkup(rows)


def oauth_keyboard(auth_url: str) -> InlineKeyboardMarkup:
    """Phase 2: send the user to Google's consent screen."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(copy.BTN_VERIFY_GOOGLE, url=auth_url)],
            [
                InlineKeyboardButton(
                    copy.BTN_RECHECK, callback_data=Action.RECHECK.value
                )
            ],
        ]
    )


def retry_keyboard(subscribe_url: str) -> InlineKeyboardMarkup:
    """Shown when a real check said 'not subscribed'."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(copy.BTN_SUBSCRIBE, url=subscribe_url)],
            [
                InlineKeyboardButton(
                    copy.BTN_RECHECK, callback_data=Action.RECHECK.value
                )
            ],
        ]
    )


def restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(copy.BTN_START_OVER, callback_data=Action.RESTART.value)]]
    )
