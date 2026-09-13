"""Persistent bottom menu (reply keyboard): builder rules and message routing."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import ReplyKeyboardMarkup

from bot import VERSION_TAG, __version__
from bot.config import Settings
from bot.handlers import menu
from bot.handlers.common import home_text
from bot.services.database import Database
from bot.utils import keyboards as K


def _labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.keyboard for b in row]


# ------------------------------------------------------------------ builders
def test_menu_variants_match_role():
    full = K.main_menu_keyboard(True, True)
    assert full.is_persistent and full.resize_keyboard
    assert set(_labels(full)) == {
        K.MENU_DASHBOARD, K.MENU_MEMBERS, K.MENU_PENDING, K.MENU_INVITES, K.MENU_SETTINGS, K.MENU_CHATS, K.MENU_HELP,
    }
    assert len(full.keyboard) == 4  # 2+2+2+1 rows — stays compact on phones

    assert _labels(K.main_menu_keyboard(True, False)) == [K.MENU_ADD, K.MENU_HELP]
    assert _labels(K.main_menu_keyboard(False, False)) == [K.MENU_STATUS, K.MENU_HELP]


def test_every_label_is_routable_and_unique():
    all_labels = set()
    for is_admin, has_chats in ((True, True), (True, False), (False, False)):
        all_labels.update(_labels(K.main_menu_keyboard(is_admin, has_chats)))
    assert all_labels == set(K.MENU_BUTTONS)
    assert len(K.MENU_BUTTONS) == 9


def test_menu_key_normalises_and_rejects_free_text():
    assert K.menu_key(K.MENU_HELP) == K.MENU_HELP
    assert K.menu_key("  📖   Help ") == K.MENU_HELP
    assert K.menu_key("/help") is None
    assert K.menu_key("@someone") is None
    assert K.menu_key("123456789") is None
    assert K.menu_key("") is None
    assert K.menu_key(None) is None


def test_remove_keyboard_and_add_links():
    assert K.remove_reply_keyboard().remove_keyboard is True
    texts = [b.text for row in K.add_to_group_keyboard("mybot").inline_keyboard for b in row]
    assert texts == ["➕ Add to a group", "📢 Add to a channel"]


def test_home_text_carries_version_tag():
    assert VERSION_TAG in home_text("Ann", True, True)
    assert VERSION_TAG in home_text("Ann", False, False)
    assert __version__.count(".") == 2


# ------------------------------------------------------------------- routing
@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "menu.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[1], default_duration="1m")
    bot = MagicMock()
    me = MagicMock()
    me.username = "trackerbot"
    bot.me = AsyncMock(return_value=me)
    bot.get_chat_member = AsyncMock(side_effect=AssertionError("no live lookups expected"))
    yield db, bot, settings
    await db.close()


def _msg(text: str, uid: int = 1, chat_type: str = "private"):
    m = MagicMock()
    m.text = text
    m.from_user = MagicMock()
    m.from_user.id = uid
    m.from_user.first_name = "Ann"
    m.chat = MagicMock()
    m.chat.type = chat_type
    m.chat.id = uid
    m.answer = AsyncMock()
    return m


def _state():
    s = MagicMock()
    s.clear = AsyncMock()
    return s


def _sent(m) -> list[tuple[str, object]]:
    return [(c.args[0], c.kwargs.get("reply_markup")) for c in m.answer.await_args_list]


@pytest.mark.asyncio
async def test_help_and_status_work_for_everyone(env):
    db, _bot, settings = env
    state = _state()
    m = _msg(K.MENU_HELP, uid=42)
    await menu.menu_help(m, state)
    text, markup = _sent(m)[0]
    assert "Help" in text and markup is not None
    state.clear.assert_awaited_once()

    m = _msg(K.MENU_STATUS, uid=42)
    await menu.menu_status(m, db, settings, _state())
    assert "no tracked memberships" in _sent(m)[0][0]


@pytest.mark.asyncio
async def test_single_chat_is_auto_selected_for_dashboard(env):
    db, bot, settings = env
    await db.upsert_chat(-100, "VIP Club", "supergroup", None, added_by=1)
    m = _msg(K.MENU_DASHBOARD, uid=1)
    await menu.menu_dashboard(m, bot, db, settings, _state())
    text, markup = _sent(m)[0]
    assert "VIP Club" in text
    assert "👥 Members" in [b.text for row in markup.inline_keyboard for b in row]
    assert await db.get_context(1) == -100


@pytest.mark.asyncio
async def test_multiple_chats_without_context_show_picker(env):
    db, bot, settings = env
    await db.upsert_chat(-100, "A", "supergroup", None, added_by=1)
    await db.upsert_chat(-200, "B", "channel", None, added_by=1)
    m = _msg(K.MENU_SETTINGS, uid=1)
    await menu.menu_settings(m, bot, db, settings, _state())
    text, markup = _sent(m)[0]
    assert "Your chats" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("A" in t for t in labels) and any("B" in t for t in labels)


@pytest.mark.asyncio
async def test_context_is_respected_for_members_pending_invites_settings(env):
    db, bot, settings = env
    await db.upsert_chat(-100, "A", "supergroup", None, added_by=1)
    await db.upsert_chat(-200, "B", "supergroup", None, added_by=1)
    await db.set_context(1, -200)
    for handler, needle in (
        (menu.menu_members, "Members — B"),
        (menu.menu_pending, "Pending — B"),
        (menu.menu_invites, "Invite links — B"),
        (menu.menu_settings, "Settings — B"),
    ):
        m = _msg("x", uid=1)
        await handler(m, bot, db, settings, _state())
        assert needle in _sent(m)[0][0], needle


@pytest.mark.asyncio
async def test_admin_without_chats_gets_add_flow(env):
    db, bot, settings = env
    m = _msg(K.MENU_DASHBOARD, uid=1)  # super admin, nothing tracked yet
    await menu.menu_dashboard(m, bot, db, settings, _state())
    sent = _sent(m)
    assert "No chats yet" in sent[0][0]
    assert _labels(sent[0][1]) == [K.MENU_ADD, K.MENU_HELP]
    assert "startgroup=true" in sent[1][1].inline_keyboard[0][0].url


@pytest.mark.asyncio
async def test_non_admin_with_stale_keyboard_is_downgraded(env):
    db, bot, settings = env
    m = _msg(K.MENU_CHATS, uid=777)  # regular user tapping an admin button
    await menu.menu_chats(m, bot, db, settings, _state())
    sent = _sent(m)
    assert _labels(sent[0][1]) == [K.MENU_STATUS, K.MENU_HELP]
    assert "Quick actions" in sent[1][0]


@pytest.mark.asyncio
async def test_add_button_upgrades_stale_keyboard_when_chats_exist(env):
    db, bot, settings = env
    await db.upsert_chat(-100, "A", "supergroup", None, added_by=1)
    m = _msg(K.MENU_ADD, uid=1)
    await menu.menu_add(m, bot, db, settings, _state())
    sent = _sent(m)
    assert K.MENU_DASHBOARD in _labels(sent[0][1])
    assert "Add me to a chat" in sent[1][0]


@pytest.mark.asyncio
async def test_chats_list_marks_current(env):
    db, bot, settings = env
    await db.upsert_chat(-100, "A", "supergroup", None, added_by=1)
    await db.upsert_chat(-200, "B", "supergroup", None, added_by=1)
    await db.set_context(1, -200)
    m = _msg(K.MENU_CHATS, uid=1)
    await menu.menu_chats(m, bot, db, settings, _state())
    labels = [b.text for row in _sent(m)[0][1].inline_keyboard for b in row]
    assert any(t.startswith("▸") and "B" in t for t in labels)


def test_router_filters_ignore_groups_and_free_text():
    """The label filter must not swallow ordinary text or group messages."""
    private_tap = _msg(K.MENU_HELP)
    group_tap = _msg(K.MENU_HELP, chat_type="supergroup")
    free_text = _msg("hello")
    chat_f, text_f = menu._tap(K.MENU_HELP)
    assert chat_f.resolve(private_tap) and text_f.resolve(private_tap)
    assert not chat_f.resolve(group_tap)
    assert not text_f.resolve(free_text)
