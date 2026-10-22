"""Member-initiated renewal requests: 🙏 button → admin prompt → extend / decline."""
# ruff: noqa: F811  (pytest fixtures are injected by name; re-using ``env`` is intentional)
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.handlers.common import mystatus_screen
from bot.utils import keyboards as K
from tests.test_keyboards import _buttons, _chat, _data
from tests.test_service import ADMIN_ID, OWNER_ID, env  # noqa: F401 - fixture re-export

NOW = datetime.now(timezone.utc)


async def _setup(db, days: float = 3, **chat_over):
    chat = await db.upsert_chat(-1, "G", "supergroup", None, added_by=ADMIN_ID)
    if chat_over:
        await db.update_chat(-1, **chat_over)
        chat = await db.get_chat(-1)
    member = await db.upsert_member(-1, 10, "Alice", "alice", NOW + timedelta(days=days))
    return chat, member


# ----------------------------------------------------------------- requesting
@pytest.mark.asyncio
async def test_request_creates_pending_and_prompts_owner(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db)
    ok, note = await service.request_renewal(chat, member)
    assert ok, note
    pending = await db.get_pending_for(-1, 10)
    assert pending is not None and pending.source == "renewal" and pending.status == "pending"
    # owner was DM'd with the renewal prompt
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    assert args[0] == OWNER_ID
    assert "Renewal request" in args[1]
    texts = _buttons(kwargs["reply_markup"])
    assert texts[0].startswith("✅ Extend")
    assert "🚫 Decline" in texts
    assert await db.count_pending(-1) == 1


@pytest.mark.asyncio
async def test_request_blocked_when_disabled_lifetime_or_duplicate(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db, renewals_enabled=0)
    ok, note = await service.request_renewal(chat, member)
    assert not ok and "disabled" in note

    await db.update_chat(-1, renewals_enabled=1)
    chat = await db.get_chat(-1)
    await db.set_member_expiry(-1, 10, None)
    member = await db.get_member(-1, 10)
    ok, note = await service.request_renewal(chat, member)
    assert not ok and "lifetime" in note

    await db.set_member_expiry(-1, 10, NOW + timedelta(days=2))
    member = await db.get_member(-1, 10)
    ok, _ = await service.request_renewal(chat, member)
    assert ok
    ok, note = await service.request_renewal(chat, member)
    assert not ok and "already waiting" in note
    assert bot.send_message.await_count == 1  # second attempt did not re-prompt


@pytest.mark.asyncio
async def test_request_cooldown_after_decision(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db)
    ok, _ = await service.request_renewal(chat, member)
    assert ok
    pending = await db.get_pending_for(-1, 10)
    ok, _ = await service.apply_decision(pending, "remove", OWNER_ID, "Owner")
    assert ok
    ok, note = await service.request_renewal(chat, member)
    assert not ok and "another renewal request in" in note
    # once the cooldown has passed, a new request is allowed again
    await db._exec(
        "UPDATE pending_joins SET decided_at=? WHERE id=?",
        ((NOW - timedelta(hours=25)).isoformat(), pending.id),
    )
    ok, _ = await service.request_renewal(chat, member)
    assert ok


@pytest.mark.asyncio
async def test_left_members_cannot_request(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db)
    await db.set_member_status(-1, 10, "left")
    member = await db.get_member(-1, 10)
    ok, note = await service.request_renewal(chat, member)
    assert not ok and "no longer a member" in note


# ------------------------------------------------------------------ deciding
@pytest.mark.asyncio
async def test_approve_extends_from_current_expiry_and_notifies(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db, days=3)
    await service.request_renewal(chat, member)
    pending = await db.get_pending_for(-1, 10)
    bot.send_message.reset_mock()

    ok, outcome = await service.apply_decision(pending, "1w", OWNER_ID, "Owner")
    assert ok, outcome
    assert "Renewed" in outcome
    m = await db.get_member(-1, 10)
    expected = member.expires_at + timedelta(days=7)
    assert abs((m.expires_at - expected).total_seconds()) < 2
    assert m.renewals == 1
    assert (await db.get_pending(pending.id)).status == "decided"
    # prompt edited (buttons removed) + member DM'd
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    assert "Renewal request" in bot.edit_message_text.await_args.args[0]
    dm = [c for c in bot.send_message.await_args_list if c.args[0] == 10]
    assert dm and "extended until" in dm[0].args[1]

    # second admin is rejected
    ok2, msg2 = await service.apply_decision(pending, "1m", ADMIN_ID)
    assert not ok2 and "Already" in msg2


