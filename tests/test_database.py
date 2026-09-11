"""Database behaviour, including the privacy guarantees."""

from __future__ import annotations

import asyncio

import pytest

from src.database import Database
from src.database.db import utcnow


async def test_touch_user_creates_then_updates(db: Database):
    first = await db.touch_user(1001)
    assert first.telegram_user_id == 1001
    assert first.first_seen_at is not None
    assert first.is_verified is False

    await asyncio.sleep(0.01)
    second = await db.touch_user(1001)
    assert second.first_seen_at == first.first_seen_at  # unchanged
    assert second.last_seen_at >= first.last_seen_at


async def test_get_unknown_user_returns_none(db: Database):
    assert await db.get_user(404) is None


async def test_full_funnel_progression(db: Database):
    await db.touch_user(1)
    await db.mark_subscribe_clicked(1)
    await db.mark_confirmed(1, "honor")
    await db.mark_invite_sent(1, "https://t.me/+abc")
    await db.mark_joined(1)

    record = await db.get_user(1)
    assert record.clicked_subscribe_at is not None
    assert record.confirmed_at is not None
    assert record.verification_method == "honor"
    assert record.invite_link == "https://t.me/+abc"
    assert record.joined_at is not None
    assert record.is_verified and record.has_invite


async def test_timestamps_are_not_overwritten_on_repeat(db: Database):
    await db.touch_user(2)
    await db.mark_confirmed(2, "honor")
    first_confirm = (await db.get_user(2)).confirmed_at

    await asyncio.sleep(0.01)
    await db.mark_confirmed(2, "honor")
    assert (await db.get_user(2)).confirmed_at == first_confirm


async def test_confirm_attempts_increment(db: Database):
    await db.touch_user(3)
    assert await db.increment_confirm_attempts(3) == 1
    assert await db.increment_confirm_attempts(3) == 2


async def test_reset_user_clears_verification_but_keeps_row(db: Database):
    await db.touch_user(4)
    await db.mark_confirmed(4, "honor")
    await db.reset_user(4)
    record = await db.get_user(4)
    assert record is not None
    assert record.is_verified is False
    assert record.confirm_attempts == 0


async def test_delete_user_removes_everything(db: Database):
    await db.touch_user(5)
    await db.create_oauth_nonce("n5", 5, "verifier")
    await db.delete_user(5)
    assert await db.get_user(5) is None
    assert await db.consume_oauth_nonce("n5") is None


async def test_only_expected_columns_exist(db: Database):
    """Guard against someone quietly adding a name/username/email column."""
    async with db.conn.execute("PRAGMA table_info(users)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    assert columns == {
        "telegram_user_id",
        "first_seen_at",
        "last_seen_at",
        "clicked_subscribe_at",
        "confirmed_at",
        "verification_method",
        "invite_sent_at",
        "invite_link",
        "joined_at",
        "confirm_attempts",
    }
    for forbidden in ("username", "first_name", "last_name", "phone", "email"):
        assert forbidden not in columns


async def test_stats(db: Database):
    for user_id in (10, 11, 12):
        await db.touch_user(user_id)
    await db.mark_subscribe_clicked(10)
    await db.mark_confirmed(10, "honor")
    await db.mark_invite_sent(10, "https://t.me/+x")
    await db.mark_confirmed(11, "youtube_oauth")

    stats = await db.stats()
    assert stats["total_users"] == 3
    assert stats["clicked_subscribe"] == 1
    assert stats["confirmed"] == 2
    assert stats["invites_sent"] == 1
    assert stats["honor_confirmed"] == 1
    assert stats["oauth_verified"] == 1


async def test_recent_users_limit_is_clamped(db: Database):
    for user_id in range(20):
        await db.touch_user(user_id)
    assert len(await db.recent_users(limit=5)) == 5
    assert len(await db.recent_users(limit=10_000)) <= 100
    assert len(await db.recent_users(limit=0)) == 1


# ---------------------------------------------------------------------------
# OAuth nonces
# ---------------------------------------------------------------------------


async def test_nonce_round_trip(db: Database):
    await db.create_oauth_nonce("nonce-a", 77, "verifier-a")
    assert await db.consume_oauth_nonce("nonce-a") == (77, "verifier-a")


async def test_nonce_is_single_use(db: Database):
    await db.create_oauth_nonce("nonce-b", 77, "v")
    assert await db.consume_oauth_nonce("nonce-b") is not None
    assert await db.consume_oauth_nonce("nonce-b") is None  # replay blocked


async def test_expired_nonce_is_rejected_and_removed(db: Database):
    await db.create_oauth_nonce("nonce-c", 77, "v", ttl_seconds=-1)
    assert await db.consume_oauth_nonce("nonce-c") is None


async def test_unknown_nonce_returns_none(db: Database):
    assert await db.consume_oauth_nonce("never-existed") is None


async def test_purge_expired_nonces(db: Database):
    await db.create_oauth_nonce("old", 1, "v", ttl_seconds=-10)
    await db.create_oauth_nonce("fresh", 1, "v", ttl_seconds=900)
    assert await db.purge_expired_nonces() == 1
    assert await db.consume_oauth_nonce("fresh") is not None


# ---------------------------------------------------------------------------
# key/value + injection safety
# ---------------------------------------------------------------------------


async def test_kv_round_trip(db: Database):
    assert await db.kv_get("missing") is None
    await db.kv_set("k", "v1")
    await db.kv_set("k", "v2")
    assert await db.kv_get("k") == "v2"
    await db.kv_delete("k")
    assert await db.kv_get("k") is None


async def test_sql_injection_attempt_is_stored_as_literal_text(db: Database):
    """Parameterised queries mean this is just a weird string, not SQL."""
    payload = "'; DROP TABLE users; --"
    await db.touch_user(1)
    await db.kv_set(payload, payload)

    assert await db.kv_get(payload) == payload
    # The users table is still there and intact.
    assert await db.get_user(1) is not None


async def test_injection_in_invite_link_is_inert(db: Database):
    await db.touch_user(1)
    await db.touch_user(2)
    await db.mark_invite_sent(2, "https://t.me/+x'); DELETE FROM users; --")

    # Both rows survive: the payload was stored as text, never executed.
    assert await db.get_user(1) is not None
    assert await db.get_user(2) is not None
    stats = await db.stats()
    assert stats["total_users"] == 2


async def test_connect_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "gate.sqlite3"
    database = Database(nested)
    await database.connect()
    await database.close()
    assert nested.exists()


async def test_use_before_connect_raises(tmp_path):
    database = Database(tmp_path / "x.sqlite3")
    with pytest.raises(RuntimeError):
        _ = database.conn


async def test_context_manager(tmp_path):
    async with Database(tmp_path / "ctx.sqlite3") as database:
        await database.touch_user(1)
        assert await database.get_user(1) is not None


async def test_stored_timestamps_are_timezone_aware(db: Database):
    await db.touch_user(9)
    record = await db.get_user(9)
    assert record.first_seen_at.tzinfo is not None
    assert (utcnow() - record.first_seen_at).total_seconds() < 10
