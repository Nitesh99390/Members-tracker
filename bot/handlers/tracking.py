"""Passive tracking: bot added/removed, members join/leave, join requests."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import ChatMemberUpdatedFilter, IS_MEMBER, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message

from bot.config import Settings
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.utils.permissions import refresh_chat_admins
from bot.utils.timeparse import format_dt

log = logging.getLogger(__name__)
router = Router(name="tracking")

TRACKED_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}


# ------------------------------------------------------------- bot membership
@router.my_chat_member()
async def on_bot_membership(
    event: ChatMemberUpdated, bot: Bot, db: Database, service: MembershipService, settings: Settings
) -> None:
    chat = event.chat
    if chat.type not in TRACKED_TYPES:
        return
    new = event.new_chat_member
    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.add_log(chat.id, None, "bot_removed", None, event.from_user.id)
        log.info("Bot removed from %s (%s)", chat.title, chat.id)
        await db.update_chat(chat.id, tracking_enabled=0)
        return

    record = await db.upsert_chat(chat.id, chat.title, chat.type, chat.username, event.from_user.id)
    await db.update_chat(chat.id, tracking_enabled=1)
    await refresh_chat_admins(bot, db, chat.id)
    await db.add_log(chat.id, None, "bot_added", new.status, event.from_user.id)

    can_restrict = new.status == ChatMemberStatus.ADMINISTRATOR and getattr(
        new, "can_restrict_members", False
    )
    warning = "" if can_restrict else (
        "\n\n⚠️ <b>I need admin rights with <i>Ban users</i> permission</b> to remove expired members."
    )
    text = (
        f"✅ <b>Now tracking {escape(record.display)}</b>\n"
        f"Type: {chat.type} • Default duration: <b>{service.effective_duration(record)}</b>"
        f"{warning}\n\n"
        f"Manage it from my private chat with /panel."
    )
    # Notify the admin who added the bot privately (works for channels too)
    if not event.from_user.is_bot:
        await db.set_context(event.from_user.id, chat.id)
        await service.dm_user(event.from_user.id, text)
    if chat.type != ChatType.CHANNEL:
        try:
            await bot.send_message(chat.id, text)
        except Exception as exc:  # noqa: BLE001
            log.debug("Cannot greet chat %s: %s", chat.id, exc)


# ------------------------------------------------------------- member events
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_join(
    event: ChatMemberUpdated, bot: Bot, db: Database, service: MembershipService, settings: Settings
) -> None:
    chat_rec = await db.get_chat(event.chat.id)
    if chat_rec is None:
        chat_rec = await db.upsert_chat(
            event.chat.id, event.chat.title, event.chat.type, event.chat.username
        )
    if not chat_rec.tracking_enabled:
        return
    user = event.new_chat_member.user
    actor = event.from_user.id if event.from_user and event.from_user.id != user.id else None
    member = await service.track_join(chat_rec, user, actor_id=actor)
    if member is None:
        return
    if chat_rec.welcome_enabled and event.chat.type != ChatType.CHANNEL:
        template = chat_rec.welcome_text or (
            "👋 Welcome {mention}!\n⏳ Your membership is valid until <b>{expires}</b>."
        )
        text = template.format(
            mention=member.mention_html,
            name=escape(user.full_name),
            expires=format_dt(member.expires_at, settings.tz),
            chat=escape(chat_rec.display),
        )
        try:
            await bot.send_message(event.chat.id, text)
        except Exception as exc:  # noqa: BLE001
            log.debug("Welcome failed in %s: %s", event.chat.id, exc)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_member_leave(
    event: ChatMemberUpdated, db: Database, service: MembershipService
) -> None:
    chat_rec = await db.get_chat(event.chat.id)
    if chat_rec is None:
        return
    user = event.new_chat_member.user
    status = "kicked" if event.new_chat_member.status == ChatMemberStatus.KICKED else "left"
    # If we removed them ourselves the status is already set; track_leave is idempotent-ish
    member = await db.get_member(event.chat.id, user.id)
    if member and member.status == "active":
        await service.track_leave(chat_rec, user, status)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_MEMBER))
async def on_member_promote(event: ChatMemberUpdated, bot: Bot, db: Database) -> None:
    """Admin promoted / demoted → refresh admin cache."""
    old, new = event.old_chat_member.status, event.new_chat_member.status
    admin_states = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    if (old in admin_states) != (new in admin_states):
        await refresh_chat_admins(bot, db, event.chat.id)


@router.chat_join_request()
async def on_join_request(
    event: ChatJoinRequest, bot: Bot, db: Database, service: MembershipService
) -> None:
    chat_rec = await db.get_chat(event.chat.id)
    if not chat_rec or not chat_rec.approve_requests:
        return
    try:
        await event.approve()
        await db.add_log(event.chat.id, event.from_user.id, "request_approved")
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot approve join request in %s: %s", event.chat.id, exc)


# Fallback for groups where chat_member updates are not delivered (e.g. missing allowed_updates)
@router.message(F.new_chat_members, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_new_chat_members_message(
    message: Message, db: Database, service: MembershipService
) -> None:
    chat_rec = await db.get_chat(message.chat.id)
    if chat_rec is None:
        chat_rec = await db.upsert_chat(
            message.chat.id, message.chat.title, message.chat.type, message.chat.username
        )
    if not chat_rec.tracking_enabled:
        return
    for user in message.new_chat_members or []:
        existing = await db.get_member(message.chat.id, user.id)
        if existing and existing.status == "active":
            continue  # already tracked via chat_member update
        actor = message.from_user.id if message.from_user and message.from_user.id != user.id else None
        await service.track_join(chat_rec, user, actor_id=actor)


@router.message(F.left_chat_member, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_left_chat_member_message(
    message: Message, db: Database, service: MembershipService
) -> None:
    chat_rec = await db.get_chat(message.chat.id)
    if chat_rec is None or message.left_chat_member is None:
        return
    member = await db.get_member(message.chat.id, message.left_chat_member.id)
    if member and member.status == "active":
        await service.track_leave(chat_rec, message.left_chat_member, "left")


# Keep names/usernames fresh when tracked users talk
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.from_user)
async def on_group_message(message: Message, db: Database, service: MembershipService) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    chat_rec = await db.get_chat(message.chat.id)
    if not chat_rec or not chat_rec.tracking_enabled:
        return
    member = await db.get_member(message.chat.id, user.id)
    if member is None:
        # user was present before the bot joined → start tracking from now
        await service.track_join(chat_rec, user)
        return
    if member.full_name != user.full_name or member.username != user.username:
        await db.update_member_profile(message.chat.id, user.id, user.full_name, user.username)
