from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import User

from bot.config import Settings
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m", reminder_hours=[72, 24, 1])
    bot = MagicMock()
    bot.id = 999
    bot.send_message = AsyncMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings)
    yield db, bot, service, scheduler, settings
    await db.close()


def make_user(uid: int, name: str = "User", username: str | None = None) -> User:
    return User(id=uid, is_bot=False, first_name=name, username=username)


@pytest.mark.asyncio
async def test_track_join_uses_default_duration(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    before = datetime.now(timezone.utc)
    member = await service.track_join(chat, make_user(10, "Alice"))
    assert member is not None and member.expires_at is not None
    # 1 month from now -> between 28 and 31 days
    delta = member.expires_at - before
    assert timedelta(days=27) < delta < timedelta(days=32)


@pytest.mark.asyncio
async def test_whitelisted_join_is_permanent(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await db.add_whitelist(-1, 10, 1)
    member = await service.track_join(chat, make_user(10))
    assert member.expires_at is None


@pytest.mark.asyncio
async def test_scheduler_kicks_expired_and_unbans_in_kick_mode(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.update_chat(-1, log_chat_id=-555)
    await db.upsert_member(-1, 10, "Old", None, datetime.now(timezone.utc) - timedelta(seconds=5))
    await db.upsert_member(-1, 11, "Fresh", None, datetime.now(timezone.utc) + timedelta(days=1))

    result = await scheduler.run_once()
    assert result["removed"] == 1
    bot.ban_chat_member.assert_awaited_once_with(-1, 10)
    bot.unban_chat_member.assert_awaited_once()
    assert (await db.get_member(-1, 10)).status == "expired"
    assert (await db.get_member(-1, 11)).status == "active"
    # log + DM to user
    assert bot.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_scheduler_ban_mode_does_not_unban(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.update_chat(-1, kick_mode="ban", notify_user=0)
    await db.upsert_member(-1, 10, "Old", None, datetime.now(timezone.utc) - timedelta(seconds=5))
    await scheduler.run_once()
    bot.ban_chat_member.assert_awaited_once()
    bot.unban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_autokick_off_only_marks(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.update_chat(-1, auto_kick=0)
    await db.upsert_member(-1, 10, "Old", None, datetime.now(timezone.utc) - timedelta(seconds=5))
    await scheduler.run_once()
    bot.ban_chat_member.assert_not_awaited()
    assert (await db.get_member(-1, 10)).status == "expired"


@pytest.mark.asyncio
async def test_remove_failure_reschedules(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.upsert_member(-1, 10, "Old", None, datetime.now(timezone.utc) - timedelta(seconds=5))
    bot.ban_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: not enough rights")
    )
    result = await scheduler.run_once()
    assert result["removed"] == 0
    m = await db.get_member(-1, 10)
    assert m.status == "active" and m.expires_at > datetime.now(timezone.utc) + timedelta(minutes=50)


@pytest.mark.asyncio
async def test_reminders_sent_once(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.upsert_member(-1, 10, "Soon", None, datetime.now(timezone.utc) + timedelta(hours=20))
    r1 = await scheduler.run_once()
    r2 = await scheduler.run_once()
    assert r1["reminded"] == 1 and r2["reminded"] == 0
    m = await db.get_member(-1, 10)
    assert {24, 72} <= m.reminders_sent and 1 not in m.reminders_sent


@pytest.mark.asyncio
async def test_extend_from_current_expiry_and_absolute(env):
    db, bot, service, _, settings = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    base = datetime.now(timezone.utc) + timedelta(days=10)
    member = await db.upsert_member(-1, 10, "A", None, base)
    new = await service.extend_member(chat, member, "5d", 1)
    assert abs((new - (base + timedelta(days=5))).total_seconds()) < 2

    member = await db.get_member(-1, 10)
    new = await service.extend_member(chat, member, "2099-01-01", 1)
    assert new.year == 2099 or new.year == 2098  # tz conversion

    new = await service.extend_member(chat, member, "never", 1)
    assert new is None
    assert (await db.get_member(-1, 10)).expires_at is None
