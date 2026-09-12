"""Async SQLite persistence layer with lightweight migrations."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
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
    status        TEXT NOT NULL DEFAULT 'active',  -- active | left | kicked | expired | manual
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

-- Join decisions waiting for an admin answer
CREATE TABLE IF NOT EXISTS pending_joins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    full_name   TEXT,
    username    TEXT,
    source      TEXT NOT NULL DEFAULT 'join',   -- join | request | manual
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | decided | expired | cancelled
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  INTEGER,
    decision    TEXT,                           -- e.g. "1m", "never", "remove"
    UNIQUE (chat_id, user_id, status) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_joins(status, created_at);

-- Messages sent to admins for a pending join (so we can edit them all once decided)
CREATE TABLE IF NOT EXISTS prompt_messages (
    pending_id  INTEGER NOT NULL,
    admin_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (pending_id, admin_id),
    FOREIGN KEY (pending_id) REFERENCES pending_joins(id) ON DELETE CASCADE
);

-- Invite links created through the bot with a preset duration
CREATE TABLE IF NOT EXISTS invite_links (
    invite_link  TEXT PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    name         TEXT,
    duration     TEXT NOT NULL,             -- duration text or 'never'
    created_by   INTEGER,
    created_at   TEXT NOT NULL,
    uses         INTEGER NOT NULL DEFAULT 0,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invite_chat ON invite_links(chat_id);

-- Generic key/value store for runtime state (last check time, metrics, ...)
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# (table, column, definition) — applied with ALTER TABLE when missing
MIGRATIONS: list[tuple[str, str, str]] = [
    ("chats", "ask_on_join", "INTEGER"),  # NULL => use global default
    ("chats", "ask_target", "TEXT NOT NULL DEFAULT 'owner'"),  # owner | admins
    ("chats", "owner_id", "INTEGER"),
    ("chats", "grace_hours", "INTEGER NOT NULL DEFAULT 0"),  # grace after expiry before removal
    ("members", "fail_count", "INTEGER NOT NULL DEFAULT 0"),
    ("members", "source", "TEXT"),  # join | request | invite:<link> | manual
    ("members", "renewals", "INTEGER NOT NULL DEFAULT 0"),
]


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
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_get(row: aiosqlite.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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
    approve_requests: int  # 0 = ignore, 1 = auto-approve, 2 = ask owner/admins
    added_by: int | None
    ask_on_join: bool | None = None
    ask_target: str = "owner"
    owner_id: int | None = None
    grace_hours: int = 0

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Chat":
        ask = _row_get(row, "ask_on_join")
        return cls(
            chat_id=row["chat_id"],
            title=row["title"],
            chat_type=row["chat_type"],
            username=row["username"],
            default_duration=row["default_duration"],
            tracking_enabled=bool(row["tracking_enabled"]),
            auto_kick=bool(row["auto_kick"]),
            kick_mode=row["kick_mode"] or "kick",
            notify_user=bool(row["notify_user"]),
            log_chat_id=row["log_chat_id"],
            welcome_enabled=bool(row["welcome_enabled"]),
            welcome_text=row["welcome_text"],
            approve_requests=int(row["approve_requests"] or 0),
            added_by=row["added_by"],
            ask_on_join=None if ask is None else bool(ask),
            ask_target=_row_get(row, "ask_target", "owner") or "owner",
            owner_id=_row_get(row, "owner_id"),
            grace_hours=int(_row_get(row, "grace_hours", 0) or 0),
        )

    @property
    def display(self) -> str:
        return self.title or (f"@{self.username}" if self.username else str(self.chat_id))

    @property
    def is_channel(self) -> bool:
        return self.chat_type == "channel"


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
    fail_count: int = 0
    source: str | None = None
    renewals: int = 0

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Member":
        sent: set[int] = set()
        for x in (row["reminders_sent"] or "").split(","):
            x = x.strip()
            if x.isdigit():
                sent.add(int(x))
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
            fail_count=int(_row_get(row, "fail_count", 0) or 0),
            source=_row_get(row, "source"),
            renewals=int(_row_get(row, "renewals", 0) or 0),
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

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass
class PendingJoin:
    id: int
    chat_id: int
    user_id: int
    full_name: str | None
    username: str | None
    source: str
    status: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: int | None
    decision: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "PendingJoin":
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            full_name=row["full_name"],
            username=row["username"],
            source=row["source"],
            status=row["status"],
            created_at=from_iso(row["created_at"]) or utcnow(),
            decided_at=from_iso(row["decided_at"]),
            decided_by=row["decided_by"],
            decision=row["decision"],
        )

    @property
    def mention_html(self) -> str:
        from html import escape

        return f'<a href="tg://user?id={self.user_id}">{escape(self.full_name or str(self.user_id))}</a>'

    @property
    def display(self) -> str:
        name = self.full_name or "Unknown"
        return f"{name} (@{self.username})" if self.username else name


@dataclass
class InviteLink:
    invite_link: str
    chat_id: int
    name: str | None
    duration: str
    created_by: int | None
    created_at: datetime
    uses: int
    revoked: bool

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "InviteLink":
        return cls(
            invite_link=row["invite_link"],
            chat_id=row["chat_id"],
            name=row["name"],
            duration=row["duration"],
            created_by=row["created_by"],
            created_at=from_iso(row["created_at"]) or utcnow(),
            uses=int(row["uses"] or 0),
            revoked=bool(row["revoked"]),
        )


class Database:
    """Thin async wrapper around a single aiosqlite connection.

    All writes go through :meth:`_exec` which commits immediately; a lock
    guarantees that multi-statement operations are not interleaved.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, timeout=30)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=30000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        log.info("Database ready at %s", self.path)

    async def _migrate(self) -> None:
        """Add columns that were introduced after the first release."""
        for table, column, definition in MIGRATIONS:
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {r["name"] for r in await cur.fetchall()}
            if column not in existing:
                log.info("Migrating: adding %s.%s", table, column)
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn:
            try:
                await self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def _exec(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._write_lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.rowcount

    async def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def backup_to(self, dest: str) -> str:
        """Create a consistent copy of the database file at ``dest``."""
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        async with self._write_lock:
            async with aiosqlite.connect(dest) as target:
                await self.conn.backup(target)
        return dest

    async def healthcheck(self) -> bool:
        try:
            row = await self._fetchone("SELECT 1")
            return bool(row)
        except Exception:  # noqa: BLE001
            return False

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
                title=COALESCE(excluded.title, chats.title),
                chat_type=COALESCE(excluded.chat_type, chats.chat_type),
                username=excluded.username,
                added_by=COALESCE(chats.added_by, excluded.added_by),
                updated_at=excluded.updated_at
            """,
            (chat_id, title, chat_type, username, added_by, now, now),
        )
        chat = await self.get_chat(chat_id)
        assert chat is not None
        return chat

    async def get_chat(self, chat_id: int) -> Chat | None:
        row = await self._fetchone("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        return Chat.from_row(row) if row else None

    async def list_chats(self, only_tracking: bool = False) -> list[Chat]:
        sql = "SELECT * FROM chats"
        if only_tracking:
            sql += " WHERE tracking_enabled=1"
        rows = await self._fetchall(sql + " ORDER BY created_at")
        return [Chat.from_row(r) for r in rows]

    async def update_chat(self, chat_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())
        values.extend([to_iso(utcnow()), chat_id])
        await self._exec(f"UPDATE chats SET {cols}, updated_at=? WHERE chat_id=?", values)

    async def delete_chat(self, chat_id: int) -> None:
        async with self._write_lock:
            for table in ("members", "whitelist", "chat_admins", "invite_links", "pending_joins", "chats"):
                await self.conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
            await self.conn.commit()

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
        source: str | None = None,
    ) -> Member:
        now = utcnow()
        await self._exec(
            """
            INSERT INTO members (chat_id, user_id, full_name, username, joined_at, expires_at,
                                 status, note, reminders_sent, added_by, updated_at, fail_count, source)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, '', ?, ?, 0, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                full_name=COALESCE(excluded.full_name, members.full_name),
                username=COALESCE(excluded.username, members.username),
                joined_at=excluded.joined_at,
                expires_at=excluded.expires_at,
                status='active',
                reminders_sent='',
                fail_count=0,
                note=COALESCE(excluded.note, members.note),
                added_by=COALESCE(excluded.added_by, members.added_by),
                source=COALESCE(excluded.source, members.source),
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
                source,
            ),
        )
        member = await self.get_member(chat_id, user_id)
        assert member is not None
        return member

    async def get_member(self, chat_id: int, user_id: int) -> Member | None:
        row = await self._fetchone(
            "SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        return Member.from_row(row) if row else None

    async def set_member_expiry(
        self, chat_id: int, user_id: int, expires_at: datetime | None, count_renewal: bool = False
    ) -> None:
        renew = ", renewals=renewals+1" if count_renewal else ""
        await self._exec(
            f"""UPDATE members SET expires_at=?, status='active', reminders_sent='', fail_count=0,
                updated_at=?{renew} WHERE chat_id=? AND user_id=?""",
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

    async def bump_fail_count(self, chat_id: int, user_id: int) -> int:
        await self._exec(
            "UPDATE members SET fail_count=fail_count+1, updated_at=? WHERE chat_id=? AND user_id=?",
            (to_iso(utcnow()), chat_id, user_id),
        )
        m = await self.get_member(chat_id, user_id)
        return m.fail_count if m else 0

    async def update_member_profile(
        self, chat_id: int, user_id: int, full_name: str | None, username: str | None
    ) -> None:
        await self._exec(
            "UPDATE members SET full_name=?, username=?, updated_at=? WHERE chat_id=? AND user_id=?",
            (full_name, username, to_iso(utcnow()), chat_id, user_id),
        )

    async def mark_reminder_sent(self, chat_id: int, user_id: int, hours: Iterable[int]) -> None:
        member = await self.get_member(chat_id, user_id)
        if not member:
            return
        sent = member.reminders_sent | set(hours)
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
        rows = await self._fetchall(sql, params)
        return [Member.from_row(r) for r in rows]

    async def iter_all_members(self, chat_id: int) -> list[Member]:
        rows = await self._fetchall(
            "SELECT * FROM members WHERE chat_id=? ORDER BY joined_at", (chat_id,)
        )
        return [Member.from_row(r) for r in rows]

    async def count_members(self, chat_id: int, status: str | None = "active") -> int:
        sql = "SELECT COUNT(*) FROM members WHERE chat_id=?"
        params: list[Any] = [chat_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        row = await self._fetchone(sql, params)
        return int(row[0]) if row else 0

    async def expired_members(self, now: datetime | None = None, limit: int = 200) -> list[Member]:
        now = now or utcnow()
        rows = await self._fetchall(
            """SELECT * FROM members
               WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?
               ORDER BY expires_at ASC LIMIT ?""",
            (to_iso(now), limit),
        )
        return [Member.from_row(r) for r in rows]

    async def expiring_members(self, before: datetime, chat_id: int | None = None) -> list[Member]:
        """Active members expiring before ``before`` (for reminders / reports)."""
        sql = """SELECT * FROM members
                 WHERE status='active' AND expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?"""
        params: list[Any] = [to_iso(utcnow()), to_iso(before)]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        sql += " ORDER BY expires_at ASC LIMIT 500"
        rows = await self._fetchall(sql, params)
        return [Member.from_row(r) for r in rows]

    async def search_members(self, chat_id: int, query: str, limit: int = 20) -> list[Member]:
        like = f"%{query.lstrip('@')}%"
        rows = await self._fetchall(
            """SELECT * FROM members WHERE chat_id=? AND (full_name LIKE ? OR username LIKE ?
               OR CAST(user_id AS TEXT) LIKE ?) ORDER BY updated_at DESC LIMIT ?""",
            (chat_id, like, like, like, limit),
        )
        return [Member.from_row(r) for r in rows]

    async def find_member_by_username(self, chat_id: int, username: str) -> Member | None:
        row = await self._fetchone(
            "SELECT * FROM members WHERE chat_id=? AND LOWER(username)=LOWER(?) LIMIT 1",
            (chat_id, username.lstrip("@")),
        )
        return Member.from_row(row) if row else None

    async def member_stats(self, chat_id: int) -> dict[str, int]:
        rows = await self._fetchall(
            "SELECT status, COUNT(*) AS c FROM members WHERE chat_id=? GROUP BY status", (chat_id,)
        )
        stats = {r["status"]: int(r["c"]) for r in rows}
        row = await self._fetchone(
            "SELECT COUNT(*) FROM members WHERE chat_id=? AND status='active' AND expires_at IS NULL",
            (chat_id,),
        )
        stats["permanent"] = int(row[0]) if row else 0
        row = await self._fetchone(
            "SELECT COUNT(*) FROM members WHERE chat_id=? AND joined_at >= ?",
            (chat_id, to_iso(utcnow().replace(hour=0, minute=0, second=0, microsecond=0))),
        )
        stats["joined_today"] = int(row[0]) if row else 0
        return stats

    async def expiring_within_count(self, chat_id: int, before: datetime) -> int:
        row = await self._fetchone(
            """SELECT COUNT(*) FROM members WHERE chat_id=? AND status='active'
               AND expires_at IS NOT NULL AND expires_at <= ?""",
            (chat_id, to_iso(before)),
        )
        return int(row[0]) if row else 0

    async def memberships_for_user(self, user_id: int) -> list[Member]:
        rows = await self._fetchall(
            "SELECT * FROM members WHERE user_id=? AND status='active' ORDER BY expires_at IS NULL, expires_at",
            (user_id,),
        )
        return [Member.from_row(r) for r in rows]

    # -------------------------------------------------------------- whitelist
    async def add_whitelist(self, chat_id: int, user_id: int, added_by: int | None) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO whitelist (chat_id, user_id, added_by, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, added_by, to_iso(utcnow())),
        )

    async def remove_whitelist(self, chat_id: int, user_id: int) -> None:
        await self._exec("DELETE FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def is_whitelisted(self, chat_id: int, user_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        return row is not None

    async def list_whitelist(self, chat_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT user_id FROM whitelist WHERE chat_id=? ORDER BY created_at", (chat_id,)
        )
        return [int(r["user_id"]) for r in rows]

    # ------------------------------------------------------------ chat admins
    async def set_chat_admins(self, chat_id: int, user_ids: Iterable[int]) -> None:
        async with self._write_lock:
            await self.conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
            await self.conn.executemany(
                "INSERT OR IGNORE INTO chat_admins (chat_id, user_id) VALUES (?, ?)",
                [(chat_id, uid) for uid in user_ids],
            )
            await self.conn.commit()

    async def list_chat_admins(self, chat_id: int) -> list[int]:
        rows = await self._fetchall("SELECT user_id FROM chat_admins WHERE chat_id=?", (chat_id,))
        return [int(r["user_id"]) for r in rows]

    async def is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM chat_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        return row is not None

    async def chats_for_admin(self, user_id: int) -> list[Chat]:
        rows = await self._fetchall(
            """SELECT c.* FROM chats c
               WHERE c.added_by=? OR c.owner_id=?
                  OR c.chat_id IN (SELECT chat_id FROM chat_admins WHERE user_id=?)
               ORDER BY c.created_at""",
            (user_id, user_id, user_id),
        )
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
            (chat_id, user_id, action, (details or "")[:500] or None, actor_id, to_iso(utcnow())),
        )

    async def recent_logs(self, chat_id: int, limit: int = 15) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM logs WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )

    async def user_logs(self, chat_id: int, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM logs WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, user_id, limit),
        )

    async def prune_logs(self, keep_days: int = 90) -> int:
        cutoff = utcnow().timestamp() - keep_days * 86400
        cutoff_iso = to_iso(datetime.fromtimestamp(cutoff, tz=timezone.utc))
        return await self._exec("DELETE FROM logs WHERE created_at < ?", (cutoff_iso,))

    # ----------------------------------------------------------- user context
    async def set_context(self, user_id: int, chat_id: int) -> None:
        await self._exec(
            """INSERT INTO user_context (user_id, chat_id, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, updated_at=excluded.updated_at""",
            (user_id, chat_id, to_iso(utcnow())),
        )

    async def get_context(self, user_id: int) -> int | None:
        row = await self._fetchone("SELECT chat_id FROM user_context WHERE user_id=?", (user_id,))
        return int(row["chat_id"]) if row else None

    # ---------------------------------------------------------- pending joins
    async def create_pending(
        self,
        chat_id: int,
        user_id: int,
        full_name: str | None,
        username: str | None,
        source: str = "join",
    ) -> PendingJoin:
        # cancel any older pending record for the same user/chat
        await self._exec(
            "UPDATE pending_joins SET status='cancelled' WHERE chat_id=? AND user_id=? AND status='pending'",
            (chat_id, user_id),
        )
        await self._exec(
            """INSERT INTO pending_joins (chat_id, user_id, full_name, username, source, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (chat_id, user_id, full_name, username, source, to_iso(utcnow())),
        )
        p = await self.get_pending_for(chat_id, user_id)
        assert p is not None
        return p

    async def get_pending(self, pending_id: int) -> PendingJoin | None:
        row = await self._fetchone("SELECT * FROM pending_joins WHERE id=?", (pending_id,))
        return PendingJoin.from_row(row) if row else None

    async def get_pending_for(self, chat_id: int, user_id: int) -> PendingJoin | None:
        row = await self._fetchone(
            """SELECT * FROM pending_joins WHERE chat_id=? AND user_id=? AND status='pending'
               ORDER BY id DESC LIMIT 1""",
            (chat_id, user_id),
        )
        return PendingJoin.from_row(row) if row else None

    async def list_pending(self, chat_id: int | None = None, limit: int = 50) -> list[PendingJoin]:
        sql = "SELECT * FROM pending_joins WHERE status='pending'"
        params: list[Any] = []
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(sql, params)
        return [PendingJoin.from_row(r) for r in rows]

    async def count_pending(self, chat_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) FROM pending_joins WHERE chat_id=? AND status='pending'", (chat_id,)
        )
        return int(row[0]) if row else 0

    async def resolve_pending(
        self, pending_id: int, decided_by: int | None, decision: str, status: str = "decided"
    ) -> bool:
        """Atomically mark a pending join as decided. Returns False if it was already resolved."""
        rows = await self._exec(
            """UPDATE pending_joins SET status=?, decided_at=?, decided_by=?, decision=?
               WHERE id=? AND status='pending'""",
            (status, to_iso(utcnow()), decided_by, decision[:100], pending_id),
        )
        return rows > 0

    async def stale_pending(self, older_than: datetime) -> list[PendingJoin]:
        rows = await self._fetchall(
            "SELECT * FROM pending_joins WHERE status='pending' AND created_at <= ? LIMIT 100",
            (to_iso(older_than),),
        )
        return [PendingJoin.from_row(r) for r in rows]

    async def add_prompt_message(self, pending_id: int, admin_id: int, message_id: int) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO prompt_messages (pending_id, admin_id, message_id) VALUES (?, ?, ?)",
            (pending_id, admin_id, message_id),
        )

    async def prompt_messages(self, pending_id: int) -> list[tuple[int, int]]:
        rows = await self._fetchall(
            "SELECT admin_id, message_id FROM prompt_messages WHERE pending_id=?", (pending_id,)
        )
        return [(int(r["admin_id"]), int(r["message_id"])) for r in rows]

    # ----------------------------------------------------------- invite links
    async def add_invite_link(
        self, invite_link: str, chat_id: int, name: str | None, duration: str, created_by: int | None
    ) -> InviteLink:
        await self._exec(
            """INSERT OR REPLACE INTO invite_links (invite_link, chat_id, name, duration, created_by, created_at, uses, revoked)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0)""",
            (invite_link, chat_id, name, duration, created_by, to_iso(utcnow())),
        )
        link = await self.get_invite_link(invite_link)
        assert link is not None
        return link

    async def get_invite_link(self, invite_link: str) -> InviteLink | None:
        row = await self._fetchone("SELECT * FROM invite_links WHERE invite_link=?", (invite_link,))
        return InviteLink.from_row(row) if row else None

    async def list_invite_links(self, chat_id: int, include_revoked: bool = False) -> list[InviteLink]:
        sql = "SELECT * FROM invite_links WHERE chat_id=?"
        if not include_revoked:
            sql += " AND revoked=0"
        rows = await self._fetchall(sql + " ORDER BY created_at DESC", (chat_id,))
        return [InviteLink.from_row(r) for r in rows]

    async def bump_invite_use(self, invite_link: str) -> None:
        await self._exec("UPDATE invite_links SET uses=uses+1 WHERE invite_link=?", (invite_link,))

    async def revoke_invite_link(self, invite_link: str) -> None:
        await self._exec("UPDATE invite_links SET revoked=1 WHERE invite_link=?", (invite_link,))

    # --------------------------------------------------------------------- kv
    async def kv_set(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = await self._fetchone("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    # ---------------------------------------------------------------- global
    async def global_stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, sql in {
            "chats": "SELECT COUNT(*) FROM chats",
            "tracking": "SELECT COUNT(*) FROM chats WHERE tracking_enabled=1",
            "active": "SELECT COUNT(*) FROM members WHERE status='active'",
            "expired": "SELECT COUNT(*) FROM members WHERE status='expired'",
            "total": "SELECT COUNT(*) FROM members",
            "pending": "SELECT COUNT(*) FROM pending_joins WHERE status='pending'",
            "invites": "SELECT COUNT(*) FROM invite_links WHERE revoked=0",
        }.items():
            row = await self._fetchone(sql)
            out[key] = int(row[0]) if row else 0
        return out

    def db_size_bytes(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0

    @staticmethod
    def copy_file(src: str, dest: str) -> None:
        shutil.copy2(src, dest)
