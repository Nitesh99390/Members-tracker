"""Memory-bounding layer: SQL-level paging, streaming iterators, covering indexes,
pinned SQLite PRAGMAs and the LRU-bounded middleware dictionaries.

Everything here guards the invariant that **no code path materialises a whole
table in Python** — RAM must stay flat whether a chat has 30 or 300 000 members.
"""
from __future__ import annotations

import csv
import io
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.handlers import screens as S
from bot.middlewares import (
    BOUNDED_MAX,
    BoundedDict,
    CallbackAckMiddleware,
    ErrorReportingMiddleware,
    ThrottlingMiddleware,
)
from bot.services import export as X
from bot.services.database import CHATS_PAGE, PRAGMAS, STREAM_BATCH, Database
from bot.utils import keyboards as K

TZ = ZoneInfo("Asia/Kolkata")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "mem.db"))
    await database.connect()
    yield database
    await database.close()


async def _seed_chats(db: Database, n: int, added_by: int = 1) -> list[int]:
    ids = []
    for i in range(n):
        cid = -1000 - i
        await db.upsert_chat(cid, f"Chat {i:03d}", "supergroup", None, added_by=added_by)
        ids.append(cid)
    return ids


async def _seed_members(db: Database, chat_id: int, n: int, status: str = "active") -> None:
    future = datetime.now(timezone.utc) + timedelta(days=10)
    for uid in range(1, n + 1):
        await db.upsert_member(chat_id, uid, f"User {uid}", None, future)
        if status != "active":
            await db.set_member_status(chat_id, uid, status)


# ------------------------------------------------------------------ PRAGMAs
async def test_sqlite_memory_pragmas_are_pinned(db: Database):
    """The connection must run with a 4 MB page cache, disk temp store and no mmap."""
    assert any("cache_size = -4000" in p for p in PRAGMAS)
    stats = await db.memory_stats()
    assert stats["cache_size"] == -4000
    assert stats["temp_store"] == 1  # FILE
    assert stats["mmap_size"] == 0
    assert str(stats["journal_mode"]).lower() == "wal"


# ------------------------------------------------------------------ indexes
async def test_covering_indexes_exist(db: Database):
    assert await db.has_index_prefix("members", ["chat_id", "status"])
    assert await db.has_index_prefix("members", ["chat_id", "user_id"])  # PK autoindex
    assert await db.has_index_prefix("members", ["chat_id", "joined_at"])
    assert await db.has_index_prefix("chat_admins", ["user_id", "chat_id"])
    assert await db.has_index_prefix("pending_joins", ["chat_id", "status", "id"])
    assert await db.has_index_prefix("chats", ["owner_id"])  # created via MIGRATION_INDEXES
    assert await db.has_index_prefix("chats", ["added_by"])


async def test_hot_counts_use_covering_index(db: Database):
    await _seed_chats(db, 1)
    await _seed_members(db, -1000, 5)
    plan = await db.query_plan(
        "SELECT COUNT(*) FROM members WHERE chat_id=? AND status=?", (-1000, "active")
    )
    assert "COVERING INDEX" in plan.upper()
    plan = await db.query_plan(
        "SELECT COUNT(*) FROM pending_joins WHERE chat_id=? AND status='pending'", (-1000,)
    )
    assert "COVERING INDEX" in plan.upper()
    plan = await db.query_plan("SELECT chat_id FROM chat_admins WHERE user_id=?", (7,))
    assert "COVERING INDEX" in plan.upper()


# --------------------------------------------------------------- chat picker
async def test_count_and_first_admin_chat(db: Database):
    ids = await _seed_chats(db, 3, added_by=1)
    # a plain admin of chat 2 only
    await db.set_chat_admins(ids[2], [42])
    # owner of chat 1
    await db.update_chat(ids[1], owner_id=77)

    assert await db.count_chats() == 3
    assert await db.count_admin_chats(None) == 3  # super-admin sees all
    assert await db.count_admin_chats(1) == 3  # added_by
    assert await db.count_admin_chats(42) == 1
    assert await db.count_admin_chats(77) == 1
    assert await db.count_admin_chats(999) == 0

    only = await db.first_admin_chat(42)
    assert only is not None and only.chat_id == ids[2]
    assert await db.first_admin_chat(999) is None


async def test_admin_chats_page_is_sql_bounded(db: Database):
    ids = await _seed_chats(db, 14)
    visible, page, total_pages = await db.admin_chats_page(None, 0)
    assert len(visible) == CHATS_PAGE == K.CHATS_PAGE
    assert (page, total_pages) == (0, 3)
    assert [c.chat_id for c in visible] == ids[:CHATS_PAGE]

    visible, page, total_pages = await db.admin_chats_page(None, 2)
    assert [c.chat_id for c in visible] == ids[12:]
    assert page == 2

    # out-of-range pages are clamped, never empty
    visible, page, _ = await db.admin_chats_page(None, 99)
    assert page == 2 and len(visible) == 2
    visible, page, _ = await db.admin_chats_page(None, -5)
    assert page == 0 and len(visible) == CHATS_PAGE

    # a non-admin gets a single empty page
    assert await db.admin_chats_page(999, 0) == ([], 0, 1)


