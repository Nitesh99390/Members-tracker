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
from bot.utils.keyboards import join_prompt_keyboard
from bot.utils.permissions import refresh_chat_admins
from bot.utils.timeparse import describe_duration, format_dt

log = logging.getLogger(__name__)
router = Router(name="tracking")

TRACKED_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


# ------------------------------------------------------------- bot membership
@router.my_chat_member()
async def on_bot_membership(
    event: ChatMemberUpdated, bot: Bot, db: Database, service: MembershipService, settings: Settings
) -> None:
    chat = event.chat
    if chat.type not in TRACKED_TYPES:
        return
    new = event.new_chat_member
    actor = event.from_user
    actor_id = actor.id if actor and not actor.is_bot else None

    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.add_log(chat.id, None, "bot_removed", None, actor_id)
        log.info("Bot removed from %s (%s)", chat.title, chat.id)
        if await db.get_chat(chat.id):
            await db.update_chat(chat.id, tracking_enabled=0)
        return

    record = await db.upsert_chat(chat.id, chat.title, chat.type, chat.username, actor_id)
    await db.update_chat(chat.id, tracking_enabled=1)
    admin_ids = await refresh_chat_admins(bot, db, chat.id)
    owner_id = await service.get_owner_id(record, refresh=True)
    await db.add_log(chat.id, None, "bot_added", new.status, actor_id)

    is_admin_now = new.status == ChatMemberStatus.ADMINISTRATOR
    can_restrict = is_admin_now and bool(getattr(new, "can_restrict_members", False))
    can_invite = is_admin_now and bool(getattr(new, "can_invite_users", False))

    problems: list[str] = []
    if not is_admin_now:
        problems.append("Promote me to <b>Administrator</b> — Telegram only sends join/leave events to admin bots.")
    elif not can_restrict:
        problems.append("Grant me the <b>Ban users</b> right so I can remove expired members.")
    if is_admin_now and not can_invite:
        problems.append("Optional: <b>Invite users via link</b> lets me create duration-bound invite links.")

    text = (
        f"✅ <b>Now tracking {escape(record.display)}</b>\n"
        f"Type: {chat.type} • Default duration: <b>{describe_duration(service.effective_duration(record))}</b>\n"
        f"🔔 Ask on join: <b>{'ON' if service.ask_enabled(record) else 'OFF'}</b>"
        + ("\n\n⚠️ " + "\n⚠️ ".join(problems) if problems else "")
        + "\n\nManage it from my private chat with /panel."
    )

    # Notify the admin who added the bot privately (works for channels too)
    notified: set[int] = set()
    if actor_id:
        await db.set_context(actor_id, chat.id)
        if await service.dm_user(actor_id, text):
            notified.add(actor_id)
    if owner_id and owner_id not in notified:
        await db.set_context(owner_id, chat.id)
        if await service.dm_user(owner_id, text):
            notified.add(owner_id)
    if not notified and chat.type != ChatType.CHANNEL:
        # nobody reachable in private → post once in the group
        await service.safe_send(chat.id, text)
    log.info(
        "Tracking %s (%s) owner=%s admins=%d notified=%s",
        record.display, chat.id, owner_id, len(admin_ids), sorted(notified),
    )


# ------------------------------------------------------------- member events
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_join(
    event: ChatMemberUpdated, bot: Bot, db: Database, service: MembershipService, settings: Settings
) -> None:
    if event.chat.type not in TRACKED_TYPES:
        return
    chat_rec = await db.get_chat(event.chat.id)
    if chat_rec is None:
        chat_rec = await db.upsert_chat(
            event.chat.id, event.chat.title, event.chat.type, event.chat.username
        )
    if not chat_rec.tracking_enabled:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return

    existing = await db.get_member(chat_rec.chat_id, user.id)
    if (
        getattr(event, "via_join_request", False)
        and existing
        and existing.is_active
        and existing.source == "request"
    ):
        # already decided by an admin while approving the join request
        await db.update_member_profile(chat_rec.chat_id, user.id, user.full_name, user.username)
        return

    actor = event.from_user.id if event.from_user and event.from_user.id != user.id else None
    invite = event.invite_link.invite_link if event.invite_link else None
    member = await service.track_join(chat_rec, user, actor_id=actor, invite_link=invite)
    if member is None:
        return
    if chat_rec.welcome_enabled and event.chat.type != ChatType.CHANNEL:
        template = chat_rec.welcome_text or (
            "👋 Welcome {mention}!\n⏳ Your membership is valid until <b>{expires}</b>."
        )
        try:
            text = template.format(
                mention=member.mention_html,
                name=escape(user.full_name),
                expires=format_dt(member.expires_at, settings.tz),
                chat=escape(chat_rec.display),
            )
        except (KeyError, IndexError, ValueError):
            text = f"👋 Welcome {member.mention_html}!"
        await service.safe_send(event.chat.id, text)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_member_leave(
    event: ChatMemberUpdated, db: Database, service: MembershipService
) -> None:
    chat_rec = await db.get_chat(event.chat.id)
    if chat_rec is None:
        return
    user = event.new_chat_member.user
    status = "kicked" if event.new_chat_member.status == ChatMemberStatus.KICKED else "left"
    member = await db.get_member(event.chat.id, user.id)
    if member and member.is_active:
        await service.track_leave(chat_rec, user, status)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_MEMBER))
