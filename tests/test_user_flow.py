"""End-to-end simulation of the whole user journey.

This is the test that answers "does the thing the user asked for actually
work?" — it drives a real GateHandlers instance through the exact sequence a
person from Instagram would follow, and asserts on the messages and buttons
they would see.
"""

from __future__ import annotations

from src.bot import copy
from src.bot.keyboards import Action
from src.config import InviteMode, VerificationMode
from tests.conftest import build_gate, make_settings


# ---------------------------------------------------------------------------
# THE MAIN FLOW (honour system, static link) — the default product
# ---------------------------------------------------------------------------


async def test_complete_happy_path(gate, db):
    """Instagram -> /start -> subscribe -> confirm -> group link."""

    # 1. They tap the bio link, which opens the bot and sends /start.
    welcome = await gate.send_command("start")
    assert "Welcome" in welcome.text
    assert "the Test Crew" in welcome.text
    assert copy.BTN_SUBSCRIBE in welcome.button_texts
    assert copy.BTN_CONFIRMED in welcome.button_texts

    # The subscribe button is a real YouTube link that opens the sub dialog.
    youtube_url = next(u for u in welcome.button_urls if "youtube.com" in u)
    assert "sub_confirmation=1" in youtube_url

    # 2. They tap "I've Subscribed" -> honest disclosure, not instant access.
    disclosure = await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    assert "honour system" in disclosure.text
    assert "not" in disclosure.text.lower()
    assert "t.me" not in disclosure.text  # no link leaked yet

    # 3. They confirm for real -> they get the group link.
    success = await gate.tap(Action.CONFIRM.value)
    assert "Thanks" in success.text
    assert copy.BTN_JOIN_GROUP in success.button_texts
    assert success.button_urls == ["https://t.me/+TestInviteLink123"]

    # 4. The database recorded exactly the funnel, nothing more.
    record = await db.get_user(gate.user.id)
    assert record.clicked_subscribe_at is not None
    assert record.confirmed_at is not None
    assert record.verification_method == "honor"
    assert record.invite_sent_at is not None


async def test_link_is_never_given_before_confirming(gate):
    """The group link must not appear in any message before the final tap."""
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    assert not any("TestInviteLink" in text for text in gate.recorder.texts)


async def test_returning_verified_user_gets_link_immediately(gate):
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    gate.recorder.clear()

    again = await gate.send_command("start")
    assert "Welcome back" in again.text
    assert again.button_urls == ["https://t.me/+TestInviteLink123"]
    # One message, not the whole funnel again.
    assert len(gate.recorder.messages) == 1


async def test_repeated_taps_do_not_spam_the_chat(gate):
    await gate.send_command("start")
    gate.recorder.clear()

    for _ in range(3):
        await gate.tap(Action.SUBSCRIBE_CLICKED.value)

    # Each tap edits the same message in place instead of sending a new one.
    assert all(message.kind == "edit" for message in gate.recorder.messages)


async def test_identical_edit_is_swallowed(gate):
    """Telegram's "Message is not modified" error must not surface to the user."""
    from telegram.error import BadRequest

    from tests.conftest import FakeCallbackQuery

    await gate.send_command("start")
    gate.recorder.clear()

    query = FakeCallbackQuery(
        Action.HELP.value, gate.user, gate.chat, gate.recorder
    )
    query.edit_error = BadRequest("Message is not modified")
    update = gate._update(callback_query=query)
    from tests.conftest import FakeContext

    await gate.handlers.on_callback(update, FakeContext(bot=gate.bot))

    assert gate.recorder.messages == []  # silently ignored, no duplicate sent


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------


async def test_help_command(gate):
    message = await gate.send_command("help")
    assert "/start" in message.text
    assert "/help" in message.text


async def test_status_tracks_progress(gate):
    before = await gate.send_command("status")
    assert "⬜ Received the group link" in before.text

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)

    after = await gate.send_command("status")
    assert "✅ Received the group link" in after.text
    assert "honour system" in after.text


async def test_privacy_command_lists_what_is_stored(gate):
    message = await gate.send_command("privacy")
    assert "Telegram user ID" in message.text
    assert "never asks for your Google account" in message.text