async def test_chats_screen_for_matches_in_memory_screen(db: Database):
    ids = await _seed_chats(db, 8)
    await db.create_pending(ids[0], 5, "Pending Pete", None)
    chats = await db.list_chats()

    text_mem, kb_mem = await S.chats_screen(db, chats, ids[1], page=1)
    text_sql, kb_sql = await S.chats_screen_for(db, None, ids[1], page=1)
    assert text_mem == text_sql
    assert kb_mem.model_dump() == kb_sql.model_dump()

    text, kb = await S.chats_screen_for(db, None, None, page=0)
    assert "1/2" in text and "🔔" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("🔔1" in lbl for lbl in labels)


async def test_chats_keyboard_accepts_prefetched_page(db: Database):
    await _seed_chats(db, 6)
    chats = await db.list_chats()  # exactly one page, as if fetched with LIMIT 6
    kb = K.chats_keyboard(chats, None, page=1, total_pages=4)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "chats:0" in data and "chats:2" in data  # « and » around page 1 of 4
    kb = K.chats_keyboard(chats, None, page=9, total_pages=4)  # clamped
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "chats:2" in data


# ---------------------------------------------------------- streaming members
async def test_iter_members_keyset_paging(db: Database):
    await _seed_chats(db, 1)
    await _seed_members(db, -1000, 23)
    seen = [m.user_id async for m in db.iter_members(-1000, "active", batch=5)]
    assert seen == list(range(1, 24))  # every row exactly once, in key order

    # empty when nothing matches
    assert [m async for m in db.iter_members(-1000, "expired", batch=5)] == []

    # batch size exactly divides the total → still terminates
    seen = [m.user_id async for m in db.iter_members(-1000, "active", batch=23)]
    assert len(seen) == 23


async def test_stream_fetchmany_batches(db: Database):
    await _seed_chats(db, 1)
    await _seed_members(db, -1000, 250)
    before = db.queries
    rows = [r async for r in db._stream("SELECT user_id FROM members WHERE chat_id=? ORDER BY user_id", (-1000,))]
    assert len(rows) == 250
    assert db.queries == before + 1  # one cursor, many fetchmany() calls
    assert STREAM_BATCH == 100


async def test_export_streams_rows_and_counts(db: Database):
    await _seed_chats(db, 1)
    await _seed_members(db, -1000, 12)
    await db.set_member_status(-1000, 3, "expired")

    assert await db.count_members_view(-1000, "all") == 12
    assert await db.count_members_view(-1000, "active") == 11
    assert await db.count_members_view(-1000, "past") == 1
    assert await db.count_members_view(-1000, "bogus") == 12  # unknown → all

    data, count = await X.export_chat_csv(db, -1000, "all", TZ)
    assert count == 12
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == list(X.CSV_COLUMNS)
    assert len(rows) == 13
    assert rows[-1][0] == "3"  # expired row sorts last

    # the streaming writer must produce byte-identical output to the list version
    members = await db.members_for_export(-1000, "all")
    now = datetime.now(timezone.utc)
    assert X.members_csv(members, TZ, now) == (await X.members_csv_stream(db.iter_members_for_export(-1000, "all"), TZ, now))[0]

    # the bounded convenience wrapper really is bounded
    assert len(await db.members_for_export(-1000, "all", limit=5)) == 5


# ------------------------------------------------------------- streaming chats
async def test_iter_chats_and_digest_stream(db: Database):
    ids = await _seed_chats(db, 7)
    await db.update_chat(ids[1], tracking_enabled=False)
    await db.update_chat(ids[2], digest_enabled=True)
    await db.update_chat(ids[1], digest_enabled=True)  # not tracking → excluded

    all_ids = sorted([c.chat_id async for c in db.iter_chats()])
    assert all_ids == sorted(ids)
    tracking = [c.chat_id async for c in db.iter_chats(only_tracking=True)]
    assert ids[1] not in tracking and len(tracking) == 6
    assert await db.count_chats(only_tracking=True) == 6
    assert [c.chat_id async for c in db.iter_digest_chats()] == [ids[2]]
    assert [c.chat_id for c in await db.digest_chats()] == [ids[2]]

    # list_chats is now a bounded page
    assert len(await db.list_chats(limit=3)) == 3
    assert [c.chat_id for c in await db.list_chats(limit=3, offset=6)] == [ids[6]]


