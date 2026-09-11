"""SQLite persistence layer.

Privacy note
------------
We deliberately store only what the funnel actually needs:

* the numeric Telegram user id (needed to recognise a returning user)
* timestamps for each funnel step
* which verification method was used
* the single-use invite link we generated for that user, if any

We do **not** store names, usernames, phone numbers, emails, Google
credentials, OAuth tokens, or message contents.

SQL safety
----------
Every statement uses ``?`` placeholders. No SQL string is ever built by
concatenating or formatting user-controlled data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    telegram_user_id     INTEGER PRIMARY KEY,
    first_seen_at        TEXT    NOT NULL,
    last_seen_at         TEXT    NOT NULL,
    clicked_subscribe_at TEXT,
    confirmed_at         TEXT,
    verification_method  TEXT,
    invite_sent_at       TEXT,
    invite_link          TEXT,
    joined_at            TEXT,
    confirm_attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_confirmed_at ON users (confirmed_at);

-- Short-lived one-time nonces for the Google OAuth round trip (Phase 2).
-- Rows are deleted as soon as they are used, and expired rows are purged.
CREATE TABLE IF NOT EXISTS oauth_nonces (
    nonce            TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    code_verifier    TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    expires_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_nonces_expires ON oauth_nonces (expires_at);

-- Generic key/value store for small bits of bot state, e.g. the cached
-- join-request invite link so we do not recreate it on every restart.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    """Timezone-aware UTC 'now'. One place, so tests can reason about it."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Could not parse stored timestamp %r; treating as empty", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class UserRecord:
    """One row of the ``users`` table."""

    telegram_user_id: int
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    clicked_subscribe_at: datetime | None
    confirmed_at: datetime | None
    verification_method: str | None
    invite_sent_at: datetime | None
    invite_link: str | None
    joined_at: datetime | None
    confirm_attempts: int

    @property
    def is_verified(self) -> bool:
        return self.confirmed_at is not None

    @property
    def has_invite(self) -> bool:
        return self.invite_sent_at is not None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "UserRecord":
        return cls(
            telegram_user_id=row["telegram_user_id"],
            first_seen_at=_parse(row["first_seen_at"]),
            last_seen_at=_parse(row["last_seen_at"]),
            clicked_subscribe_at=_parse(row["clicked_subscribe_at"]),
            confirmed_at=_parse(row["confirmed_at"]),
            verification_method=row["verification_method"],
            invite_sent_at=_parse(row["invite_sent_at"]),
            invite_link=row["invite_link"],
            joined_at=_parse(row["joined_at"]),
            confirm_attempts=row["confirm_attempts"] or 0,
        )


