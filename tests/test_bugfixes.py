"""Regression tests for bugs found in the full code review."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import User

from bot.config import Settings
from bot.handlers import tracking
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler
from bot.utils.keyboards import invite_created_keyboard
from bot.utils.timeparse import ParseError, parse_expiry

TZ = ZoneInfo("Asia/Kolkata")


def make_user(uid: int, name: str = "User") -> User:
    return User(id=uid, is_bot=False, first_name=name)


def _admin(uid: int, status: str):
    m = MagicMock()
    m.status = status
    m.user = make_user(uid)
    return m


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m", reminder_hours=[72, 24, 1])
    bot = MagicMock()
    bot.id = 999
    sent = MagicMock()
    sent.message_id = 1
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.get_chat_administrators = AsyncMock(return_value=[_admin(1, "creator")])
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings)
    yield db, bot, service, scheduler
    await db.close()


async def _expired(db, chat_id, uid, hours_ago=2):
    await db.upsert_member(chat_id, uid, f"U{uid}", None, datetime.now(timezone.utc) - timedelta(hours=hours_ago))


# ---------------------------------------------------------------- scheduler
@pytest.mark.asyncio
async def test_paused_chat_does_not_starve_removal_queue(env):
    db, bot, _, scheduler = env
    await db.upsert_chat(-1, "Paused", "supergroup", None)
    await db.update_chat(-1, tracking_enabled=0)
    await db.upsert_chat(-2, "Live", "supergroup", None)
    for uid in range(100, 103):  # older expiries in the paused chat
        await _expired(db, -1, uid, hours_ago=10)
    await _expired(db, -2, 500, hours_ago=1)
    due = await db.expired_members(limit=2)
    assert [m.user_id for m in due] == [500]
    result = await scheduler.run_once()
    assert result["removed"] == 1


@pytest.mark.asyncio
async def test_grace_period_members_not_returned_until_grace_over(env):
    db, *_ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.update_chat(-1, grace_hours=24)
    await _expired(db, -1, 10, hours_ago=2)   # still in grace
    await _expired(db, -1, 11, hours_ago=30)  # grace over
    assert [m.user_id for m in await db.expired_members()] == [11]


@pytest.mark.asyncio
async def test_reminders_skip_muted_chats_and_page_through_all(env, monkeypatch):
    db, bot, _, scheduler = env
    import bot.services.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "REMINDER_PAGE", 2)
    await db.upsert_chat(-1, "Muted", "supergroup", None)
    await db.update_chat(-1, notify_user=0)
    await db.upsert_chat(-2, "Loud", "supergroup", None)
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    for uid in range(10, 15):
        await db.upsert_member(-1, uid, "m", None, soon - timedelta(minutes=1))
    for uid in range(20, 25):
        await db.upsert_member(-2, uid, "l", None, soon)
    assert await scheduler._process_reminders() == 5
    assert {c.args[0] for c in bot.send_message.call_args_list} == set(range(20, 25))


# ---------------------------------------------------------------- parsing
def test_parse_expiry_rejects_past_absolute_dates():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with pytest.raises(ParseError):
        parse_expiry("2025-01-31", TZ, now)
    assert parse_expiry("2026-12-31", TZ, now) > now


@pytest.mark.asyncio
async def test_invite_preset_in_the_past_falls_back_to_default(env):
    db, _, service, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await db.add_invite_link("https://t.me/+old", -1, "Old", "2020-01-01", 1)
    m = await service.track_join(chat, make_user(10), invite_link="https://t.me/+old")
    assert m.expires_at > datetime.now(timezone.utc) + timedelta(days=27)


def test_share_url_keeps_plus_in_private_invite_links():
    url = "https://t.me/+AbCd_123"
    share = invite_created_keyboard(-1, url).inline_keyboard[1][0].url
    assert parse_qs(urlparse(share).query)["url"] == [url]


# ---------------------------------------------------------------- rejoin
def _join_event(chat_id: int, user: User, invite: str | None = None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="G", username=None),
        new_chat_member=SimpleNamespace(user=user),
        from_user=user,
        invite_link=SimpleNamespace(invite_link=invite) if invite else None,
        via_join_request=False,
    )


@pytest.mark.asyncio
async def test_rejoin_after_renewal_keeps_decided_expiry(env):
    db, bot, service, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    decided = datetime.now(timezone.utc) + timedelta(days=200)
    await db.upsert_member(-1, 10, "A", None, decided, source="manual")
    await tracking.on_member_join(_join_event(-1, make_user(10)), bot, db, service, service.settings)
    m = await db.get_member(-1, 10)
    assert abs((m.expires_at - decided).total_seconds()) < 1
    assert await db.get_pending_for(chat.chat_id, 10) is None


@pytest.mark.asyncio
async def test_rejoin_through_preset_invite_link_still_applies_preset(env):
    db, bot, service, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.upsert_member(-1, 10, "A", None, datetime.now(timezone.utc) + timedelta(days=3))
    await db.add_invite_link("https://t.me/+year", -1, "Year", "1y", 1)
    await tracking.on_member_join(_join_event(-1, make_user(10), "https://t.me/+year"), bot, db, service, service.settings)
    m = await db.get_member(-1, 10)
    assert m.expires_at > datetime.now(timezone.utc) + timedelta(days=360)


# ---------------------------------------------------------------- non-members
@pytest.mark.asyncio
async def test_linked_channel_and_anonymous_posts_are_not_tracked(env):
    db, _, service, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    chat = SimpleNamespace(id=-1, type="supergroup")
    svc_msg = SimpleNamespace(chat=chat, from_user=User(id=777000, is_bot=False, first_name="Telegram"), sender_chat=None)
    anon_msg = SimpleNamespace(chat=chat, from_user=make_user(55), sender_chat=SimpleNamespace(id=-5))
    await tracking.on_group_message(svc_msg, db, service)
    await tracking.on_group_message(anon_msg, db, service)
    assert await db.get_member(-1, 777000) is None
    assert await db.get_member(-1, 55) is None


# ---------------------------------------------------------------- sync
@pytest.mark.asyncio
async def test_sync_does_not_mark_members_left_on_chat_level_error(env):
    db, bot, service, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    for uid in (10, 11):
        await db.upsert_member(-1, uid, "A", None, None)
    method = GetChatMember(chat_id=-1, user_id=10)
    bot.get_chat_member = AsyncMock(side_effect=TelegramBadRequest(method, "Bad Request: chat not found"))
    result = await service.sync_chat_members(chat)
    assert result == {"checked": 2, "gone": 0, "errors": 2}
    assert (await db.get_member(-1, 10)).status == "active"

    bot.get_chat_member = AsyncMock(side_effect=TelegramBadRequest(method, "Bad Request: user not found"))
    result = await service.sync_chat_members(chat)
    assert result["gone"] == 2
    assert (await db.get_member(-1, 10)).status == "left"
