"""CSV export, grace-period helpers and the daily expiring digest."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from bot.config import Settings
from bot.handlers import screens as S
from bot.handlers.admin import parse_grace
from bot.services import export as X
from bot.services.database import Chat, Database, Member
from bot.services.membership import MembershipService
from bot.services.scheduler import DIGEST_DAYS, DIGEST_ROWS, ExpiryScheduler
from bot.utils import keyboards as K

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def _chat(**over) -> Chat:
    base = dict(
        chat_id=-100, title="VIP Club / Gold", chat_type="supergroup", username=None, default_duration="3m",
        tracking_enabled=True, auto_kick=True, kick_mode="kick", notify_user=True, log_chat_id=None,
        welcome_enabled=False, welcome_text=None, approve_requests=0, added_by=1,
    )
    base.update(over)
    return Chat(**base)


def _member(uid: int, name: str, days: float | None = 10, status: str = "active", **over) -> Member:
    base = dict(
        chat_id=-100, user_id=uid, full_name=name, username=None, joined_at=NOW - timedelta(days=5),
        expires_at=None if days is None else NOW + timedelta(days=days), status=status,
        note=None, reminders_sent=set(), added_by=1,
    )
    base.update(over)
    return Member(**base)


# ------------------------------------------------------------------ export (pure)
def test_members_csv_has_bom_header_and_rows():
    members = [
        _member(10, "Alice", 2, username="alice", note="paid via UPI\nref 123", source="invite:abc", renewals=2),
        _member(11, "Bob", None),
        _member(12, "Carol", -3, status="expired"),
    ]
    data = X.members_csv(members, TZ, NOW)
    assert data.startswith("\ufeff".encode("utf-8"))
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == list(X.CSV_COLUMNS)
    assert len(rows) == 4

    alice = dict(zip(X.CSV_COLUMNS, rows[1], strict=True))
    assert alice["user_id"] == "10"
    assert alice["username"] == "@alice"
    assert alice["remaining"] == "2d"
    assert alice["source"] == "invite:abc"
    assert alice["renewals"] == "2"
    assert alice["note"] == "paid via UPI ref 123"  # newline flattened
    assert alice["joined_at"] == "2026-01-05 17:30"  # converted to Asia/Kolkata (+5:30)

    bob = dict(zip(X.CSV_COLUMNS, rows[2], strict=True))
    assert bob["expires_at"] == "" and bob["remaining"] == "lifetime" and bob["username"] == ""

    carol = dict(zip(X.CSV_COLUMNS, rows[3], strict=True))
    assert carol["status"] == "expired" and carol["remaining"] == ""


def test_members_csv_empty_is_header_only():
    data = X.members_csv([], TZ, NOW).decode("utf-8-sig")
    assert data.strip() == ",".join(X.CSV_COLUMNS)


def test_export_filename_is_filesystem_safe():
    name = X.export_filename(_chat(), "active", NOW)
    assert name == "members_VIP_Club_Gold_active_2026-01-10.csv"
    assert "/" not in name and " " not in name
    # untitled chat falls back to the id
    assert X.export_filename(_chat(title=None, chat_id=-555), "all", NOW).startswith("members_-555_all_")


def test_export_caption_and_labels():
    assert X.export_caption(_chat(), "soon", 1) == "📥 VIP Club / Gold — Expiring soon: 1 member"
    assert X.export_caption(_chat(), "all", 12) == "📥 VIP Club / Gold — Everyone: 12 members"
    assert X.view_label("unknown") == "unknown"
    assert [k for k, _ in X.EXPORT_VIEWS] == ["all", "active", "soon", "lifetime", "past"]


# -------------------------------------------------------------- grace helpers
def test_grace_label_and_cycle():
    assert K.grace_label(0) == "off"
    assert K.grace_label(12) == "12h"
    assert K.grace_label(24) == "1d"
    assert K.grace_label(72) == "3d"
    assert K.grace_label(36) == "36h"
    assert K.next_grace(0) == 12
    assert K.next_grace(72) == 0  # wraps
    assert K.next_grace(36) == 0  # custom value resets to first step


def test_parse_grace_accepts_hours_days_and_caps():
    assert parse_grace("24") == 24
    assert parse_grace("12h") == 12
    assert parse_grace("3d") == 72
    assert parse_grace(" 2D ") == 48
    with pytest.raises(ValueError):
        parse_grace("abc")
    with pytest.raises(ValueError):
        parse_grace("31d")


def test_advanced_keyboard_shows_grace_and_digest_buttons():
    chat = _chat(grace_hours=24, digest_enabled=True)
    texts = [b.text for row in K.advanced_keyboard(chat).inline_keyboard for b in row]
    assert "⏱ Grace · 1d" in texts
    assert "✅ Daily digest" in texts
    texts_off = [b.text for row in K.advanced_keyboard(_chat()).inline_keyboard for b in row]
    assert "⏱ Grace · off" in texts_off and "❌ Daily digest" in texts_off


def test_tools_and_export_keyboards():
    tools = [b.text for row in K.tools_keyboard(_chat()).inline_keyboard for b in row]
    assert "📥 Export CSV" in tools
    kb = K.export_keyboard(-100, {"active": 3, "soon": 1, "lifetime": 1, "past": 2})
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert buttons[0].text == "📥 Everyone · 5" and buttons[0].callback_data == "exportv:-100:all"
    data = [b.callback_data for b in buttons]
    assert "exportv:-100:soon" in data and "tools:-100" in data
    # every label fits Telegram's 64-char cap
    assert all(len(b.text) <= 64 for b in buttons)


# -------------------------------------------------------------------- database
@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_members_for_export_and_digest_chats(db: Database):
    await db.upsert_chat(-100, "A", "supergroup", None, added_by=1)
    await db.upsert_chat(-200, "B", "supergroup", None, added_by=1)
    now = datetime.now(timezone.utc)
    await db.upsert_member(-100, 10, "Alice", "alice", now + timedelta(days=1))
    await db.upsert_member(-100, 11, "Bob", None, None)
    await db.upsert_member(-100, 12, "Carol", None, now + timedelta(days=30))
    await db.set_member_status(-100, 12, "expired")

    everyone = await db.members_for_export(-100, "all")
    # active first (soonest expiry, then lifetime), then past
    assert [m.user_id for m in everyone] == [10, 11, 12]
    assert [m.user_id for m in await db.members_for_export(-100, "past")] == [12]
    assert [m.user_id for m in await db.members_for_export(-100, "lifetime")] == [11]
    assert [m.user_id for m in await db.members_for_export(-100, "bogus")] == [10, 11, 12]

    assert await db.digest_chats() == []
    await db.update_chat(-200, digest_enabled=1)
    chats = await db.digest_chats()
    assert [c.chat_id for c in chats] == [-200] and chats[0].digest_enabled is True
    # tracking disabled → excluded
    await db.update_chat(-200, tracking_enabled=0)
    assert await db.digest_chats() == []


@pytest.mark.asyncio
async def test_export_screen_lists_counts(db: Database):
    await db.upsert_chat(-100, "A", "supergroup", None, added_by=1)
    now = datetime.now(timezone.utc)
    await db.upsert_member(-100, 10, "Alice", None, now + timedelta(days=1))
    await db.upsert_member(-100, 11, "Bob", None, None)
    chat = await db.get_chat(-100)
    text, kb = await S.export_screen(db, chat)
    assert "Export CSV" in text and "<b>2</b> tracked members" in text
    assert kb.inline_keyboard[0][0].text == "📥 Everyone · 2"


# ---------------------------------------------------------------------- digest
@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "d.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m", digest_hour=9)
    bot = MagicMock()
    bot.id = 999
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    owner = MagicMock()
    owner.status = "creator"
    owner.user = MagicMock(id=1, is_bot=False)
    bot.get_chat_administrators = AsyncMock(return_value=[owner])
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings)
    yield db, bot, service, scheduler, settings
    await db.close()


def test_digest_text_lists_soonest_and_truncates():
    settings = Settings(bot_token="x", super_admins=[1])
    scheduler = ExpiryScheduler(MagicMock(), MagicMock(), settings)
    chat = _chat()
    assert scheduler.digest_text(chat, [], NOW) is None
    members = [_member(100 + i, f"M{i}", days=1 + i / 100) for i in range(DIGEST_ROWS + 4)]
    text = scheduler.digest_text(chat, members, NOW)
    assert text is not None
    assert f"{DIGEST_ROWS + 4}</b> members expiring within {DIGEST_DAYS} days" in text
    assert text.count("• ") == DIGEST_ROWS
    assert "…and 4 more" in text
    assert "<code>100</code>" in text and "1d" in text


@pytest.mark.asyncio
async def test_send_digests_only_for_opted_in_chats_with_expiring_members(env):
    db, bot, service, scheduler, settings = env
    now = datetime.now(timezone.utc)
    await db.upsert_chat(-100, "Opted in", "supergroup", None, added_by=1)
    await db.upsert_chat(-200, "Not opted", "supergroup", None, added_by=1)
    await db.upsert_chat(-300, "Nobody expiring", "supergroup", None, added_by=1)
    await db.update_chat(-100, digest_enabled=1)
    await db.update_chat(-300, digest_enabled=1)
    await db.upsert_member(-100, 10, "Alice", None, now + timedelta(days=1))
    await db.upsert_member(-100, 11, "Bob", None, now + timedelta(days=30))  # outside window
    await db.upsert_member(-200, 20, "Zed", None, now + timedelta(hours=2))
    await db.upsert_member(-300, 30, "Far", None, now + timedelta(days=20))

    sent = await scheduler.send_digests()
    assert sent == 1
    assert bot.send_message.await_count == 1
    call = bot.send_message.await_args
    assert call.args[0] == 1  # owner DM
    text = call.args[1] if len(call.args) > 1 else call.kwargs["text"]
    assert "Opted in" in text and "Alice" in text and "Bob" not in text
    logs = await db.recent_logs(-100, limit=5)
    assert any(row["action"] == "digest" for row in logs)


@pytest.mark.asyncio
async def test_digest_job_is_registered_at_configured_hour():
    settings = Settings(bot_token="x", super_admins=[1], digest_hour=7, check_interval=60)
    scheduler = ExpiryScheduler(MagicMock(), MagicMock(), settings)
    scheduler.start()
    try:
        job = scheduler.scheduler.get_job("daily_digest")
        assert job is not None
        assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("hour")]) == "7"
        assert str(job.trigger.timezone) == str(settings.tz)
    finally:
        scheduler.scheduler.shutdown(wait=False)