class Database:
    """Thin async wrapper around a single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    # -- lifecycle -------------------------------------------------------

    async def connect(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # aiosqlite.Connection IS a Thread, and it is not a daemon by default.
        # If the process ever dies without calling close() (an unhandled crash,
        # a failing test), a non-daemon thread keeps the interpreter alive
        # forever instead of letting it exit. Marking it daemon must happen
        # before awaiting, because awaiting is what start()s the thread.
        connection = aiosqlite.connect(self.path)
        connection.daemon = True
        self._conn = await connection

        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            ("schema_version", str(SCHEMA_VERSION), _iso(utcnow())),
        )
        await self._conn.commit()
        logger.info("Database ready at %s", self.path)
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        return self._conn

    async def _commit(self) -> None:
        await self.conn.commit()

    # -- users -----------------------------------------------------------

    async def touch_user(self, telegram_user_id: int) -> UserRecord:
        """Create the user row if missing, otherwise bump ``last_seen_at``."""
        now = _iso(utcnow())
        await self.conn.execute(
            """
            INSERT INTO users (telegram_user_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (telegram_user_id, now, now),
        )
        await self._commit()
        record = await self.get_user(telegram_user_id)
        assert record is not None  # just inserted
        return record

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        async with self.conn.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return UserRecord.from_row(row) if row else None

    async def mark_subscribe_clicked(self, telegram_user_id: int) -> None:
        """Record the *first* time the user tapped 'Subscribe on YouTube'."""
        await self.conn.execute(
            """
            UPDATE users
               SET clicked_subscribe_at = COALESCE(clicked_subscribe_at, ?),
                   last_seen_at = ?
             WHERE telegram_user_id = ?
            """,
            (_iso(utcnow()), _iso(utcnow()), telegram_user_id),
        )
        await self._commit()

    async def increment_confirm_attempts(self, telegram_user_id: int) -> int:
        await self.conn.execute(
            "UPDATE users SET confirm_attempts = confirm_attempts + 1 "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await self._commit()
        async with self.conn.execute(
            "SELECT confirm_attempts FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["confirm_attempts"] if row else 0

    async def mark_confirmed(self, telegram_user_id: int, method: str) -> None:
        """Mark the user as having passed the gate (honor or real verification)."""
        now = _iso(utcnow())
        await self.conn.execute(
            """
            UPDATE users
               SET confirmed_at = COALESCE(confirmed_at, ?),
                   verification_method = ?,
                   last_seen_at = ?
             WHERE telegram_user_id = ?
            """,
            (now, method, now, telegram_user_id),
        )
        await self._commit()

    async def mark_invite_sent(
        self, telegram_user_id: int, invite_link: str | None
    ) -> None:
        now = _iso(utcnow())
        await self.conn.execute(
            """
            UPDATE users
               SET invite_sent_at = COALESCE(invite_sent_at, ?),
                   invite_link = ?,
                   last_seen_at = ?
             WHERE telegram_user_id = ?
            """,
            (now, invite_link, now, telegram_user_id),
        )
        await self._commit()

    async def mark_joined(self, telegram_user_id: int) -> None:
        await self.conn.execute(
            "UPDATE users SET joined_at = COALESCE(joined_at, ?) "
            "WHERE telegram_user_id = ?",
            (_iso(utcnow()), telegram_user_id),
        )
        await self._commit()

    async def reset_user(self, telegram_user_id: int) -> None:
        """Undo verification for one user (admin tool / tests)."""
        await self.conn.execute(
            """
            UPDATE users
               SET confirmed_at = NULL,
                   verification_method = NULL,
                   invite_sent_at = NULL,
                   invite_link = NULL,
                   joined_at = NULL,
                   confirm_attempts = 0
             WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        )
        await self._commit()

    async def delete_user(self, telegram_user_id: int) -> None:
        """Hard-delete everything we know about one user (GDPR-style erase)."""
        await self.conn.execute(
            "DELETE FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        await self.conn.execute(
            "DELETE FROM oauth_nonces WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        await self._commit()

    # -- stats -----------------------------------------------------------

    async def stats(self) -> dict[str, int]:
        query = """
            SELECT
                COUNT(*)                                              AS total_users,
                SUM(CASE WHEN clicked_subscribe_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked_subscribe,
                SUM(CASE WHEN confirmed_at IS NOT NULL THEN 1 ELSE 0 END)         AS confirmed,
                SUM(CASE WHEN invite_sent_at IS NOT NULL THEN 1 ELSE 0 END)       AS invites_sent,
                SUM(CASE WHEN joined_at IS NOT NULL THEN 1 ELSE 0 END)            AS joined,
                SUM(CASE WHEN verification_method = 'youtube_oauth' THEN 1 ELSE 0 END) AS oauth_verified,
                SUM(CASE WHEN verification_method = 'honor' THEN 1 ELSE 0 END)         AS honor_confirmed
            FROM users
        """
        async with self.conn.execute(query) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {}
        return {key: (row[key] or 0) for key in row.keys()}

    async def recent_users(self, limit: int = 10) -> list[UserRecord]:
        limit = max(1, min(int(limit), 100))  # clamp; never interpolate raw input
        async with self.conn.execute(
            "SELECT * FROM users ORDER BY first_seen_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [UserRecord.from_row(row) for row in rows]

    # -- oauth nonces (Phase 2) -----------------------------------------

    async def create_oauth_nonce(
        self,
        nonce: str,
        telegram_user_id: int,
        code_verifier: str,
        ttl_seconds: int = 900,
    ) -> None:
        now = utcnow()
        await self.conn.execute(
            """
            INSERT INTO oauth_nonces
                (nonce, telegram_user_id, code_verifier, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                nonce,
                telegram_user_id,
                code_verifier,
                _iso(now),
                _iso(now + timedelta(seconds=ttl_seconds)),
            ),
        )
        await self._commit()

    async def consume_oauth_nonce(self, nonce: str) -> tuple[int, str] | None:
        """Atomically fetch-and-delete a nonce. Returns ``(user_id, verifier)``.

        Returns ``None`` if the nonce is unknown, already used, or expired.
        Single-use by construction, which is what stops replay attacks.
        """
        async with self.conn.execute(
            "SELECT telegram_user_id, code_verifier, expires_at "
            "FROM oauth_nonces WHERE nonce = ?",
            (nonce,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None

        await self.conn.execute("DELETE FROM oauth_nonces WHERE nonce = ?", (nonce,))
        await self._commit()

        expires_at = _parse(row["expires_at"])
        if expires_at is None or expires_at < utcnow():
            return None
        return row["telegram_user_id"], row["code_verifier"]

    async def purge_expired_nonces(self) -> int:
        cursor = await self.conn.execute(
            "DELETE FROM oauth_nonces WHERE expires_at < ?", (_iso(utcnow()),)
        )
        await self._commit()
        return cursor.rowcount or 0

    # -- key/value -------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _iso(utcnow())),
        )
        await self._commit()

    async def kv_delete(self, key: str) -> None:
        await self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        await self._commit()

    # -- export ----------------------------------------------------------

    async def export_rows(self) -> Iterable[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM users ORDER BY first_seen_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
