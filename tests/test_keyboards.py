"""Keyboards stay compact: the UI promise is 'only what you need on one screen'."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.services.database import Chat, InviteLink, Member, PendingJoin
from bot.utils import keyboards as K


def _chat(**over) -> Chat:
    base = dict(
        chat_id=-100, title="VIP Club", chat_type="supergroup", username=None, default_duration="3m",
        tracking_enabled=True, auto_kick=True, kick_mode="kick", notify_user=True, log_chat_id=None,
        welcome_enabled=False, welcome_text=None, approve_requests=0, added_by=1,
    )
    base.update(over)
    return Chat(**base)


def _member(uid: int, name: str = "Alice", days: float | None = 10, status: str = "active") -> Member:
    now = datetime.now(timezone.utc)
    return Member(
        chat_id=-100, user_id=uid, full_name=name, username=None, joined_at=now,
        expires_at=None if days is None else now + timedelta(days=days), status=status,
        note=None, reminders_sent=set(), added_by=1,
    )


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _data(markup) -> list[str | None]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _count(markup) -> int:
    return len(_buttons(markup))


def _assert_button_limits(markup) -> None:
    """Telegram hard limits: 64 bytes of callback data, ~64 chars of label, 8 buttons per row."""
    for row in markup.inline_keyboard:
        assert len(row) <= 8
        for b in row:
            assert len(b.text) <= 64, b.text
            if b.callback_data is not None:
                assert len(b.callback_data.encode()) <= 64, b.callback_data


def test_home_is_minimal():
    assert _count(K.home_keyboard(True, "bot", True)) == 3
    assert _count(K.home_keyboard(False, "bot", False)) == 3
    assert "📂 My chats" not in _buttons(K.home_keyboard(True, "bot", False))


def test_dashboard_and_settings_are_small():
    assert _count(K.dashboard_keyboard(_chat(), 0)) <= 10
    assert _count(K.settings_keyboard(_chat(), "1m", True)) <= 8
    labels = " ".join(_buttons(K.dashboard_keyboard(_chat(), 3, 5)))
    assert "Pending · 3" in labels and "Expiring · 5" in labels
    # single chat → no "Chats" button, just Home
    assert "◀️ Chats" not in _buttons(K.dashboard_keyboard(_chat(), 0, 0, many_chats=False))
    assert "◀️ Chats" in _buttons(K.dashboard_keyboard(_chat(), 0, 0, many_chats=True))
    # members / expiring open the tabbed list with the right view
    data = _data(K.dashboard_keyboard(_chat(), 0, 0))
    assert "list:-100:active:0" in data and "list:-100:soon:0" in data


def test_tools_is_off_dashboard_and_reachable():
    assert "🛠 Tools" in _buttons(K.dashboard_keyboard(_chat()))
    tools = _buttons(K.tools_keyboard(_chat()))
    for label in ("➕ Add member", "🔍 Search", "📣 Broadcast", "🛡 VIP list"):
        assert label in tools
    _assert_button_limits(K.tools_keyboard(_chat()))


def test_chats_keyboard_badges_and_clipping():
    long = _chat(chat_id=-1, title="A very very very long chat title that would overflow a button")
    kb = K.chats_keyboard([long, _chat(chat_id=-2, title="B", tracking_enabled=False)], -2, {-1: (120, 3), -2: (4, 0)})
    texts = _buttons(kb)
    assert texts[0].endswith("· 120 · 🔔3") and "…" in texts[0]
    assert texts[1].startswith("▸ ") and "⏸" in texts[1] and "🔔" not in texts[1]
    _assert_button_limits(kb)


def test_settings_shows_human_duration():
    assert "3 months" in _buttons(K.settings_keyboard(_chat(), "1m", True))[0]
    assert "1 month" in _buttons(K.settings_keyboard(_chat(default_duration=None), "1m", True))[0]


def test_join_prompt_compact_and_default_not_duplicated():
    kb = K.join_prompt_keyboard(1, "3m")
    texts = _buttons(kb)
    assert _count(kb) == 6
    assert texts[0] == "✅ Keep · 3 months"
    assert texts.count("3 months") == 0  # default not repeated in quick row
    assert "🚫 Remove" in texts


def test_join_request_wording():
    texts = _buttons(K.join_prompt_keyboard(1, "1m", is_request=True))
    assert texts[0].startswith("✅ Approve")
    assert "🚫 Reject" in texts


def test_join_more_has_all_presets_and_custom():
    texts = _buttons(K.join_more_keyboard(1))
    for label, _ in K.DURATION_PRESETS:
        assert label in texts
    assert any("Custom" in t for t in texts)


def test_member_card_primary_vs_more():
    kb = K.member_keyboard(-1, 2, True, "soon", 3)
    assert _count(kb) == 8
    assert "list:-1:soon:3" in _data(kb)  # back returns to the same list view/page
    more = _buttons(K.member_more_keyboard(-1, 2, True, False))
    assert "🚫 Remove from chat" in more and "🛡 Protect (VIP)" in more
    assert "📝 Note" in more and "🔔 Ask owner" in more
    assert "🚫 Remove from chat" not in _buttons(K.member_more_keyboard(-1, 2, False, True))
    assert "🛡 Unprotect" in _buttons(K.member_more_keyboard(-1, 2, True, True))


def test_list_keyboard_is_tappable_with_tabs_and_pager():
    members = [_member(1, "Alice", 10.5), _member(2, "Bob", 0.51), _member(3, "Carol", None), _member(4, "Dan", -1)]
    kb = K.list_keyboard(-100, "active", 1, 3, members, {"active": 30, "soon": 2, "lifetime": 1, "past": 7})
    texts = _buttons(kb)
    data = _data(kb)
    # tabs: current one marked, counts inline
    assert texts[0] == "▸ 🟢 Active 30" and "⏰ Expiring 2" in texts
    # rows: urgency glyph + name + compact remaining
    assert any(t.startswith("🟢 Alice · 10d") for t in texts)
    assert any(t.startswith("🟠 Bob · 12h") for t in texts)
    assert any(t.startswith("♾ Carol · ∞") for t in texts)
    assert any(t.startswith("🔴 Dan · exp") for t in texts)
    assert "member:-100:2:active:1" in data
    # pager in the middle page has both arrows and "2 / 3"
    assert "list:-100:active:0" in data and "list:-100:active:2" in data and "2 / 3" in texts
    assert "🔍 Search" in texts and "➕ Add member" in texts
    _assert_button_limits(kb)


def test_list_keyboard_search_mode_and_single_page():
    kb = K.list_keyboard(-100, "active", 0, 1, [_member(1)], None, query="ali")
    texts = _buttons(kb)
    assert "✖️ Clear search" in texts and "🔍 Search again" in texts
    assert not any(t.startswith("▸") for t in texts)  # no tab is 'current' while searching
    assert "1 / 1" not in texts  # pager hidden on a single page


def test_long_member_names_fit_in_buttons():
    m = _member(1, "X" * 200, 5)
    kb = K.list_keyboard(-100, "active", 0, 1, [m])
    _assert_button_limits(kb)
    assert "…" in _buttons(kb)[4]


def test_pending_keyboard_rows_and_bulk():
    now = datetime.now(timezone.utc)
    items = [
        PendingJoin(id=i, chat_id=-100, user_id=i, full_name=f"User {i}", username=None,
                    source="request" if i % 2 else "join", status="pending", created_at=now,
                    decided_at=None, decided_by=None, decision=None)
        for i in range(1, 4)
    ]
    kb = K.pending_keyboard(-100, items)
    texts, data = _buttons(kb), _data(kb)
    assert texts[0] == "🙋 User 1" and texts[1] == "👤 User 2"
    assert "jb:2" in data and "pall:-100" in data
    assert "✅ Default for all (3)" in texts
    assert "pall:-100" not in _data(K.pending_keyboard(-100, items[:1]))  # no bulk for a single item


def test_invites_new_link_is_two_step():
    assert _buttons(K.invites_keyboard(_chat(), []))[0] == "➕ New link"
    assert _count(K.invite_pick_keyboard(-100)) == len(K.DURATION_PRESETS) + 2  # + custom + back
    assert any("Custom" in t for t in _buttons(K.invite_pick_keyboard(-100)))


def test_invite_rows_copy_and_revoke():
    link = InviteLink("https://t.me/+abcdefghijklmnopqrstuvwxyz", -100, "Gold", "3m", 1,
                      datetime.now(timezone.utc), 7, False)
    kb = K.invites_keyboard(_chat(), [link])
    row = kb.inline_keyboard[1]
    assert row[0].copy_text is not None and row[0].copy_text.text == link.invite_link
    assert row[0].text == "📋 Gold · 7"
    assert row[1].callback_data.startswith("irev:-100:")
    _assert_button_limits(kb)
    created = K.invite_created_keyboard(-100, link.invite_link)
    assert created.inline_keyboard[0][0].copy_text.text == link.invite_link
    assert "t.me/share/url" in created.inline_keyboard[1][0].url


def test_duration_keyboard_marks_current():
    texts = _buttons(K.duration_keyboard(-100, "3m"))
    assert "✓ 3 months" in texts and "1 month" in texts
    assert "✏️ Custom" in texts


def test_every_screen_keyboard_has_home_escape():
    for kb in (
        K.dashboard_keyboard(_chat()), K.tools_keyboard(_chat()), K.settings_keyboard(_chat(), "1m", True),
        K.advanced_keyboard(_chat()), K.stats_keyboard(-100), K.logs_keyboard(-100, 0, False),
        K.pending_keyboard(-100, []), K.invites_keyboard(_chat(), []), K.vips_keyboard(-100, [], []),
        K.list_keyboard(-100, "active", 0, 1, []),
    ):
        assert "home" in _data(kb), kb


def test_quick_durations_exclude_default():
    assert "1m" not in K.quick_durations("1m")
    assert len(K.quick_durations("never")) == 3