@pytest.mark.asyncio
async def test_default_decision_uses_chat_default(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db, days=3)
    await db.update_chat(-1, default_duration="2w")
    chat = await db.get_chat(-1)
    await service.request_renewal(chat, member)
    pending = await db.get_pending_for(-1, 10)
    ok, _ = await service.apply_decision(pending, "default", OWNER_ID)
    assert ok
    m = await db.get_member(-1, 10)
    assert abs((m.expires_at - (member.expires_at + timedelta(days=14))).total_seconds()) < 2


@pytest.mark.asyncio
async def test_decline_keeps_expiry_and_tells_member(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db, days=3)
    await service.request_renewal(chat, member)
    pending = await db.get_pending_for(-1, 10)
    bot.send_message.reset_mock()
    ok, outcome = await service.apply_decision(pending, "remove", OWNER_ID, "Owner")
    assert ok and "declined" in outcome
    m = await db.get_member(-1, 10)
    assert m.expires_at == member.expires_at and m.status == "active"
    dm = [c for c in bot.send_message.await_args_list if c.args[0] == 10]
    assert dm and "declined" in dm[0].args[1]
    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_decision_keeps_pending(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db)
    await service.request_renewal(chat, member)
    pending = await db.get_pending_for(-1, 10)
    ok, msg = await service.apply_decision(pending, "banana", OWNER_ID)
    assert not ok and "Invalid" in msg
    assert (await db.get_pending(pending.id)).status == "pending"


@pytest.mark.asyncio
async def test_approve_after_removal_unbans_and_asks_to_rejoin(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db, days=-1)
    await db.set_member_status(-1, 10, "expired")
    member = await db.get_member(-1, 10)
    ok, _ = await service.request_renewal(chat, member)
    assert ok
    pending = await db.get_pending_for(-1, 10)
    bot.send_message.reset_mock()
    ok, outcome = await service.apply_decision(pending, "1m", OWNER_ID)
    assert ok and "rejoin" in outcome
    bot.unban_chat_member.assert_awaited_once()
    m = await db.get_member(-1, 10)
    assert m.status == "active" and m.expires_at > NOW + timedelta(days=25)
    dm = [c for c in bot.send_message.await_args_list if c.args[0] == 10]
    assert dm and "rejoin" in dm[0].args[1]


@pytest.mark.asyncio
async def test_stale_renewal_closes_without_touching_expiry(env):
    db, bot, service, _, _ = env
    chat, member = await _setup(db)
    await service.request_renewal(chat, member)
    pending = await db.get_pending_for(-1, 10)
    await db._exec(
        "UPDATE pending_joins SET created_at=? WHERE id=?",
        ((NOW - timedelta(hours=30)).isoformat(), pending.id),
    )
    bot.send_message.reset_mock()
    assert await service.expire_stale_prompts() == 1
    assert (await db.get_pending(pending.id)).status == "expired"
    assert (await db.get_member(-1, 10)).expires_at == member.expires_at
    assert "expiry unchanged" in bot.edit_message_text.await_args.args[0]
    dm = [c for c in bot.send_message.await_args_list if c.args[0] == 10]
    assert dm and "Nobody answered" in dm[0].args[1]