async def on_member_promote(
    event: ChatMemberUpdated, bot: Bot, db: Database, service: MembershipService
) -> None:
    """Admin promoted / demoted → refresh admin cache (and owner)."""
    old, new = event.old_chat_member.status, event.new_chat_member.status
    admin_states = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    if (old in admin_states) != (new in admin_states):
        await refresh_chat_admins(bot, db, event.chat.id)
        chat = await db.get_chat(event.chat.id)
        if chat:
            await service.get_owner_id(chat, refresh=True)
            # newly promoted admins should never be auto-removed
            if new in admin_states:
                member = await db.get_member(chat.chat_id, event.new_chat_member.user.id)
                if member and member.is_active and member.expires_at is not None:
                    await db.set_member_expiry(chat.chat_id, member.user_id, None)
                    await db.add_log(chat.chat_id, member.user_id, "promoted_permanent")


@router.chat_join_request()
async def on_join_request(
    event: ChatJoinRequest, bot: Bot, db: Database, service: MembershipService, settings: Settings
) -> None:
    chat_rec = await db.get_chat(event.chat.id)
    if not chat_rec or not chat_rec.tracking_enabled or not chat_rec.approve_requests:
        return
    user = event.from_user
    if chat_rec.approve_requests == 1:
        try:
            await event.approve()
            await db.add_log(event.chat.id, user.id, "request_approved", "auto")
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot approve join request in %s: %s", event.chat.id, exc)
        return

    # mode 2: ask owner/admins to approve with a duration
    recipients = await service.prompt_recipients(chat_rec)
    if not recipients:
        return
    pending = await db.create_pending(
        chat_rec.chat_id, user.id, user.full_name, user.username, source="request"
    )
    uname = f" (@{escape(user.username)})" if user.username else ""
    bio = f"\n💬 Bio: <i>{escape(event.bio)}</i>" if getattr(event, "bio", None) else ""
    text = (
        f"🙋 <b>Join request</b>\n\n"
        f"👤 {pending.mention_html}{uname}\n🆔 <code>{user.id}</code>{bio}\n"
        f"📍 <b>{escape(chat_rec.display)}</b>\n\n"
        f"Approve and choose how long they may stay, or reject.\n"
        f"<i>Default: {describe_duration(service.effective_duration(chat_rec))}</i>"
    )
    delivered = 0
    for admin_id in recipients:
        msg = await service.safe_send(admin_id, text, reply_markup=join_prompt_keyboard(pending.id))
        if msg:
            delivered += 1
            await db.add_prompt_message(pending.id, admin_id, msg.message_id)
    if not delivered:
        await db.resolve_pending(pending.id, None, "undeliverable", status="cancelled")
    await db.add_log(event.chat.id, user.id, "request_prompt", f"to {delivered} admin(s)")


# Fallback for groups where chat_member updates are not delivered
@router.message(F.new_chat_members, F.chat.type.in_(GROUP_TYPES))
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
        if existing and existing.is_active:
            continue  # already tracked via chat_member update
        actor = message.from_user.id if message.from_user and message.from_user.id != user.id else None
        await service.track_join(chat_rec, user, actor_id=actor)


@router.message(F.left_chat_member, F.chat.type.in_(GROUP_TYPES))
async def on_left_chat_member_message(
    message: Message, db: Database, service: MembershipService
) -> None:
    chat_rec = await db.get_chat(message.chat.id)
    if chat_rec is None or message.left_chat_member is None:
        return
    member = await db.get_member(message.chat.id, message.left_chat_member.id)
    if member and member.is_active:
        await service.track_leave(chat_rec, message.left_chat_member, "left")


# Keep names/usernames fresh when tracked users talk; pick up pre-existing members
@router.message(F.chat.type.in_(GROUP_TYPES), F.from_user)
async def on_group_message(message: Message, db: Database, service: MembershipService) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    chat_rec = await db.get_chat(message.chat.id)
    if not chat_rec or not chat_rec.tracking_enabled:
        return
    member = await db.get_member(message.chat.id, user.id)
    if member is None:
        # user was present before the bot joined → start tracking from now (no prompt spam)
        await service.track_join(chat_rec, user, source="existing", expires_at=service.compute_expiry(chat_rec))
        return
    if member.full_name != user.full_name or member.username != user.username:
        await db.update_member_profile(message.chat.id, user.id, user.full_name, user.username)