async def test_forgetme_erases_the_user(gate, db):
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    assert await db.get_user(gate.user.id) is not None

    message = await gate.send_command("forgetme")
    assert "deleted" in message.text
    assert await db.get_user(gate.user.id) is None


async def test_stats_is_admin_only(gate):
    denied = await gate.send_command("stats")
    assert "only for the bot owner" in denied.text


async def test_stats_for_admin(settings, db, recorder, fake_bot):
    admin = build_gate(settings, db, recorder, fake_bot, user_id=999)
    await admin.send_command("start")
    message = await admin.send_command("stats")
    assert "Funnel stats" in message.text
    assert "Conversion" in message.text


async def test_unknown_command_is_handled_gracefully(gate):
    message = await gate.send_command("start")
    gate.recorder.clear()
    message = await gate.unknown_command()
    assert "don't know that command" in message.text
    assert copy.BTN_START_OVER in message.button_texts


async def test_random_text_gets_a_nudge_not_an_error(gate):
    message = await gate.say("how do i join???")
    assert "buttons" in message.text
    assert copy.BTN_SUBSCRIBE in message.button_texts


async def test_verified_user_typing_text_gets_the_link_again(gate):
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    gate.recorder.clear()

    message = await gate.say("thanks!")
    assert message.button_urls == ["https://t.me/+TestInviteLink123"]


# ---------------------------------------------------------------------------
# INVALID / HOSTILE INPUT
# ---------------------------------------------------------------------------


async def test_unknown_callback_data_is_ignored(gate):
    from tests.conftest import FakeCallbackQuery, FakeContext

    query = FakeCallbackQuery("gate:give_me_admin", gate.user, gate.chat, gate.recorder)
    update = gate._update(callback_query=query)
    await gate.handlers.on_callback(update, FakeContext(bot=gate.bot))

    assert gate.recorder.messages == []  # nothing sent
    assert query.answers == [None]  # spinner stopped


async def test_crafted_callback_cannot_skip_the_gate(gate, db):
    """A forged payload must not hand out the link."""
    from tests.conftest import FakeCallbackQuery, FakeContext

    for payload in ["gate:confirm ", "GATE:CONFIRM", "'; --", "gate:invite", ""]:
        query = FakeCallbackQuery(payload, gate.user, gate.chat, gate.recorder)
        await gate.handlers.on_callback(
            gate._update(callback_query=query), FakeContext(bot=gate.bot)
        )

    assert not any("TestInviteLink" in text for text in gate.recorder.texts)
    record = await db.get_user(gate.user.id)
    assert record is None or not record.is_verified


async def test_bot_users_are_ignored(gate):
    from tests.conftest import FakeContext

    gate.user.is_bot = True
    await gate.handlers.start(gate._update(), FakeContext(bot=gate.bot))
    assert gate.recorder.messages == []


async def test_rate_limiting_kicks_in(gate):
    await gate.send_command("start")
    gate.recorder.clear()
    for _ in range(40):
        await gate.tap(Action.HELP.value)
    # The limiter stopped it well before 40 responses.
    assert len(gate.recorder.messages) < 20


# ---------------------------------------------------------------------------
# MIN_SECONDS_BEFORE_CONFIRM friction
# ---------------------------------------------------------------------------


async def test_too_fast_confirmation_is_slowed_down(tmp_path, db, recorder, fake_bot):
    settings = make_settings(tmp_path, min_seconds_before_confirm=30)
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    message = await gate.tap(Action.CONFIRM.value)

    assert "That was quick" in message.text
    assert not any("TestInviteLink" in text for text in recorder.texts)


async def test_zero_wait_is_the_default(gate):
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    message = await gate.tap(Action.CONFIRM.value)
    assert "Thanks" in message.text


# ---------------------------------------------------------------------------
# INVITE MODES
# ---------------------------------------------------------------------------


