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
    await db.mark_reminder_sent(-300, 1, [24])
    await db.mark_reminder_sent(-300, 1, [72])
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
    found = await db.find_member_by_username(-400, "@JohnD")
    assert found is not None and found.user_id == 777


@pytest.mark.asyncio
async def test_pending_joins_lifecycle(db: Database):
    await db.upsert_chat(-500, "G", "supergroup", None, added_by=1)
    p = await db.create_pending(-500, 10, "Alice", "alice", source="join")
    assert p.status == "pending" and p.source == "join"
    assert await db.count_pending(-500) == 1
    assert (await db.get_pending_for(-500, 10)).id == p.id

    # creating a second one for the same user cancels the first
    p2 = await db.create_pending(-500, 10, "Alice", "alice")
    assert p2.id != p.id
    assert (await db.get_pending(p.id)).status == "cancelled"
    assert await db.count_pending(-500) == 1

    await db.add_prompt_message(p2.id, 1, 111)
    await db.add_prompt_message(p2.id, 2, 222)
    assert sorted(await db.prompt_messages(p2.id)) == [(1, 111), (2, 222)]

    # only the first resolve wins
    assert await db.resolve_pending(p2.id, 1, "1m") is True
    assert await db.resolve_pending(p2.id, 2, "2m") is False
    resolved = await db.get_pending(p2.id)
    assert resolved.status == "decided" and resolved.decision == "1m" and resolved.decided_by == 1
    assert await db.count_pending(-500) == 0

    # stale detection
    p3 = await db.create_pending(-500, 11, "Bob", None)
    assert [x.id for x in await db.stale_pending(datetime.now(timezone.utc) + timedelta(seconds=1))] == [p3.id]
    assert await db.stale_pending(datetime.now(timezone.utc) - timedelta(hours=1)) == []


@pytest.mark.asyncio
async def test_invite_links_and_kv(db: Database):
    await db.upsert_chat(-600, "G", "supergroup", None)
    link = await db.add_invite_link("https://t.me/+abc", -600, "Gold", "3m", 1)
    assert link.duration == "3m" and link.uses == 0 and not link.revoked
    await db.bump_invite_use("https://t.me/+abc")
    assert (await db.get_invite_link("https://t.me/+abc")).uses == 1
    assert len(await db.list_invite_links(-600)) == 1
    await db.revoke_invite_link("https://t.me/+abc")
    assert await db.list_invite_links(-600) == []
    assert len(await db.list_invite_links(-600, include_revoked=True)) == 1

    await db.kv_set("k", "v1")
    await db.kv_set("k", "v2")
    assert await db.kv_get("k") == "v2"
    assert await db.kv_get("missing", "d") == "d"


@pytest.mark.asyncio
async def test_migration_adds_columns_to_old_schema(tmp_path):
    """A database created by the first release must be upgraded transparently."""
    import aiosqlite

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
            INSERT INTO chats (chat_id, title, chat_type, created_at, updated_at)
                VALUES (-1, 'Old', 'supergroup', '2024-01-01', '2024-01-01');
            """
        )
        await conn.commit()
    db = Database(str(path))
    await db.connect()
    chat = await db.get_chat(-1)
    assert chat is not None and chat.ask_on_join is None and chat.ask_target == "owner"
    await db.update_chat(-1, ask_on_join=1, owner_id=42)
    chat = await db.get_chat(-1)
    assert chat.ask_on_join is True and chat.owner_id == 42
    await db.close()


@pytest.mark.asyncio
async def test_backup_and_delete_chat(db: Database, tmp_path):
    await db.upsert_chat(-700, "G", "supergroup", None)
    await db.upsert_member(-700, 1, "A", None, None)
    dest = tmp_path / "copy.db"
    await db.backup_to(str(dest))
    assert dest.exists() and dest.stat().st_size > 0
    assert await db.healthcheck()

    await db.delete_chat(-700)
    assert await db.get_chat(-700) is None
    assert await db.get_member(-700, 1) is None
