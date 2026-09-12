"""Business logic shared by handlers and the scheduler."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import User

from bot.config import Settings
from bot.services.database import Chat, Database, Member
from bot.utils.timeparse import (
    ParseError,
    apply_duration,
    format_dt,
    humanize_delta,
    is_permanent,
    parse_date,
    parse_expiry,
)

log = logging.getLogger(__name__)


class MembershipService:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings

    # ---------------------------------------------------------------- helpers
    def effective_duration(self, chat: Chat) -> str:
        return chat.default_duration or self.settings.default_duration

    def compute_expiry(self, chat: Chat, start: datetime | None = None) -> datetime | None:
        duration = self.effective_duration(chat)
        if is_permanent(duration):
            return None
        start = start or datetime.now(timezone.utc)
        try:
            return apply_duration(start, duration)
        except ParseError:
            log.error("Invalid duration %r for chat %s, falling back to 30d", duration, chat.chat_id)
            return apply_duration(start, "30d")

    def parse_user_expiry(self, text: str) -> datetime | None:
        """Parse admin-provided expiry text. Returns None for permanent."""
        if is_permanent(text):
            return None
        return parse_expiry(text, self.settings.tz)

    async def send_log(self, chat: Chat, text: str) -> None:
        if not chat.log_chat_id:
            return
        try:
            await self.bot.send_message(chat.log_chat_id, text, disable_web_page_preview=True)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("Cannot send log to %s: %s", chat.log_chat_id, exc)

    async def dm_user(self, user_id: int, text: str) -> bool:
        try:
            await self.bot.send_message(user_id, text, disable_web_page_preview=True)
            return True
        except (TelegramBadRequest, TelegramForbiddenError):
            return False

    # ------------------------------------------------------------ operations
    async def track_join(
        self, chat: Chat, user: User, actor_id: int | None = None, expires_at: datetime | None = ...
    ) -> Member | None:
        """Register a user who joined ``chat``. Returns the Member or None if skipped."""
        if user.is_bot:
            return None
        if await self.db.is_whitelisted(chat.chat_id, user.id) or await self.db.is_chat_admin(
            chat.chat_id, user.id
        ):
            member = await self.db.upsert_member(
                chat.chat_id, user.id, user.full_name, user.username, None, actor_id
            )
            await self.db.add_log(chat.chat_id, user.id, "join_whitelisted", None, actor_id)
            return member
        if expires_at is ...:
            expires_at = self.compute_expiry(chat)
        member = await self.db.upsert_member(
            chat.chat_id, user.id, user.full_name, user.username, expires_at, actor_id
        )
        await self.db.add_log(
            chat.chat_id,
            user.id,
            "join",
            f"expires={expires_at.isoformat() if expires_at else 'never'}",
            actor_id,
        )
        await self.send_log(
            chat,
            f"➕ <b>Joined</b> {member.mention_html} [<code>{user.id}</code>]\n"
            f"📍 {escape(chat.display)}\n"
            f"⏳ Expires: <b>{format_dt(expires_at, self.settings.tz)}</b>",
        )
        return member

    async def track_leave(self, chat: Chat, user: User, status: str = "left") -> None:
        member = await self.db.get_member(chat.chat_id, user.id)
        if not member:
            return
        await self.db.set_member_status(chat.chat_id, user.id, status)
        await self.db.add_log(chat.chat_id, user.id, status)
        await self.send_log(
            chat,
            f"➖ <b>Left</b> {member.mention_html} [<code>{user.id}</code>]\n📍 {escape(chat.display)}",
        )

    async def remove_member(
        self, chat: Chat, member: Member, reason: str = "expired", actor_id: int | None = None
    ) -> tuple[bool, str]:
        """Kick or ban a member. Returns (success, message)."""
        try:
            await self.bot.ban_chat_member(chat.chat_id, member.user_id)
            if chat.kick_mode == "kick":
                # unban so they can rejoin later (kick semantics)
                await self.bot.unban_chat_member(chat.chat_id, member.user_id, only_if_banned=True)
        except TelegramRetryAfter as exc:
            return False, f"rate limited, retry in {exc.retry_after}s"
        except TelegramForbiddenError as exc:
            return False, f"forbidden: {exc.message}"
        except TelegramBadRequest as exc:
            msg = exc.message.lower()
            if "not enough rights" in msg or "can't remove chat owner" in msg or "administrator" in msg:
                return False, f"insufficient rights: {exc.message}"
            if "user not found" in msg or "participant_id_invalid" in msg or "user_not_participant" in msg:
                # already gone -> treat as success
                await self.db.set_member_status(chat.chat_id, member.user_id, reason)
                return True, "user already not in chat"
            return False, exc.message

        await self.db.set_member_status(chat.chat_id, member.user_id, reason)
        await self.db.add_log(
            chat.chat_id, member.user_id, f"removed_{reason}", chat.kick_mode, actor_id
        )
        action = "Banned" if chat.kick_mode == "ban" else "Removed"
        await self.send_log(
            chat,
            f"🚫 <b>{action}</b> {member.mention_html} [<code>{member.user_id}</code>]\n"
            f"📍 {escape(chat.display)}\n"
            f"📋 Reason: {escape(reason)}",
        )
        if chat.notify_user and reason == "expired":
            await self.dm_user(
                member.user_id,
                f"⌛ <b>Membership expired</b>\n\n"
                f"Your access to <b>{escape(chat.display)}</b> has ended and you have been removed.\n"
                f"Contact an admin to renew your membership.",
            )
        return True, "ok"

    async def extend_member(
        self, chat: Chat, member: Member, expiry_text: str, actor_id: int | None
    ) -> datetime | None:
        """Extend from *current* expiry (or now if expired/none) by duration, or set absolute date."""
        if is_permanent(expiry_text):
            new_expiry: datetime | None = None
        else:
            now = datetime.now(timezone.utc)
            try:
                # absolute date wins
                new_expiry = parse_date(expiry_text, self.settings.tz)
            except ParseError:
                # relative duration: extend from current expiry (or now if already past)
                base = member.expires_at if member.expires_at and member.expires_at > now else now
                new_expiry = apply_duration(base, expiry_text)
            if new_expiry <= now:
                raise ParseError("Expiry must be in the future")
        await self.db.set_member_expiry(chat.chat_id, member.user_id, new_expiry)
        await self.db.add_log(
            chat.chat_id,
            member.user_id,
            "extend",
            f"to={new_expiry.isoformat() if new_expiry else 'never'}",
            actor_id,
        )
        await self.send_log(
            chat,
            f"🔄 <b>Extended</b> {member.mention_html} [<code>{member.user_id}</code>]\n"
            f"📍 {escape(chat.display)}\n"
            f"⏳ New expiry: <b>{format_dt(new_expiry, self.settings.tz)}</b>",
        )
        return new_expiry

    async def set_expiry(
        self, chat: Chat, member: Member, expiry_text: str, actor_id: int | None
    ) -> datetime | None:
        new_expiry = self.parse_user_expiry(expiry_text)
        await self.db.set_member_expiry(chat.chat_id, member.user_id, new_expiry)
        await self.db.add_log(
            chat.chat_id,
            member.user_id,
            "set_expiry",
            f"to={new_expiry.isoformat() if new_expiry else 'never'}",
            actor_id,
        )
        await self.send_log(
            chat,
            f"📝 <b>Expiry set</b> {member.mention_html} [<code>{member.user_id}</code>]\n"
            f"📍 {escape(chat.display)}\n"
            f"⏳ Expires: <b>{format_dt(new_expiry, self.settings.tz)}</b>",
        )
        return new_expiry

    # -------------------------------------------------------------- formatting
    def member_card(self, chat: Chat, member: Member) -> str:
        tz = self.settings.tz
        now = datetime.now(timezone.utc)
        if member.expires_at is None:
            remaining = "♾ Permanent"
        else:
            remaining = humanize_delta(member.expires_at - now)
        status_icon = {
            "active": "🟢",
            "left": "⚪",
            "kicked": "🔴",
            "expired": "⌛",
        }.get(member.status, "❔")
        lines = [
            f"👤 <b>{escape(member.full_name or 'Unknown')}</b>"
            + (f" (@{escape(member.username)})" if member.username else ""),
            f"🆔 <code>{member.user_id}</code>",
            f"📍 {escape(chat.display)}",
            f"{status_icon} Status: <b>{member.status}</b>",
            f"📅 Joined: {format_dt(member.joined_at, tz)}",
            f"⏳ Expires: <b>{format_dt(member.expires_at, tz)}</b>",
            f"⏱ Remaining: <b>{remaining}</b>",
        ]
        if member.note:
            lines.append(f"📝 Note: {escape(member.note)}")
        return "\n".join(lines)

    async def resolve_user(self, chat_id: int, token: str) -> tuple[int | None, str | None]:
        """Resolve a user ID or @username to a user ID using the DB, then Telegram."""
        token = token.strip()
        if token.lstrip("-").isdigit():
            return int(token), None
        uname = token.lstrip("@")
        members = await self.db.search_members(chat_id, uname, limit=5)
        for m in members:
            if m.username and m.username.lower() == uname.lower():
                return m.user_id, None
        # fallback: try public resolution (works only for channels/users with visible usernames)
        try:
            chat = await self.bot.get_chat(f"@{uname}")
            if chat.type == "private":
                return chat.id, None
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return None, "User not found. Use a numeric user ID or reply to the user's message."
