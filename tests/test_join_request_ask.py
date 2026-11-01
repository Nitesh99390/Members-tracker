"""Join requests: every subscriber request asks the owner how long they may stay."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio
from aiogram.types import User

from bot.config import Settings, _parse_request_mode
from bot.services.database import Database
from bot.services.membership import MembershipService

OWNER_ID = 1


def _admin(uid: int, status: str):
    m = MagicMock()
    m.status = status
    m.user = User(id=uid, is_bot=False, first_name=f"U{uid}")
    return m


def make_user(uid: int, name: str = "Sub", username: str | None = None) -> User:
    return User(id=uid, is_bot=False, first_name=name, username=username)


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m", ask_timeout_hours=24)
    bot = MagicMock()
    bot.id = 999
    sent = MagicMock()
    sent.message_id = 7000
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    bot.get_chat_administrators = AsyncMock(return_value=[_admin(OWNER_ID, "creator")])
    bot.approve_chat_join_request = AsyncMock()
    bot.decline_chat_join_request = AsyncMock()
    service = MembershipService(bot, db, settings)
    yield db, bot, service
    await db.close()


def test_env_parsing():
    assert _parse_request_mode(None) == 2
    assert _parse_request_mode("ask") == 2
    assert _parse_request_mode("AUTO") == 1
    assert _parse_request_mode("ignore") == 0
    assert _parse_request_mode("garbage") == 2


@pytest.mark.asyncio
async def test_new_chat_defaults_to_ask(env):
    db, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    assert chat.approve_requests == 2


@pytest.mark.asyncio
async def test_legacy_ignore_chats_migrated_once(tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            """
            CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, chat_type TEXT, username TEXT,
                default_duration TEXT, tracking_enabled INTEGER NOT NULL DEFAULT 1,
                auto_kick INTEGER NOT NULL DEFAULT 1, kick_mode TEXT NOT NULL DEFAULT 'kick',
                notify_user INTEGER NOT NULL DEFAULT 1, log_chat_id INTEGER,
                welcome_enabled INTEGER NOT NULL DEFAULT 0, welcome_text TEXT,
                approve_requests INTEGER NOT NULL DEFAULT 0, added_by INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO chats (chat_id, title, chat_type, approve_requests, created_at, updated_at)
                VALUES (-1, 'Old', 'supergroup', 0, '2024-01-01', '2024-01-01'),
                       (-2, 'Auto', 'supergroup', 1, '2024-01-01', '2024-01-01');
            """
        )
        await conn.commit()
    db = Database(str(path))
    await db.connect()
    assert (await db.get_chat(-1)).approve_requests == 2  # ignore → ask
    assert (await db.get_chat(-2)).approve_requests == 1  # explicit auto kept
    # owner deliberately switches back to "ignore" → must survive restarts
    await db.update_chat(-1, approve_requests=0)
    await db.close()
    db = Database(str(path))
    await db.connect()
    assert (await db.get_chat(-1)).approve_requests == 0
    await db.close()


@pytest.mark.asyncio
async def test_request_asks_owner_and_does_not_auto_approve(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "VIP", "supergroup", None)
    pending = await service.ask_about_join_request(chat, make_user(10, "Alice", "alice"), bio="paid")
    assert pending is not None and pending.source == "request"
    bot.approve_chat_join_request.assert_not_awaited()
    args, kwargs = bot.send_message.call_args
    assert args[0] == OWNER_ID
    assert "How long may they stay" in args[1] and "paid" in args[1]
    buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert f"jd:{pending.id}:default" in buttons and f"jr:{pending.id}" in buttons
    assert await db.get_member(-1, 10) is None  # not a member until approved

    ok, _ = await service.apply_decision(pending, "3m", OWNER_ID, "Owner")
    assert ok
    bot.approve_chat_join_request.assert_awaited_once_with(-1, 10)
    m = await db.get_member(-1, 10)
    assert timedelta(days=85) < m.expires_at - datetime.now(timezone.utc) < timedelta(days=95)


@pytest.mark.asyncio
async def test_repeated_request_supersedes_old_prompt(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    first = await service.ask_about_join_request(chat, make_user(10))
    second = await service.ask_about_join_request(chat, make_user(10))
    assert second.id != first.id
    assert (await db.get_pending(first.id)).status == "cancelled"
    assert bot.edit_message_text.await_count == 1  # old buttons retired


@pytest.mark.asyncio
async def test_whitelisted_request_approved_lifetime(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await db.add_whitelist(-1, 10, 1)
    assert await service.ask_about_join_request(chat, make_user(10)) is None
    bot.approve_chat_join_request.assert_awaited_once_with(-1, 10)
    assert (await db.get_member(-1, 10)).expires_at is None
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_undeliverable_prompt_leaves_request_pending(env):
    db, bot, service = env
    bot.send_message = AsyncMock(return_value=None)
    service.safe_send = AsyncMock(return_value=None)
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    assert await service.ask_about_join_request(chat, make_user(10)) is None
    bot.approve_chat_join_request.assert_not_awaited()
    assert await db.count_pending(-1) == 0


@pytest.mark.asyncio
async def test_timeout_never_auto_approves_request(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    pending = await service.ask_about_join_request(chat, make_user(10))
    await db._exec(
        "UPDATE pending_joins SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(), pending.id),
    )
    assert await service.expire_stale_prompts() == 1
    bot.approve_chat_join_request.assert_not_awaited()
    assert await db.get_member(-1, 10) is None
    text = bot.edit_message_text.call_args.args[0]
    assert "unapproved" in text and "Join request" in text
