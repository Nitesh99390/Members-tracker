"""Screens render text + keyboard together; DB filtered views back the tabs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from bot.config import Settings
from bot.handlers import screens as S
from bot.services.database import Database
from bot.utils import ui


# ---------------------------------------------------------------- ui helpers
def test_clip_normalises_whitespace_and_cuts():
    assert ui.clip("  hello   world ", 20) == "hello world"
    assert ui.clip("abcdefghij", 5) == "abcd…"
    assert ui.clip("abcdefghij", 1) == "a"
    assert ui.clip(None, 5) == ""
    assert ui.clip("multi\nline\tname", 40) == "multi line name"


def test_page_label_is_one_based_and_clamped():
    assert ui.page_label(0, 3) == "1 / 3"
    assert ui.page_label(5, 3) == "3 / 3"
    assert ui.page_label(-2, 0) == "1 / 1"


def test_short_delta_buckets():
    assert ui.short_delta(timedelta(seconds=-5)) == "exp"
    assert ui.short_delta(timedelta(minutes=3)) == "3m"
    assert ui.short_delta(timedelta(seconds=10)) == "1m"
    assert ui.short_delta(timedelta(hours=5, minutes=10)) == "5h"
    assert ui.short_delta(timedelta(days=12)) == "12d"
    assert ui.short_delta(timedelta(days=75)) == "2mo"
    assert ui.short_delta(timedelta(days=800)) == "2y"


def test_urgency_glyphs():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ui.urgency(None, now) == "♾"
    assert ui.urgency(now - timedelta(minutes=1), now) == "🔴"
    assert ui.urgency(now + timedelta(hours=3), now) == "🟠"
    assert ui.urgency(now + timedelta(days=3), now) == "🟡"
    assert ui.urgency(now + timedelta(days=30), now) == "🟢"
    # naive datetimes are treated as UTC
    assert ui.urgency(datetime(2026, 1, 3), now) == "🟡"


def test_progress_bar():
    assert ui.progress_bar(0, 0) == "▱" * 10
    assert ui.progress_bar(5, 10) == "▰▰▰▰▰▱▱▱▱▱"
    assert ui.progress_bar(10, 10, width=4) == "▰▰▰▰"
    assert ui.progress_bar(20, 10, width=4) == "▰▰▰▰"  # never overflows


# ------------------------------------------------------------------ fixtures
@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "screens.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m")
    chat = await db.upsert_chat(-100, "VIP Club", "supergroup", None, added_by=1)
    now = datetime.now(timezone.utc)
    await db.upsert_member(-100, 10, "Alice Long Name", "alice", now + timedelta(days=30))
    await db.upsert_member(-100, 11, "Bob", None, now + timedelta(days=2))  # expiring soon
    await db.upsert_member(-100, 12, "Carol", "carol", None)  # lifetime
    await db.upsert_member(-100, 13, "Dan", None, now - timedelta(hours=1))
    await db.set_member_status(-100, 13, "expired")
    yield db, chat, settings
    await db.close()


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _data(markup) -> list[str | None]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ------------------------------------------------------------------ DB views
@pytest.mark.asyncio
async def test_member_views_and_counts(env):
    db, _chat, _settings = env
    counts = await db.member_view_counts(-100)
    assert counts == {"active": 3, "soon": 1, "lifetime": 1, "past": 1}

    rows, total = await db.list_members_view(-100, "active")
    assert total == 3 and [m.user_id for m in rows] == [11, 10, 12]  # soonest first, lifetime last

    rows, total = await db.list_members_view(-100, "soon")
    assert total == 1 and rows[0].user_id == 11

    rows, total = await db.list_members_view(-100, "past")
    assert total == 1 and rows[0].status == "expired"

    rows, total = await db.list_members_view(-100, "all", query="@ali")
    assert total == 1 and rows[0].user_id == 10
    rows, total = await db.list_members_view(-100, "all", query="1")  # matches ids 10-13
    assert total == 4

    # paging
    rows, total = await db.list_members_view(-100, "active", limit=2, offset=2)
    assert total == 3 and len(rows) == 1

    # unknown view falls back to active
    _rows, total = await db.list_members_view(-100, "bogus")
    assert total == 3


@pytest.mark.asyncio
async def test_recent_logs_page(env):
    db, _chat, _settings = env
    for i in range(20):
        await db.add_log(-100, None, f"a{i}", None, 1)
    first = await db.recent_logs_page(-100, 15, 0)
    second = await db.recent_logs_page(-100, 15, 15)
    assert len(first) == 15 and len(second) == 5
    assert first[0]["action"] == "a19" and second[-1]["action"] == "a0"


# ------------------------------------------------------------------- screens
@pytest.mark.asyncio
async def test_members_screen_tabs_rows_and_pager(env):
    db, chat, settings = env
    text, kb = await S.members_screen(db, chat, settings, "active", 0)
    assert "Active members — VIP Club" in text and "· 3" in text
    assert "Alice Long Name" in text and "Tap a row" in text
    texts = _buttons(kb)
    assert texts[0] == "▸ 🟢 Active 3"
    assert "⏰ Expiring 1" in texts and "📁 Past 1" in texts
    assert "member:-100:11:active:0" in _data(kb)
    assert "1 / 1" not in texts

    text, kb = await S.members_screen(db, chat, settings, "soon", 0)
    assert "Expiring members" in text and "Bob" in text and "Alice" not in text

    text, kb = await S.members_screen(db, chat, settings, "lifetime", 0)
    assert "Carol" in text and "lifetime" in text

    text, kb = await S.members_screen(db, chat, settings, "past", 0)
    assert "Dan" in text and "expired" in text

    # out-of-range page clamps instead of showing an empty page
    text, _kb = await S.members_screen(db, chat, settings, "active", 99)
    assert "Alice" in text


@pytest.mark.asyncio
async def test_members_screen_search_and_empty_states(env):
    db, chat, settings = env
    text, kb = await S.members_screen(db, chat, settings, "active", 0, query="dan")
    assert "Search “dan”" in text and "Dan" in text  # search spans all statuses
    assert "✖️ Clear search" in _buttons(kb)

    text, kb = await S.members_screen(db, chat, settings, "active", 0, query="zzz")
    assert "No matches" in text

    await db.upsert_chat(-200, "Empty", "channel", None, added_by=1)
    empty = await db.get_chat(-200)
    text, _ = await S.members_screen(db, empty, settings, "soon", 0)
    assert "Nobody expires" in text
    text, _ = await S.members_screen(db, empty, settings, "active", 0)
    assert "No active members" in text


@pytest.mark.asyncio
async def test_members_screen_paginates(env):
    db, chat, settings = env
    now = datetime.now(timezone.utc)
    for uid in range(100, 100 + S.LIST_PAGE * 2):
        await db.upsert_member(-100, uid, f"User {uid}", None, now + timedelta(days=uid))
    text, kb = await S.members_screen(db, chat, settings, "active", 1)
    assert "Page 2 / 3" in text
    data = _data(kb)
    assert "list:-100:active:0" in data and "list:-100:active:2" in data
    # a page carries exactly LIST_PAGE member rows
    assert sum(1 for d in data if d and d.startswith("member:")) == S.LIST_PAGE


@pytest.mark.asyncio
async def test_dashboard_and_chats_screens(env):
    db, chat, settings = env
    await db.create_pending(-100, 99, "Newbie", None)
    text, kb = await S.dashboard_screen(db, chat, settings, many_chats=False)
    assert "VIP Club" in text and "Active members: <b>3</b>" in text
    assert "<b>1</b> waiting" in text and "<b>1</b> expiring" in text
    texts = _buttons(kb)
    assert "🔔 Pending · 1" in texts and "⏰ Expiring · 1" in texts
    assert "◀️ Chats" not in texts

    await db.update_chat(-100, auto_kick=0)
    text, _ = await S.dashboard_screen(db, await db.get_chat(-100), settings)
    assert "Auto-remove is off" in text

    text, kb = await S.chats_screen(db, [chat], -100)
    assert "Your chats" in text and "<b>1</b> member waiting" in text
    assert _buttons(kb)[0].startswith("▸ 👥 VIP Club · 3 · 🔔1")


@pytest.mark.asyncio
async def test_settings_advanced_duration_and_editors(env):
    db, chat, settings = env
    text, kb = S.settings_screen(chat, settings)
    assert "Settings — VIP Club" in text and "1 month" in text
    assert "1 month" in _buttons(kb)[0]

    text, kb = S.advanced_screen(chat)
    assert "Advanced — VIP Club" in text and "Log channel: <b>off</b>" in text
    assert "✏️ Welcome text" in _buttons(kb) and "📨 Log channel" in _buttons(kb)

    text, kb = S.duration_screen(chat, settings)
    assert "global default" in text
    await db.update_chat(-100, default_duration="3m")
    chat3 = await db.get_chat(-100)
    text, kb = S.duration_screen(chat3, settings)
    assert "chat-specific" in text and "✓ 3 months" in _buttons(kb)

    text, kb = S.edit_text_screen(chat, "welcome")
    assert "{mention}" in text and "🗑 Clear" not in _buttons(kb)
    await db.update_chat(-100, welcome_text="hi {name}", log_chat_id=-500)
    chat_w = await db.get_chat(-100)
    text, kb = S.edit_text_screen(chat_w, "welcome")
    assert "hi {name}" in text and "🗑 Clear" in _buttons(kb)
    text, kb = S.edit_text_screen(chat_w, "log")
    assert "-500" in text and "📨 Use this chat" in _buttons(kb)


@pytest.mark.asyncio
async def test_pending_invites_stats_logs_vips_tools(env):
    db, chat, settings = env
    text, kb = await S.pending_screen(db, chat, settings)
    assert "Nothing waiting" in text
    p = await db.create_pending(-100, 99, "Newbie", None, source="request")
    await db.create_pending(-100, 98, "Other", None)
    text, kb = await S.pending_screen(db, chat, settings)
    assert "Pending — VIP Club</b> · 2" in text and "requested to join" in text
    assert f"jb:{p.id}" in _data(kb) and "pall:-100" in _data(kb)

    text, kb = await S.invites_screen(db, chat)
    assert "New link" in text
    await db.add_invite_link("https://t.me/+abc123", -100, "Gold", "3m", 1)
    text, kb = await S.invites_screen(db, chat)
    assert "<b>Gold</b> · 3 months · 0 joined" in text
    assert kb.inline_keyboard[1][0].copy_text.text == "https://t.me/+abc123"
    text, kb = S.invite_created_screen(chat, "https://t.me/+abc123", "Gold", "3m")
    assert "Invite link created" in text and "3 months" in text

    text, kb = await S.stats_screen(db, chat, settings)
    assert "Overview — VIP Club" in text and "Active: <b>3</b>" in text
    assert "list:-100:soon:0" in _data(kb)

    text, kb = await S.logs_screen(db, chat, settings, 0)
    assert "Nothing recorded yet" in text
    for i in range(S.LOGS_PAGE + 1):
        await db.add_log(-100, 10, "extend", f"x{i}", 1)
    text, kb = await S.logs_screen(db, chat, settings, 0)
    assert "logs:-100:1" in _data(kb) and "extend" in text
    text, kb = await S.logs_screen(db, chat, settings, 1)
    assert "page 2" in text and "logs:-100:0" in _data(kb) and "logs:-100:2" not in _data(kb)

    text, kb = await S.vips_screen(db, chat)
    assert "Nobody is protected" in text
    await db.add_whitelist(-100, 12, 1)
    text, kb = await S.vips_screen(db, chat)
    assert "· 1" in text and "🛡 Carol" in _buttons(kb)
    assert "member:-100:12" in _data(kb)

    text, kb = S.tools_screen(chat)
    assert "Tools — VIP Club" in text and "📣 Broadcast" in _buttons(kb)
