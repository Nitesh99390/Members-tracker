from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from bot.services.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_chat_and_member_lifecycle(db: Database):
    chat = await db.upsert_chat(-100, "Test Group", "supergroup", None, added_by=1)
    assert chat.tracking_enabled and chat.auto_kick and chat.kick_mode == "kick"

    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=1)
    future = now + timedelta(days=3)

    await db.upsert_member(-100, 10, "Alice", "alice", past)
    await db.upsert_member(-100, 11, "Bob", None, future)
    await db.upsert_member(-100, 12, "Carol", "carol", None)

    assert await db.count_members(-100) == 3
    expired = await db.expired_members(now)
    assert [m.user_id for m in expired] == [10]

    expiring = await db.expiring_members(now + timedelta(days=7))
    assert [m.user_id for m in expiring] == [11]

    stats = await db.member_stats(-100)
    assert stats["active"] == 3 and stats["permanent"] == 1

    await db.set_member_status(-100, 10, "expired")
    assert await db.count_members(-100) == 2
    assert (await db.get_member(-100, 10)).status == "expired"

    # re-join resets status
    await db.upsert_member(-100, 10, "Alice", "alice", future)
    assert (await db.get_member(-100, 10)).status == "active"


@pytest.mark.asyncio
async def test_whitelist_and_context(db: Database):
    await db.upsert_chat(-200, "Channel", "channel", "chan", added_by=5)
    await db.add_whitelist(-200, 42, 5)
    assert await db.is_whitelisted(-200, 42)
    assert await db.list_whitelist(-200) == [42]
    await db.remove_whitelist(-200, 42)
    assert not await db.is_whitelisted(-200, 42)

    await db.set_context(5, -200)
    assert await db.get_context(5) == -200
    chats = await db.chats_for_admin(5)
    assert [c.chat_id for c in chats] == [-200]


@pytest.mark.asyncio
async def test_reminders_and_logs(db: Database):
    await db.upsert_chat(-300, "G", "group", None)
    await db.upsert_member(-300, 1, "X", None, datetime.now(timezone.utc) + timedelta(hours=5))
    await db.mark_reminder_sent(-300, 1, 24)
    await db.mark_reminder_sent(-300, 1, 72)
    m = await db.get_member(-300, 1)
    assert m.reminders_sent == {24, 72}

    await db.add_log(-300, 1, "join", "x", 9)
    logs = await db.recent_logs(-300)
    assert len(logs) == 1 and logs[0]["action"] == "join"

    await db.update_chat(-300, kick_mode="ban", default_duration="2w")
    chat = await db.get_chat(-300)
    assert chat.kick_mode == "ban" and chat.default_duration == "2w"


@pytest.mark.asyncio
async def test_search(db: Database):
    await db.upsert_chat(-400, "G", "group", None)
    await db.upsert_member(-400, 777, "John Doe", "johnd", None)
    assert [m.user_id for m in await db.search_members(-400, "@johnd")] == [777]
    assert [m.user_id for m in await db.search_members(-400, "777")] == [777]
