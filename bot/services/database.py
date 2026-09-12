"""Async SQLite persistence layer."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS chats (
    chat_id            INTEGER PRIMARY KEY,
    title              TEXT,
    chat_type          TEXT,
    username           TEXT,
    default_duration   TEXT,                -- NULL => use global default
    tracking_enabled   INTEGER NOT NULL DEFAULT 1,
    auto_kick          INTEGER NOT NULL DEFAULT 1,
    kick_mode          TEXT NOT NULL DEFAULT 'kick',   -- 'kick' (can rejoin) or 'ban'
    notify_user        INTEGER NOT NULL DEFAULT 1,     -- DM user on expiry / reminders
    log_chat_id        INTEGER,                        -- where to post logs
    welcome_enabled    INTEGER NOT NULL DEFAULT 0,
    welcome_text       TEXT,
    approve_requests   INTEGER NOT NULL DEFAULT 0,     -- auto approve join requests
    added_by           INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    full_name     TEXT,
    username      TEXT,
    joined_at     TEXT NOT NULL,
    expires_at    TEXT,                    -- NULL => permanent
    status        TEXT NOT NULL DEFAULT 'active',  -- active | left | kicked | expired | whitelisted
    note          TEXT,
    reminders_sent TEXT NOT NULL DEFAULT '',        -- comma separated hours already reminded
    added_by      INTEGER,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id),
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_members_expiry ON members(status, expires_at);

CREATE TABLE IF NOT EXISTS whitelist (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    added_by INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_admins (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER,
    user_id   INTEGER,
    action    TEXT NOT NULL,
    details   TEXT,
    actor_id  INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_chat ON logs(chat_id, id DESC);

CREATE TABLE IF NOT EXISTS user_context (
    user_id  INTEGER PRIMARY KEY,
    chat_id  INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Chat:
    chat_id: int
    title: str | None
    chat_type: str | None
    username: str | None
    default_duration: str | None
    tracking_enabled: bool
    auto_kick: bool
    kick_mode: str
    notify_user: bool
    log_chat_id: int | None
    welcome_enabled: bool
    welcome_text: str | None
    approve_requests: bool
    added_by: int | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Chat":
        return cls(
            chat_id=row["chat_id"],
            title=row["title"],
            chat_type=row["chat_type"],
            username=row["username"],
            default_duration=row["default_duration"],
            tracking_enabled=bool(row["tracking_enabled"]),
            auto_kick=bool(row["auto_kick"]),
            kick_mode=row["kick_mode"],
            notify_user=bool(row["notify_user"]),
            log_chat_id=row["log_chat_id"],
            welcome_enabled=bool(row["welcome_enabled"]),
            welcome_text=row["welcome_text"],
            approve_requests=bool(row["approve_requests"]),
            added_by=row["added_by"],
        )

    @property
    def display(self) -> str:
        return self.title or (f"@{self.username}" if self.username else str(self.chat_id))


@dataclass
class Member:
    chat_id: int
    user_id: int
    full_name: str | None
    username: str | None
    joined_at: datetime
    expires_at: datetime | None
    status: str
    note: str | None
    reminders_sent: set[int]
    added_by: int | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Member":
        sent = {int(x) for x in (row["reminders_sent"] or "").split(",") if x.strip()}
        return cls(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            full_name=row["full_name"],
            username=row["username"],
            joined_at=from_iso(row["joined_at"]) or utcnow(),
            expires_at=from_iso(row["expires_at"]),
            status=row["status"],
            note=row["note"],
            reminders_sent=sent,
            added_by=row["added_by"],
        )

    @property
    def display(self) -> str:
        name = self.full_name or "Unknown"
        if self.username:
            return f"{name} (@{self.username})"
        return name

    @property
    def mention_html(self) -> str:
        from html import escape

        return f'<a href="tg://user?id={self.user_id}">{escape(self.full_name or str(self.user_id))}</a>'


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.conn.execute(sql, tuple(params))
        await self.conn.commit()

    # ------------------------------------------------------------------ chats
    async def upsert_chat(
        self,
        chat_id: int,
        title: str | None,
        chat_type: str | None,
        username: str | None,
        added_by: int | None = None,
    ) -> Chat:
        now = to_iso(utcnow())
        await self._exec(
            """
            INSERT INTO chats (chat_id, title, chat_type, username, added_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                chat_type=excluded.chat_type,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (chat_id, title, chat_type, username, added_by, now, now),
        )
        chat = await self.get_chat(chat_id)
        assert chat is not None
        return chat

    async def get_chat(self, chat_id: int) -> Chat | None:
        async with self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return Chat.from_row(row) if row else None

    async def list_chats(self) -> list[Chat]:
        async with self.conn.execute("SELECT * FROM chats ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [Chat.from_row(r) for r in rows]

    async def update_chat(self, chat_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())
        values.extend([to_iso(utcnow()), chat_id])
        await self._exec(f"UPDATE chats SET {cols}, updated_at=? WHERE chat_id=?", values)

    async def delete_chat(self, chat_id: int) -> None:
        await self._exec("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        await self._exec("DELETE FROM whitelist WHERE chat_id=?", (chat_id,))
        await self._exec("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))

    # ---------------------------------------------------------------- members
    async def upsert_member(
        self,
        chat_id: int,
        user_id: int,
        full_name: str | None,
        username: str | None,
        expires_at: datetime | None,
        added_by: int | None = None,
        joined_at: datetime | None = None,
        note: str | None = None,
    ) -> Member:
        now = utcnow()
        await self._exec(
            """
            INSERT INTO members (chat_id, user_id, full_name, username, joined_at, expires_at,
                                 status, note, reminders_sent, added_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, '', ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                full_name=excluded.full_name,
                username=excluded.username,
                joined_at=excluded.joined_at,
                expires_at=excluded.expires_at,
                status='active',
                reminders_sent='',
                note=COALESCE(excluded.note, members.note),
                added_by=COALESCE(excluded.added_by, members.added_by),
                updated_at=excluded.updated_at
            """,
            (
                chat_id,
                user_id,
                full_name,
                username,
                to_iso(joined_at or now),
                to_iso(expires_at),
                note,
                added_by,
                to_iso(now),
            ),
        )
        member = await self.get_member(chat_id, user_id)
        assert member is not None
        return member

    async def get_member(self, chat_id: int, user_id: int) -> Member | None:
        async with self.conn.execute(
            "SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        return Member.from_row(row) if row else None

    async def set_member_expiry(
        self, chat_id: int, user_id: int, expires_at: datetime | None
    ) -> None:
        await self._exec(
            """UPDATE members SET expires_at=?, status='active', reminders_sent='', updated_at=?
               WHERE chat_id=? AND user_id=?""",
            (to_iso(expires_at), to_iso(utcnow()), chat_id, user_id),
        )

    async def set_member_status(self, chat_id: int, user_id: int, status: str) -> None:
        await self._exec(
            "UPDATE members SET status=?, updated_at=? WHERE chat_id=? AND user_id=?",
            (status, to_iso(utcnow()), chat_id, user_id),
        )

    async def set_member_note(self, chat_id: int, user_id: int, note: str | None) -> None:
        await self._exec(
            "UPDATE members SET note=?, updated_at=? WHERE chat_id=? AND user_id=?",
            (note, to_iso(utcnow()), chat_id, user_id),
        )

    async def update_member_profile(
        self, chat_id: int, user_id: int, full_name: str | None, username: str | None
    ) -> None:
        await self._exec(
            "UPDATE members SET full_name=?, username=?, updated_at=? WHERE chat_id=? AND user_id=?",
            (full_name, username, to_iso(utcnow()), chat_id, user_id),
        )

    async def mark_reminder_sent(self, chat_id: int, user_id: int, hours: int) -> None:
        member = await self.get_member(chat_id, user_id)
        if not member:
            return
        sent = member.reminders_sent | {hours}
        await self._exec(
            "UPDATE members SET reminders_sent=? WHERE chat_id=? AND user_id=?",
            (",".join(str(h) for h in sorted(sent)), chat_id, user_id),
        )

    async def delete_member(self, chat_id: int, user_id: int) -> None:
        await self._exec("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def list_members(
        self,
        chat_id: int,
        status: str | None = "active",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Member]:
        sql = "SELECT * FROM members WHERE chat_id=?"
        params: list[Any] = [chat_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY expires_at IS NULL, expires_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [Member.from_row(r) for r in rows]

    async def count_members(self, chat_id: int, status: str | None = "active") -> int:
        sql = "SELECT COUNT(*) FROM members WHERE chat_id=?"
        params: list[Any] = [chat_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def expired_members(self, now: datetime | None = None) -> list[Member]:
        now = now or utcnow()
        async with self.conn.execute(
            """SELECT * FROM members
               WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?
               ORDER BY expires_at ASC LIMIT 200""",
            (to_iso(now),),
        ) as cur:
            rows = await cur.fetchall()
        return [Member.from_row(r) for r in rows]

    async def expiring_members(self, before: datetime) -> list[Member]:
        """Active members expiring before ``before`` (for reminders)."""
        async with self.conn.execute(
            """SELECT * FROM members
               WHERE status='active' AND expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?
               ORDER BY expires_at ASC LIMIT 500""",
            (to_iso(utcnow()), to_iso(before)),
        ) as cur:
            rows = await cur.fetchall()
        return [Member.from_row(r) for r in rows]

    async def search_members(self, chat_id: int, query: str, limit: int = 20) -> list[Member]:
        like = f"%{query.lstrip('@')}%"
        async with self.conn.execute(
            """SELECT * FROM members WHERE chat_id=? AND (full_name LIKE ? OR username LIKE ?
               OR CAST(user_id AS TEXT) LIKE ?) ORDER BY updated_at DESC LIMIT ?""",
            (chat_id, like, like, like, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [Member.from_row(r) for r in rows]

    async def member_stats(self, chat_id: int) -> dict[str, int]:
        async with self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM members WHERE chat_id=? GROUP BY status", (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
        stats = {r["status"]: int(r["c"]) for r in rows}
        async with self.conn.execute(
            "SELECT COUNT(*) FROM members WHERE chat_id=? AND status='active' AND expires_at IS NULL",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        stats["permanent"] = int(row[0]) if row else 0
        return stats

    async def expiring_within_count(self, chat_id: int, before: datetime) -> int:
        async with self.conn.execute(
            """SELECT COUNT(*) FROM members WHERE chat_id=? AND status='active'
               AND expires_at IS NOT NULL AND expires_at <= ?""",
            (chat_id, to_iso(before)),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -------------------------------------------------------------- whitelist
    async def add_whitelist(self, chat_id: int, user_id: int, added_by: int | None) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO whitelist (chat_id, user_id, added_by, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, added_by, to_iso(utcnow())),
        )

    async def remove_whitelist(self, chat_id: int, user_id: int) -> None:
        await self._exec("DELETE FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def is_whitelisted(self, chat_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            return await cur.fetchone() is not None

    async def list_whitelist(self, chat_id: int) -> list[int]:
        async with self.conn.execute(
            "SELECT user_id FROM whitelist WHERE chat_id=? ORDER BY created_at", (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    # ------------------------------------------------------------ chat admins
    async def set_chat_admins(self, chat_id: int, user_ids: Iterable[int]) -> None:
        await self.conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
        await self.conn.executemany(
            "INSERT OR IGNORE INTO chat_admins (chat_id, user_id) VALUES (?, ?)",
            [(chat_id, uid) for uid in user_ids],
        )
        await self.conn.commit()

    async def is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM chat_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ) as cur:
            return await cur.fetchone() is not None

    async def chats_for_admin(self, user_id: int) -> list[Chat]:
        async with self.conn.execute(
            """SELECT c.* FROM chats c
               WHERE c.added_by=? OR c.chat_id IN (SELECT chat_id FROM chat_admins WHERE user_id=?)
               ORDER BY c.created_at""",
            (user_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        return [Chat.from_row(r) for r in rows]

    # ------------------------------------------------------------------- logs
    async def add_log(
        self,
        chat_id: int | None,
        user_id: int | None,
        action: str,
        details: str | None = None,
        actor_id: int | None = None,
    ) -> None:
        await self._exec(
            "INSERT INTO logs (chat_id, user_id, action, details, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, action, details, actor_id, to_iso(utcnow())),
        )

    async def recent_logs(self, chat_id: int, limit: int = 15) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ) as cur:
            return list(await cur.fetchall())

    # ----------------------------------------------------------- user context
    async def set_context(self, user_id: int, chat_id: int) -> None:
        await self._exec(
            """INSERT INTO user_context (user_id, chat_id, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, updated_at=excluded.updated_at""",
            (user_id, chat_id, to_iso(utcnow())),
        )

    async def get_context(self, user_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT chat_id FROM user_context WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["chat_id"]) if row else None

    # ---------------------------------------------------------------- global
    async def global_stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, sql in {
            "chats": "SELECT COUNT(*) FROM chats",
            "active": "SELECT COUNT(*) FROM members WHERE status='active'",
            "expired": "SELECT COUNT(*) FROM members WHERE status='expired'",
            "total": "SELECT COUNT(*) FROM members",
        }.items():
            async with self.conn.execute(sql) as cur:
                row = await cur.fetchone()
            out[key] = int(row[0]) if row else 0
        return out
