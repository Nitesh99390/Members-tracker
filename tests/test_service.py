from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import User

from bot.config import Settings
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler

OWNER_ID = 1
ADMIN_ID = 2


def _admin(uid: int, status: str):
    m = MagicMock()
    m.status = status
    m.user = make_user(uid, f"U{uid}")
    return m


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    settings = Settings(
        bot_token="x", super_admins=[1], default_duration="1m", reminder_hours=[72, 24, 1], ask_timeout_hours=24
    )
    bot = MagicMock()
    bot.id = 999
    sent = MagicMock()
    sent.message_id = 5000
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.get_chat_administrators = AsyncMock(
        return_value=[_admin(OWNER_ID, "creator"), _admin(ADMIN_ID, "administrator")]
    )
    bot.approve_chat_join_request = AsyncMock()
    bot.decline_chat_join_request = AsyncMock()
    link = MagicMock()
    link.invite_link = "https://t.me/+preset"
    bot.create_chat_invite_link = AsyncMock(return_value=link)
    bot.revoke_chat_invite_link = AsyncMock()
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings)
    yield db, bot, service, scheduler, settings
    await db.close()


def make_user(uid: int, name: str = "User", username: str | None = None) -> User:
    return User(id=uid, is_bot=False, first_name=name, username=username)


@pytest.mark.asyncio
async def test_track_join_uses_default_duration(env):
    db, bot, service, _, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    await db.update_chat(-1, ask_on_join=0)
    chat = await db.get_chat(-1)
    before = datetime.now(timezone.utc)
    member = await service.track_join(chat, make_user(10, "Alice"))
    assert member is not None and member.expires_at is not None
    # 1 month from now -> between 28 and 31 days
    delta = member.expires_at - before
    assert timedelta(days=27) < delta < timedelta(days=32)
    # ask disabled -> no prompt created
    assert await db.count_pending(-1) == 0
    bot.send_message.assert_not_awaited()


# --------------------------------------------------------------- join prompt
@pytest.mark.asyncio
async def test_join_prompts_owner_and_decision_applies(env):
    db, bot, service, _, settings = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None, added_by=ADMIN_ID)
    member = await service.track_join(chat, make_user(10, "Alice", "alice"))
    assert member.expires_at is not None  # default applied immediately as a safety net

    # owner (creator) was resolved from Telegram and DM'd once
    assert (await db.get_chat(-1)).owner_id == OWNER_ID
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == OWNER_ID
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None
    pending = await db.get_pending_for(-1, 10)
    assert pending is not None and pending.source == "join"

    # owner picks 3 months
    ok, outcome = await service.apply_join_decision(pending, "3m", OWNER_ID, "Owner")
    assert ok, outcome
    m = await db.get_member(-1, 10)
    assert timedelta(days=85) < (m.expires_at - datetime.now(timezone.utc)) < timedelta(days=95)
    assert (await db.get_pending(pending.id)).status == "decided"
    # the prompt message was edited to show the result (buttons removed)
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None

    # second admin clicking afterwards is rejected
    ok2, msg2 = await service.apply_join_decision(pending, "1w", ADMIN_ID)
    assert not ok2 and "Already" in msg2