# -------------------------------------------------------- reminders & screens
@pytest.mark.asyncio
async def test_reminder_carries_request_button_only_when_enabled(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.upsert_member(-1, 10, "Soon", None, NOW + timedelta(hours=20))
    assert (await scheduler.run_once())["reminded"] == 1
    kb = bot.send_message.await_args.kwargs["reply_markup"]
    assert kb is not None and _data(kb) == ["rq:-1"]

    bot.send_message.reset_mock()
    await db.update_chat(-1, renewals_enabled=0)
    await db.upsert_member(-1, 11, "Soon2", None, NOW + timedelta(hours=20))
    assert (await scheduler.run_once())["reminded"] == 1
    assert bot.send_message.await_args.kwargs["reply_markup"] is None
    assert "Contact an admin" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_expired_dm_has_button_in_kick_mode_only(env):
    db, bot, service, scheduler, _ = env
    await db.upsert_chat(-1, "G", "supergroup", None)
    await db.upsert_member(-1, 10, "Gone", None, NOW - timedelta(minutes=1))
    assert (await scheduler.run_once())["removed"] == 1
    assert _data(bot.send_message.await_args.kwargs["reply_markup"]) == ["rq:-1"]

    bot.send_message.reset_mock()
    await db.update_chat(-1, kick_mode="ban")
    await db.upsert_member(-1, 11, "Gone2", None, NOW - timedelta(minutes=1))
    assert (await scheduler.run_once())["removed"] == 1
    assert bot.send_message.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_mystatus_screen_lists_renewable_chats(env):
    db, bot, service, _, settings = env
    await db.upsert_chat(-1, "Gold", "supergroup", None)
    await db.upsert_chat(-2, "Silver", "supergroup", None)
    await db.update_chat(-2, renewals_enabled=0)
    await db.upsert_member(-1, 10, "A", None, NOW + timedelta(days=3))
    await db.upsert_member(-2, 10, "A", None, NOW + timedelta(days=3))
    text, kb = await mystatus_screen(db, settings, 10, service=service)
    assert "Gold" in text and "Silver" in text
    assert _data(kb) == ["rq:-1", "mystatus", "home"]
    assert "Need more time" in text

    # once a request is open the button disappears
    chat = await db.get_chat(-1)
    await service.request_renewal(chat, await db.get_member(-1, 10))
    _, kb = await mystatus_screen(db, settings, 10, service=service)
    assert "rq:-1" not in _data(kb)

    text, kb = await mystatus_screen(db, settings, 42, service=service)
    assert "no tracked" in text and _data(kb) == ["mystatus", "home"]


# ---------------------------------------------------------------- keyboards
def test_renewal_prompt_keyboard_shape():
    kb = K.renewal_prompt_keyboard(7, "1m")
    texts = _buttons(kb)
    assert texts[0] == "✅ Extend · 1 month"
    assert all(t.startswith("+") or t == "♾ Lifetime" for t in texts[1:-2]), texts
    assert "1 month" not in texts[1:]
    assert texts[-2:] == ["⋯ More options", "🚫 Decline"]
    assert _data(kb)[0] == "jd:7:default" and _data(kb)[-1] == "jr:7"


def test_confirm_wording_per_kind():
    assert _buttons(K.join_remove_confirm_keyboard(1))[0] == "✅ Yes, remove"
    assert _buttons(K.join_remove_confirm_keyboard(1, is_request=True))[0] == "✅ Yes, reject"
    assert _buttons(K.join_remove_confirm_keyboard(1, is_renewal=True))[0] == "✅ Yes, decline"


def test_advanced_keyboard_has_renewals_toggle():
    on = _buttons(K.advanced_keyboard(_chat(renewals_enabled=True)))
    off = _buttons(K.advanced_keyboard(_chat(renewals_enabled=False)))
    assert any(t.startswith("✅") and "Renewal" in t for t in on)
    assert any(t.startswith("☑️") and "Renewal" in t for t in off) or any(
        not t.startswith("✅") and "Renewal" in t for t in off
    )
    assert "set:-100:renewals" in _data(K.advanced_keyboard(_chat()))


def test_mystatus_keyboard_caps_and_clips():
    renewable = [(i, "A very long chat title that goes on " * 3) for i in range(12)]
    kb = K.mystatus_keyboard(renewable)
    assert len(kb.inline_keyboard) == 9  # 8 chats + nav row
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.text) <= 64
