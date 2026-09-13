"""Keyboards stay compact: the UI promise is 'only what you need on one screen'."""
from __future__ import annotations

from bot.services.database import Chat
from bot.utils import keyboards as K


def _chat(**over) -> Chat:
    base = dict(
        chat_id=-100, title="VIP Club", chat_type="supergroup", username=None, default_duration="3m",
        tracking_enabled=True, auto_kick=True, kick_mode="kick", notify_user=True, log_chat_id=None,
        welcome_enabled=False, welcome_text=None, approve_requests=0, added_by=1,
    )
    base.update(over)
    return Chat(**base)


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _count(markup) -> int:
    return len(_buttons(markup))


def test_home_is_minimal():
    assert _count(K.home_keyboard(True, "bot", True)) == 3
    assert _count(K.home_keyboard(False, "bot", False)) == 3
    assert "📂 My chats" not in _buttons(K.home_keyboard(True, "bot", False))


def test_dashboard_and_settings_are_small():
    assert _count(K.dashboard_keyboard(_chat(), 0)) <= 7
    assert _count(K.settings_keyboard(_chat(), "1m", True)) <= 7
    assert "Pending (3)" in " ".join(_buttons(K.dashboard_keyboard(_chat(), 3)))


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
    assert _count(K.member_keyboard(-1, 2, True)) == 6
    more = _buttons(K.member_more_keyboard(-1, 2, True, False))
    assert "🚫 Remove from chat" in more and "🛡 Protect (VIP)" in more
    assert "🚫 Remove from chat" not in _buttons(K.member_more_keyboard(-1, 2, False, True))
    assert "🛡 Unprotect" in _buttons(K.member_more_keyboard(-1, 2, True, True))


def test_invites_new_link_is_two_step():
    assert _buttons(K.invites_keyboard(_chat(), []))[0] == "➕ New link"
    assert _count(K.invite_pick_keyboard(-100)) == len(K.DURATION_PRESETS) + 1


def test_quick_durations_exclude_default():
    assert "1m" not in K.quick_durations("1m")
    assert len(K.quick_durations("never")) == 3