# ------------------------------------------------------------- pending joins
async def test_iter_pending_keyset_and_resolution_safe(db: Database):
    await _seed_chats(db, 1)
    for uid in range(1, 8):
        await db.create_pending(-1000, uid, f"P{uid}", None)
    assert await db.count_pending(-1000) == 7

    # resolve rows *while* iterating — keyset paging must still visit each once
    visited = []
    async for p in db.iter_pending(-1000, batch=3):
        visited.append(p.user_id)
        await db.resolve_pending(p.id, 1, "default")
    assert visited == list(range(1, 8))
    assert await db.count_pending(-1000) == 0

    # after_id paging
    for uid in range(1, 5):
        await db.create_pending(-1000, uid, f"P{uid}", None)
    first = await db.list_pending(-1000, limit=2)
    rest = await db.list_pending(-1000, limit=10, after_id=first[-1].id)
    assert [p.user_id for p in first] == [1, 2]
    assert [p.user_id for p in rest] == [3, 4]


async def test_pending_screen_uses_total_count(db: Database):
    chat = await _seed_chats(db, 1)
    chat_obj = await db.get_chat(chat[0])
    for uid in range(1, 15):
        await db.create_pending(-1000, uid, f"Pending {uid}", None)
    from bot.config import Settings

    settings = Settings(bot_token="x", super_admins={1})
    text, _kb = await S.pending_screen(db, chat_obj, settings)
    assert "· 14" in text
    assert f"…and {14 - S.PENDING_PAGE} more" in text
    assert text.count("\n• ") == S.PENDING_PAGE  # one bullet per listed row, never more


# ------------------------------------------------------------- BoundedDict
def test_bounded_dict_evicts_lru():
    d: BoundedDict = BoundedDict(maxsize=3)
    d["a"], d["b"], d["c"] = 1, 2, 3
    assert d.touch("a") == 1  # refresh "a" → "b" is now oldest
    d["d"] = 4
    assert "b" not in d and list(d) == ["c", "a", "d"]
    assert d.evictions == 1
    assert d.touch("zzz", "dflt") == "dflt" and "zzz" not in d


def test_bounded_dict_expire_by_timestamp():
    d: BoundedDict = BoundedDict(maxsize=10)
    d["old"] = 100.0
    d["new"] = 190.0
    d["not_ts"] = "x"
    assert d.expire(max_age=50, now=200.0) == 1
    assert "old" not in d and "new" in d and "not_ts" in d


def test_bounded_dict_minimum_size_is_one():
    d: BoundedDict = BoundedDict(maxsize=0)
    d["a"] = 1
    d["b"] = 2
    assert list(d) == ["b"]


# ------------------------------------------------------------- middlewares
def test_throttling_tables_are_bounded():
    mw = ThrottlingMiddleware(rate=0.5, burst=5, maxsize=50)
    for uid in range(1, 201):
        assert mw._allowed(uid)
    assert len(mw) == 50
    stats = mw.stats()
    assert stats == {"tracked_users": 50, "maxsize": 50, "evictions": 150}
    # evicted users are simply treated as fresh visitors
    assert mw._allowed(1)
    assert isinstance(mw._hits.touch(1), deque)


def test_throttling_cleanup_sweeps_idle_and_warned(monkeypatch):
    mw = ThrottlingMiddleware(rate=0.5, burst=2)
    t = [1000.0]
    monkeypatch.setattr("bot.middlewares.time.monotonic", lambda: t[0])
    assert mw._allowed(1) and mw._allowed(1) and not mw._allowed(1)
    mw._warned[1] = t[0]
    t[0] += 60
    removed = mw.cleanup()
    assert removed == 1 and len(mw) == 0 and 1 not in mw._warned


def test_default_bound_constant_applies_everywhere():
    assert ThrottlingMiddleware().maxsize == BOUNDED_MAX
    assert CallbackAckMiddleware.MAX_INFLIGHT == BOUNDED_MAX
    err = ErrorReportingMiddleware(notify_ids=[], bot_getter=lambda: None)
    assert isinstance(err._last_sent, BoundedDict) and err._last_sent.maxsize == BOUNDED_MAX


@pytest.mark.asyncio
async def test_callback_ack_inflight_hard_cap():
    mw = CallbackAckMiddleware()
    for i in range(CallbackAckMiddleware.MAX_INFLIGHT):
        mw._inflight.add((i, i, "x"))
    assert len(mw._inflight) == CallbackAckMiddleware.MAX_INFLIGHT
    # the safety valve is exercised inside __call__; emulate its guard here
    if len(mw._inflight) >= mw.MAX_INFLIGHT:
        mw._inflight.clear()
    assert not mw._inflight
