#!/usr/bin/env python3
"""A stand-in for api.telegram.org, used to test that the bot really starts.

    python scripts/mock_telegram_api.py [port]

It answers the handful of Bot API methods python-telegram-bot calls during
startup and polling, so ``run.py`` can be launched for real - initialising,
running post_init, serving, and shutting down - without a live bot token and
without touching Telegram's servers.

This is a TEST FIXTURE. It is never imported by the bot itself.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from aiohttp import web

BOT_USER = {
    "id": 1234567,
    "is_bot": True,
    "first_name": "Mock Gate Bot",
    "username": "MockGateBot",
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}

CHAT = {
    "id": -1001234567890,
    "title": "Mock Existing Group",
    "type": "supergroup",
}


def _ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


async def handle(request: web.Request) -> web.Response:
    method = request.match_info["method"]

    # Log so the test (and a human) can see exactly what was called.
    print(f"[mock-telegram] {method}", flush=True)

    if method == "getMe":
        return _ok(BOT_USER)
    if method == "getUpdates":
        # Deliver any scripted updates exactly once, then go quiet.
        pending = request.app["pending_updates"]
        if pending:
            request.app["pending_updates"] = []
            return _ok(pending)
        return _ok([])
    if method in {"deleteWebhook", "setMyCommands", "close", "logOut"}:
        return _ok(True)
    if method == "getWebhookInfo":
        return _ok({"url": "", "has_custom_certificate": False, "pending_update_count": 0})
    if method == "getChat":
        return _ok(CHAT)
    if method == "getChatMemberCount":
        return _ok(5088)
    if method == "getChatMember":
        return _ok(
            {
                "status": "administrator",
                "user": BOT_USER,
                "can_invite_users": True,
                "can_be_edited": False,
                "can_manage_chat": True,
                "is_anonymous": False,
                "can_delete_messages": False,
                "can_manage_video_chats": False,
                "can_restrict_members": False,
                "can_promote_members": False,
                "can_change_info": False,
                "can_post_messages": False,
                "can_edit_messages": False,
                "can_pin_messages": False,
                "can_post_stories": False,
                "can_edit_stories": False,
                "can_delete_stories": False,
            }
        )
    if method in {"approveChatJoinRequest", "declineChatJoinRequest"}:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = dict(await request.post())
        print(f"[mock-telegram] {method.upper()} user={payload.get('user_id')}", flush=True)
        return _ok(True)
    if method == "createChatInviteLink":
        return _ok(
            {
                "invite_link": "https://t.me/+mockGeneratedLink",
                "creator": BOT_USER,
                "creates_join_request": True,
                "is_primary": False,
                "is_revoked": False,
            }
        )
    if method in {"sendMessage", "editMessageText", "answerCallbackQuery"}:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - form-encoded or empty body
            payload = dict(await request.post())
        # Print the outgoing message so a test can assert on what the user saw.
        print(f"[mock-telegram] OUTGOING {json.dumps(payload)}", flush=True)
        if method == "answerCallbackQuery":
            return _ok(True)
        return _ok(
            {
                "message_id": 1,
                "date": 0,
                "chat": {"id": payload.get("chat_id", 1), "type": "private"},
                "text": payload.get("text", ""),
            }
        )

    return web.json_response(
        {"ok": False, "error_code": 400, "description": f"Mock: unhandled {method}"},
        status=400,
    )


PRIVATE_USER = {"id": 424242, "is_bot": False, "first_name": "Insta", "language_code": "en"}
PRIVATE_CHAT = {"id": 424242, "type": "private", "first_name": "Insta"}


def start_command_update(update_id: int = 1) -> dict[str, Any]:
    """A realistic '/start' message, as Telegram would deliver it."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "date": 1700000000,
            "chat": PRIVATE_CHAT,
            "from": PRIVATE_USER,
            "text": "/start",
            "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
        },
    }


def callback_update(data: str, update_id: int = 2) -> dict[str, Any]:
    """A realistic inline-button tap."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": PRIVATE_USER,
            "chat_instance": "1",
            "data": data,
            "message": {
                "message_id": 11,
                "date": 1700000000,
                "chat": PRIVATE_CHAT,
                "from": BOT_USER,
                "text": "previous",
            },
        },
    }


def join_request_update(
    user: dict[str, Any], update_id: int, chat_id: int = -1001234567890
) -> dict[str, Any]:
    """Somebody tapping a join-request invite link."""
    return {
        "update_id": update_id,
        "chat_join_request": {
            "chat": {"id": chat_id, "title": "Mock Existing Group", "type": "supergroup"},
            "from": user,
            "user_chat_id": user["id"],
            "date": 1700000000,
        },
    }


STRANGER = {"id": 777777, "is_bot": False, "first_name": "Stranger", "language_code": "en"}


def build_app(pending_updates: list[dict[str, Any]] | None = None) -> web.Application:
    app = web.Application()
    app["pending_updates"] = list(pending_updates or [])
    # PTB builds URLs as <base_url><token>/<method>
    app.router.add_route("*", "/bot{token}/{method}", handle)
    return app


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    script = sys.argv[2] if len(sys.argv) > 2 else ""

    pending: list[dict[str, Any]] = []
    if script == "start":
        pending = [start_command_update()]
    elif script == "full":
        # The whole gate journey: /start -> "I've Subscribed" -> confirm.
        pending = [
            start_command_update(1),
            callback_update("gate:sub", 2),
            callback_update("gate:confirm", 3),
        ]
    elif script == "request":
        # The gate journey, then TWO people tap the join link: the person who
        # completed the gate, and a stranger who never did.
        pending = [
            start_command_update(1),
            callback_update("gate:sub", 2),
            callback_update("gate:confirm", 3),
            join_request_update(PRIVATE_USER, 4),  # verified -> should be approved
            join_request_update(STRANGER, 5),      # never gated -> must stay out
        ]

    print(f"[mock-telegram] listening on http://127.0.0.1:{port}", flush=True)
    if pending:
        print(f"[mock-telegram] scripted {len(pending)} update(s)", flush=True)
    web.run_app(build_app(pending), host="127.0.0.1", port=port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
