"""Invite-link usage limits: parsing, DB counters/exhaustion, service, screens, keyboards."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import User

from bot.config import Settings
from bot.handlers import screens as S
from bot.services.database import (
    INVITE_ACTIVE,
    INVITE_EXHAUSTED,
    INVITE_REVOKED,
    Chat,
    Database,
    InviteLink,
)
from bot.services.membership import MembershipService
from bot.utils import invites as I
from bot.utils import keyboards as K


# ------------------------------------------------------------------ helpers
def _chat(**over) -> Chat:
    base: dict = {
        "chat_id": -100, "title": "VIP Club", "chat_type": "supergroup", "username": None, "default_duration": "3m",
        "tracking_enabled": True, "auto_kick": True, "kick_mode": "kick", "notify_user": True, "log_chat_id": None,
        "welcome_enabled": False, "welcome_text": None, "approve_requests": 0, "added_by": 1,
    }
    base.update(over)
    return Chat(**base)


def _link(uses: int = 0, limit: int | None = None, state: int = INVITE_ACTIVE, name: str = "Gold") -> InviteLink:
    return InviteLink(
        invite_link="https://t.me/+abcdefghijklmnopqrstuvwxyz", chat_id=-100, name=name, duration="3m",
        created_by=1, created_at=datetime.now(UTC), uses=uses, revoked=bool(state),
        member_limit=limit, state=state,
    )


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _data(markup) -> list[str | None]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _user(uid: int, name: str = "User") -> User:
    return User(id=uid, is_bot=False, first_name=name)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def env(db):
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m", reminder_hours=[72], ask_timeout_hours=24)
    bot = MagicMock()
    bot.id = 999
    sent = MagicMock()
    sent.message_id = 1
    bot.send_message = AsyncMock(return_value=sent)
    tg_link = MagicMock()
    tg_link.invite_link = "https://t.me/+capped"
    bot.create_chat_invite_link = AsyncMock(return_value=tg_link)
    bot.revoke_chat_invite_link = AsyncMock()
    bot.get_chat_administrators = AsyncMock(return_value=[])
    service = MembershipService(bot, db, settings)
    return db, bot, service


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    "token,expected",
    [
        ("x5", 5), ("X5", 5), ("×5", 5), ("*5", 5),
        ("5x", 5), ("5×", 5),
        ("limit=5", 5), ("limit:5", 5), ("uses=7", 7), ("max=3", 3), ("cap=2", 2),
        ("once", 1), ("ONCE", 1), ("single", 1), ("one-time", 1),
        ("Gold", None), ("3m", None), ("x", None), ("", None), ("5", None), ("10d", None),
    ],
)
def test_parse_limit_token(token, expected):
    assert I.parse_limit_token(token) == expected


@pytest.mark.parametrize(
    "text,label,limit",
    [
        (None, None, None),
        ("", None, None),
        ("   ", None, None),
        ("Gold plan", "Gold plan", None),
        ("x10 Promo", "Promo", 10),
        ("Promo x10", "Promo", 10),
        ("once Founder seat", "Founder seat", 1),
        ("Founder once", "Founder", 1),
        ("limit=25", None, 25),
        ("limit 25 Launch", "Launch", 25),
        ("Launch uses: 4", "Launch", 4),
        ("Black  Friday   x50  deal", "Black Friday deal", 50),
        ("x5 x9", None, 9),  # last one wins
        ("Max Power", "Max Power", None),  # bare key word without number stays in label
    ],
)
def test_split_limit(text, label, limit):
    assert I.split_limit(text) == (label, limit)


@pytest.mark.parametrize("bad", ["x0", "0x", "limit=0", "x100000", "limit 999999"])
def test_split_limit_rejects_out_of_range(bad):
    with pytest.raises(I.LimitError):
        I.split_limit(bad)


def test_limit_labels():
    assert I.limit_label(None) == "unlimited"
    assert I.limit_label(0) == "unlimited"
    assert I.limit_label(1) == "1 use"
    assert I.limit_label(50) == "50 uses"
    assert I.limit_arg(None) == ""
    assert I.limit_arg(1) == "once"
    assert I.limit_arg(50) == "x50"


def test_limit_presets_shape():
    labels = [lbl for lbl, _ in I.LIMIT_PRESETS]
    values = [v for _, v in I.LIMIT_PRESETS]
    assert values[0] is None and "Unlimited" in labels[0]
    assert values[1:] == [1, 5, 10, 25, 50, 100]
    assert all(v is None or 1 <= v <= I.MAX_MEMBER_LIMIT for v in values)
    assert I.MAX_MEMBER_LIMIT == 99_999


# ----------------------------------------------------------- InviteLink model
def test_invite_link_properties_unlimited():
    link = _link(uses=7)
    assert link.member_limit is None and link.remaining is None
    assert not link.exhausted
    assert link.uses_label == "7 joined" and link.uses_short == "7"


def test_invite_link_properties_capped():
    link = _link(uses=12, limit=50)
    assert link.remaining == 38 and not link.exhausted
    assert link.uses_label == "12/50 uses" and link.uses_short == "12/50"
    full = _link(uses=50, limit=50)
    assert full.exhausted and full.remaining == 0
    over = _link(uses=53, limit=50, state=INVITE_EXHAUSTED)
    assert over.exhausted and over.remaining == 0 and over.uses_label == "50/50 uses"


def test_invite_link_from_row_without_member_limit_column():
    """Rows from DBs created before the migration must still load."""
    row = {
        "invite_link": "https://t.me/+old", "chat_id": -1, "name": "Old", "duration": "1m",
        "created_by": 1, "created_at": None, "uses": 3, "revoked": 0,
    }
    link = InviteLink.from_row(row)
    assert link.member_limit is None and link.uses == 3 and not link.revoked and link.state == INVITE_ACTIVE


# ------------------------------------------------------------------ database
@pytest.mark.asyncio
async def test_db_bump_marks_exhausted_at_limit(db: Database):
    await db.upsert_chat(-600, "G", "supergroup", None, added_by=1)
    link = await db.add_invite_link("https://t.me/+cap2", -600, "Promo", "1m", 1, member_limit=2)
    assert link.member_limit == 2 and link.state == INVITE_ACTIVE and not link.exhausted

    link = await db.bump_invite_use("https://t.me/+cap2")
    assert link.uses == 1 and link.state == INVITE_ACTIVE and link.remaining == 1

    link = await db.bump_invite_use("https://t.me/+cap2")
    assert link.uses == 2 and link.state == INVITE_EXHAUSTED and link.exhausted and link.revoked

    # a late join (Telegram race) still counts but state stays EXHAUSTED, never REVOKED
    link = await db.bump_invite_use("https://t.me/+cap2")
    assert link.uses == 3 and link.state == INVITE_EXHAUSTED and link.uses_label == "2/2 uses"

    assert await db.bump_invite_use("https://t.me/+unknown") is None


@pytest.mark.asyncio
async def test_db_unlimited_never_exhausts_and_zero_limit_means_none(db: Database):
    link = await db.add_invite_link("https://t.me/+inf", -600, "Open", "1m", 1, member_limit=0)
    assert link.member_limit is None
    for _ in range(5):
        link = await db.bump_invite_use("https://t.me/+inf")
    assert link.uses == 5 and link.state == INVITE_ACTIVE and not link.exhausted


@pytest.mark.asyncio
async def test_db_listing_hides_revoked_but_keeps_exhausted(db: Database):
    await db.add_invite_link("https://t.me/+a", -600, "A", "1m", 1)
    await db.add_invite_link("https://t.me/+b", -600, "B", "1m", 1, member_limit=1)
    await db.add_invite_link("https://t.me/+c", -600, "C", "1m", 1)
    await db.bump_invite_use("https://t.me/+b")  # exhausted
    await db.revoke_invite_link("https://t.me/+c")  # revoked

    names = {lk.name for lk in await db.list_invite_links(-600)}
    assert names == {"A", "B"}
    assert {lk.name for lk in await db.list_invite_links(-600, include_exhausted=False)} == {"A"}
    assert {lk.name for lk in await db.list_invite_links(-600, include_revoked=True)} == {"A", "B", "C"}
    assert await db.count_invite_links(-600) == 1

    c = await db.get_invite_link("https://t.me/+c")
    assert c.state == INVITE_REVOKED and c.revoked and not c.exhausted

    stats = await db.global_stats()
    assert stats["invites"] == 1


@pytest.mark.asyncio
async def test_db_list_is_bounded(db: Database):
    for i in range(12):
        await db.add_invite_link(f"https://t.me/+n{i}", -600, f"L{i}", "1m", 1)
    assert len(await db.list_invite_links(-600, limit=5)) == 5
    assert len(await db.list_invite_links(-600)) == 12


# ------------------------------------------------------------------- service
@pytest.mark.asyncio
async def test_service_forwards_member_limit_to_telegram(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    url, label = await service.create_invite_link(chat, "3m", "Promo", 1, member_limit=10)
    assert url == "https://t.me/+capped" and label == "Promo"
    kwargs = bot.create_chat_invite_link.await_args.kwargs
    assert kwargs["member_limit"] == 10 and kwargs["name"] == "Promo"
    stored = await db.get_invite_link(url)
    assert stored.member_limit == 10


@pytest.mark.asyncio
async def test_service_unlimited_omits_member_limit(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    url, _ = await service.create_invite_link(chat, "3m", None, 1)
    assert url
    assert "member_limit" not in bot.create_chat_invite_link.await_args.kwargs
    assert (await db.get_invite_link(url)).member_limit is None


@pytest.mark.asyncio
async def test_service_rejects_bad_limits(env):
    db, bot, service = env
    chat = await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    url, msg = await service.create_invite_link(chat, "3m", None, 1, member_limit=0)
    assert url is None and "between 1 and" in msg
    url, msg = await service.create_invite_link(chat, "3m", None, 1, member_limit=I.MAX_MEMBER_LIMIT + 1)
    assert url is None and "between 1 and" in msg
    url, msg = await service.create_invite_link(chat, "3m", None, 1, join_request=True, member_limit=5)
    assert url is None and "join requests" in msg
    bot.create_chat_invite_link.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_through_capped_link_counts_and_logs_exhaustion(env):
    db, bot, service = env
    await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    await db.update_chat(-1, ask_on_join=0, log_chat_id=-777)
    chat = await db.get_chat(-1)
    await db.add_invite_link("https://t.me/+one", -1, "Founder", "1y", 1, member_limit=1)

    m = await service.track_join(chat, _user(10, "Alice"), invite_link="https://t.me/+one")
    assert m is not None and m.source == "invite:Founder"
    link = await db.get_invite_link("https://t.me/+one")
    assert link.exhausted and link.uses == 1

    logs = await db.recent_logs(-1, limit=20)
    actions = [row.action if hasattr(row, "action") else row["action"] for row in logs]
    assert "invite_exhausted" in actions
    # the log channel got the "used up" note
    texts = [c.kwargs.get("text") or (c.args[1] if len(c.args) > 1 else "") for c in bot.send_message.await_args_list]
    assert any("used up" in (t or "") for t in texts)

    # second join through the (now exhausted) link still gets the preset,
    # counter keeps ticking, but no second "used up" log entry
    bot.send_message.reset_mock()
    m2 = await service.track_join(chat, _user(11, "Bob"), invite_link="https://t.me/+one")
    assert m2 is not None and m2.source == "invite:Founder"
    assert (await db.get_invite_link("https://t.me/+one")).uses == 2
    logs = await db.recent_logs(-1, limit=20)
    actions = [row.action if hasattr(row, "action") else row["action"] for row in logs]
    assert actions.count("invite_exhausted") == 1


@pytest.mark.asyncio
async def test_join_through_revoked_link_ignores_preset(env):
    db, _bot, service = env
    await db.upsert_chat(-1, "G", "supergroup", None, added_by=1)
    await db.update_chat(-1, ask_on_join=0)
    chat = await db.get_chat(-1)
    await db.add_invite_link("https://t.me/+rev", -1, "Old", "1y", 1)
    await db.revoke_invite_link("https://t.me/+rev")
    m = await service.track_join(chat, _user(12, "Carol"), invite_link="https://t.me/+rev")
    assert m is not None and (m.source or "").startswith("invite:") is False
    assert (await db.get_invite_link("https://t.me/+rev")).uses == 0


# ------------------------------------------------------------------- screens
def test_invite_limit_screen_and_keyboard():
    text, kb = S.invite_limit_screen(_chat(), "3m")
    assert "How many people" in text and "3 months" in text
    data = _data(kb)
    assert "inewl:-100:3m:0" in data  # unlimited
    assert "inewl:-100:3m:1" in data and "inewl:-100:3m:100" in data
    assert "inlimc:-100:3m" in data and "inpick:-100" in data
    assert len(data) == len(I.LIMIT_PRESETS) + 2
    assert kb.inline_keyboard[0][0].text.endswith("Unlimited") and len(kb.inline_keyboard[0]) == 1


def test_invite_created_screen_footers():
    chat = _chat()
    t_unl, _ = S.invite_created_screen(chat, "https://t.me/+x", "Gold", "3m")
    assert "unlimited" in t_unl and "automatically" in t_unl
    t_one, _ = S.invite_created_screen(chat, "https://t.me/+x", "Gold", "3m", 1)
    assert "1 use" in t_one and "Single-use" in t_one
    t_many, _ = S.invite_created_screen(chat, "https://t.me/+x", "Gold", "3m", 25)
    assert "25 uses" in t_many and "after 25 joins" in t_many


@pytest.mark.asyncio
async def test_invites_screen_marks_used_up_links(db: Database):
    chat = await db.upsert_chat(-100, "VIP Club", "supergroup", None, added_by=1)
    await db.add_invite_link("https://t.me/+open", -100, "Open", "1m", 1)
    await db.add_invite_link("https://t.me/+seat", -100, "Seat", "1y", 1, member_limit=1)
    await db.bump_invite_use("https://t.me/+seat")
    text, kb = await S.invites_screen(db, chat)
    assert "1/1 uses" in text and "used up" in text and "0 joined" in text
    assert "reached their usage limit" in text
    buttons = _buttons(kb)
    assert any(b.startswith("🎟") and "Seat" in b and "1/1" in b for b in buttons)
    assert any(b.startswith("📋") and "Open" in b for b in buttons)


def test_invites_keyboard_counters():
    kb = K.invites_keyboard(_chat(), [_link(uses=12, limit=50), _link(uses=3, name="Free")])
    buttons = _buttons(kb)
    assert any("12/50" in b for b in buttons)
    assert any("Free · 3" in b for b in buttons)
    # every row stays within Telegram's button-text comfort zone
    assert all(len(b) <= 64 for b in buttons)