@pytest.mark.asyncio
async def test_join_prompt_goes_to_all_admins_when_configured(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await db.set_chat_admins(-1, [OWNER_ID, ADMIN_ID])
    await db.update_chat(-1, ask_target="admins")
    chat = await db.get_chat(-1)
    await service.track_join(chat, make_user(10))
    targets = sorted(c.args[0] for c in bot.send_message.await_args_list)
    assert targets == [OWNER_ID, ADMIN_ID]


@pytest.mark.asyncio
async def test_join_decision_lifetime_and_remove(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await service.track_join(chat, make_user(10))
    pending = await db.get_pending_for(-1, 10)
    ok, _ = await service.apply_join_decision(pending, "never", OWNER_ID)
    assert ok and (await db.get_member(-1, 10)).expires_at is None

    await service.track_join(chat, make_user(11))
    pending = await db.get_pending_for(-1, 11)
    ok, _ = await service.apply_join_decision(pending, "remove", OWNER_ID)
    assert ok
    bot.ban_chat_member.assert_awaited_once_with(-1, 11)
    assert (await db.get_member(-1, 11)).status == "manual"


@pytest.mark.asyncio
async def test_join_decision_invalid_text_keeps_pending(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await service.track_join(chat, make_user(10))
    pending = await db.get_pending_for(-1, 10)
    ok, msg = await service.apply_join_decision(pending, "banana", OWNER_ID)
    assert not ok and "Invalid" in msg
    assert (await db.get_pending(pending.id)).status == "pending"


@pytest.mark.asyncio
async def test_prompt_undeliverable_keeps_default_silently(env):
    db, bot, service, _, _ = env
    bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message="blocked"))
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    member = await service.track_join(chat, make_user(10))
    assert member.expires_at is not None
    assert await db.count_pending(-1) == 0  # cancelled as undeliverable


@pytest.mark.asyncio
async def test_stale_prompt_times_out(env):
    db, bot, service, scheduler, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    await service.track_join(chat, make_user(10))
    pending = await db.get_pending_for(-1, 10)
    # age the prompt artificially
    await db._exec(
        "UPDATE pending_joins SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(), pending.id),
    )
    result = await scheduler.run_once()
    assert result["prompts_expired"] == 1
    assert (await db.get_pending(pending.id)).status == "expired"
    bot.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_leaving_cancels_prompt(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    user = make_user(10)
    await service.track_join(chat, user)
    pending = await db.get_pending_for(-1, 10)
    await service.track_leave(chat, user, "left")
    assert (await db.get_pending(pending.id)).status == "cancelled"
    assert (await db.get_member(-1, 10)).status == "left"


# --------------------------------------------------------------- invite links
@pytest.mark.asyncio
async def test_invite_link_preset_skips_prompt(env):
    db, bot, service, _, _ = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None)
    url, label = await service.create_invite_link(chat, "1y", "Gold", OWNER_ID)
    assert url == "https://t.me/+preset" and label == "Gold"
    bot.create_chat_invite_link.assert_awaited_once()

    member = await service.track_join(chat, make_user(10), invite_link=url)
    delta = member.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=360) < delta < timedelta(days=370)
    assert member.source == "invite:Gold"
    assert await db.count_pending(-1) == 0
    bot.send_message.assert_not_awaited()
    assert (await db.get_invite_link(url)).uses == 1

    bad_url, err = await service.create_invite_link(chat, "xyz", None, OWNER_ID)
    assert bad_url is None and "Invalid" in err


# -------------------------------------------------------------- join requests
@pytest.mark.asyncio
async def test_request_decision_approves_with_duration(env):
    db, bot, service, _, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    pending = await db.create_pending(-1, 10, "Alice", "alice", source="request")
    ok, outcome = await service.apply_request_decision(pending, "2w", OWNER_ID, "Owner")
    assert ok, outcome
    bot.approve_chat_join_request.assert_awaited_once_with(-1, 10)
    m = await db.get_member(-1, 10)
    assert m.source == "request" and timedelta(days=13) < (m.expires_at - datetime.now(timezone.utc)) < timedelta(days=15)

    pending2 = await db.create_pending(-1, 11, "Bob", None, source="request")
    ok, _ = await service.apply_request_decision(pending2, "remove", OWNER_ID)
    assert ok
    bot.decline_chat_join_request.assert_awaited_once_with(-1, 11)
    assert await db.get_member(-1, 11) is None


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
    assert m.fail_count == 1


@pytest.mark.asyncio
async def test_scheduler_never_removes_admins(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.set_chat_admins(-1, [10])
    await db.upsert_member(-1, 10, "Admin", None, datetime.now(timezone.utc) - timedelta(seconds=5))
    await scheduler.run_once()
    bot.ban_chat_member.assert_not_awaited()
    m = await db.get_member(-1, 10)
    assert m.status == "active" and m.expires_at is None


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