async def test_unique_mode_creates_a_single_use_link(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.UNIQUE, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    message = await gate.tap(Action.CONFIRM.value)

    assert "personal" in message.text
    assert fake_bot.created_links[0]["member_limit"] == 1
    assert fake_bot.created_links[0]["expire_date"] is not None
    assert message.button_urls[0].startswith("https://t.me/+generated_")


async def test_unique_link_is_reused_not_regenerated(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.UNIQUE, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    await gate.send_command("start")  # comes back later

    assert len(fake_bot.created_links) == 1  # not minted twice


async def test_request_mode_uses_a_join_request_link(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    message = await gate.tap(Action.CONFIRM.value)

    assert fake_bot.created_links[0]["creates_join_request"] is True
    assert "approved automatically" in message.text


async def test_request_link_is_cached_across_users(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_ids=(-1001234567890,)
    )
    for user_id in (1, 2, 3):
        gate = build_gate(settings, db, recorder, fake_bot, user_id=user_id)
        await gate.send_command("start")
        await gate.tap(Action.SUBSCRIBE_CLICKED.value)
        await gate.tap(Action.CONFIRM.value)

    assert len(fake_bot.created_links) == 1  # one link, reused


async def test_verified_join_request_is_approved(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    await gate.request_join()

    assert fake_bot.approved == [gate.user.id]
    assert (await db.get_user(gate.user.id)).joined_at is not None


async def test_unverified_join_request_is_left_pending(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.request_join()

    assert fake_bot.approved == []  # NOT let in
    assert any("Before I can let you in" in text for text in recorder.texts)


async def test_join_request_for_another_chat_is_ignored(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.REQUEST, group_chat_ids=(-1001234567890,)
    )
    gate = build_gate(settings, db, recorder, fake_bot)
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    recorder.clear()

    await gate.request_join(group_chat_id=-100999999)  # someone else's group

    assert fake_bot.approved == []


async def test_invite_failure_falls_back_to_static_link(tmp_path, db, recorder, fake_bot):
    settings = make_settings(
        tmp_path, invite_mode=InviteMode.UNIQUE, group_chat_ids=(-1001234567890,)
    )
    fake_bot.fail_create_link = True
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    message = await gate.tap(Action.CONFIRM.value)

    assert message.button_urls == ["https://t.me/+TestInviteLink123"]


async def test_invite_failure_without_fallback_shows_a_clear_error(
    tmp_path, db, recorder, fake_bot
):
    settings = make_settings(
        tmp_path,
        invite_mode=InviteMode.UNIQUE,
        group_chat_ids=(-1001234567890,),
        group_invite_links=(),
    )
    fake_bot.fail_create_link = True
    gate = build_gate(settings, db, recorder, fake_bot)

    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    message = await gate.tap(Action.CONFIRM.value)

    assert "couldn't create your invite link" in message.text
    assert "admin" in message.text


# ---------------------------------------------------------------------------
# HONESTY GUARANTEE
# ---------------------------------------------------------------------------


async def test_honor_mode_never_claims_technical_verification(gate):
    """No message in the free flow may claim YouTube was checked."""
    await gate.send_command("start")
    await gate.tap(Action.SUBSCRIBE_CLICKED.value)
    await gate.tap(Action.CONFIRM.value)
    await gate.send_command("status")

    combined = " ".join(gate.recorder.texts).lower()
    for false_claim in [
        "we verified your subscription",
        "youtube confirmed",
        "subscription verified with youtube",
        "we checked with youtube",
    ]:
        assert false_claim not in combined

    assert "honour system" in combined
    assert "has <b>not</b> technically\nchecked" in " ".join(gate.recorder.texts)


async def test_honor_verifier_flags_itself_as_unverified(settings, db):
    from src.verification import build_verifier

    verifier = build_verifier(settings, db)
    assert verifier.provides_real_verification is False

    await db.touch_user(1)
    result = await verifier.verify(1)
    assert result.passed is True
    assert result.was_technically_verified is False
    assert "honour" in verifier.describe()


async def test_oauth_verifier_is_selected_by_config(tmp_path, db):
    from src.verification import YouTubeOAuthVerifier, build_verifier

    settings = make_settings(
        tmp_path,
        verification_mode=VerificationMode.YOUTUBE_OAUTH,
        google_client_id="cid",
        google_client_secret="secret",
        oauth_public_base_url="https://example.com",
        oauth_state_secret="x" * 32,
        youtube_channel_id="UC_x5XG1OV2P6uZZ5FSM9Ttw",
    )
    verifier = build_verifier(settings, db)
    assert isinstance(verifier, YouTubeOAuthVerifier)
    assert verifier.provides_real_verification is True
    await verifier.stop()
