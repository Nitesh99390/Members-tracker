"""Business logic shared by handlers and the scheduler.

Includes the *join prompt* workflow: when a member joins a tracked chat the bot
DMs the chat owner (or every admin) asking how long the member should stay.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup, User

from bot.config import Settings
from bot.services.database import Chat, Database, Member, PendingJoin
from bot.utils.keyboards import join_prompt_keyboard
from bot.utils.telegram import TelegramUnavailable, error_text, tg_call, tg_try
from bot.utils.timeparse import (
    ParseError,
    apply_duration,
    describe_duration,
    format_dt,
    humanize_delta,
    is_permanent,
    parse_date,
    parse_expiry,
)

log = logging.getLogger(__name__)


class MembershipService:
    # Telegram allows ~30 msg/s overall; keep a comfortable margin for DMs so a
    # broadcast or a burst of reminders never starves interactive replies.
    SEND_CONCURRENCY = 8

    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self._send_sem = asyncio.Semaphore(self.SEND_CONCURRENCY)
        self.sent_ok = 0
        self.sent_failed = 0

    # ---------------------------------------------------------------- helpers
    def effective_duration(self, chat: Chat) -> str:
        return chat.default_duration or self.settings.default_duration

    def ask_enabled(self, chat: Chat) -> bool:
        if chat.ask_on_join is None:
            return self.settings.ask_on_join_default
        return chat.ask_on_join

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

    def expiry_from_text(self, text: str, start: datetime | None = None) -> datetime | None:
        """Parse a duration / date / 'never'. Returns None for permanent."""
        if is_permanent(text):
            return None
        return parse_expiry(text, self.settings.tz, start)

    # backwards compatible alias
    parse_user_expiry = expiry_from_text

    async def safe_send(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        retries: int = 2,
        **kwargs: Any,
    ) -> Any | None:
        """Send a message, transparently handling flood limits and dead chats.

        Returns the sent ``Message`` or ``None`` when delivery is impossible
        (user blocked the bot, never started it, chat gone, ...).
        """
        async with self._send_sem:
            try:
                msg = await tg_call(
                    self.bot.send_message,
                    chat_id,
                    text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                    retries=retries,
                    label="send_message",
                    **kwargs,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                self.sent_failed += 1
                log.debug("Cannot send to %s: %s", chat_id, error_text(exc))
                return None
            except (TelegramRetryAfter, TelegramUnavailable) as exc:
                self.sent_failed += 1
                log.warning("Delivery to %s failed: %s", chat_id, exc)
                return None
        self.sent_ok += 1
        return msg

    async def send_log(self, chat: Chat, text: str) -> None:
        if not chat.log_chat_id:
            return
        await self.safe_send(chat.log_chat_id, text)

    async def dm_user(self, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
        return await self.safe_send(user_id, text, reply_markup=reply_markup) is not None

    async def get_owner_id(self, chat: Chat, refresh: bool = False) -> int | None:
        """Return the creator of the chat, caching it in the DB."""
        if chat.owner_id and not refresh:
            return chat.owner_id
        try:
            admins = await tg_call(self.bot.get_chat_administrators, chat.chat_id, retries=2, label="get_admins")
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnavailable) as exc:
            log.warning("Cannot fetch admins for %s: %s", chat.chat_id, exc)
            return chat.owner_id or chat.added_by
        for adm in admins:
            if adm.status == ChatMemberStatus.CREATOR:
                if adm.user.id != chat.owner_id:
                    await self.db.update_chat(chat.chat_id, owner_id=adm.user.id)
                    chat.owner_id = adm.user.id
                return adm.user.id
        return chat.owner_id or chat.added_by

    async def prompt_recipients(self, chat: Chat) -> list[int]:
        """Who should be asked about a new member: owner or all human admins."""
        recipients: list[int] = []
        owner = await self.get_owner_id(chat)
        if chat.ask_target == "admins":
            recipients.extend(await self.db.list_chat_admins(chat.chat_id))
        if owner:
            recipients.append(owner)
        if not recipients and chat.added_by:
            recipients.append(chat.added_by)
        # dedupe, keep order
        seen: set[int] = set()
        ordered = []
        for r in recipients:
            if r not in seen:
                seen.add(r)
                ordered.append(r)
        return ordered

    # -------------------------------------------------------------- join flow
    async def track_join(
        self,
        chat: Chat,
        user: User,
        actor_id: int | None = None,
        expires_at: datetime | None = ...,
        source: str | None = "join",
        invite_link: str | None = None,
    ) -> Member | None:
        """Register a user who joined ``chat``.

        Priority for the expiry:
        1. explicit ``expires_at`` argument
        2. preset duration of the invite link they used
        3. chat default duration (and, if enabled, ask the owner to confirm/change)
        """
        if user.is_bot:
            return None
        if await self.db.is_whitelisted(chat.chat_id, user.id) or await self.db.is_chat_admin(
            chat.chat_id, user.id
        ):
            member = await self.db.upsert_member(
                chat.chat_id, user.id, user.full_name, user.username, None, actor_id, source=source
            )
            await self.db.add_log(chat.chat_id, user.id, "join_whitelisted", None, actor_id)
            return member

        ask = False
        link_label = ""
        if expires_at is ...:
            preset = None
            if invite_link:
                link = await self.db.get_invite_link(invite_link)
                if link and not link.revoked:
                    preset = link.duration
                    link_label = link.name or "invite link"
                    await self.db.bump_invite_use(invite_link)
                    source = f"invite:{link.name or 'link'}"
            if preset is not None:
                try:
                    expires_at = self.expiry_from_text(preset)
                except ParseError:
                    expires_at = self.compute_expiry(chat)
            else:
                expires_at = self.compute_expiry(chat)
                ask = self.ask_enabled(chat)

        member = await self.db.upsert_member(
            chat.chat_id, user.id, user.full_name, user.username, expires_at, actor_id, source=source
        )
        await self.db.add_log(
            chat.chat_id,
            user.id,
            "join",
            f"expires={expires_at.isoformat() if expires_at else 'never'}"
            + (f" via {link_label}" if link_label else ""),
            actor_id,
        )
        await self.send_log(
            chat,
            f"➕ <b>Joined</b> {member.mention_html} [<code>{user.id}</code>]\n"
            f"📍 {escape(chat.display)}\n"
            + (f"🔗 Via: {escape(link_label)}\n" if link_label else "")
            + f"⏳ Expires: <b>{format_dt(expires_at, self.settings.tz)}</b>",
        )
        if ask:
            await self.ask_owner_about_member(chat, member, source="join")
        return member

    async def ask_owner_about_member(self, chat: Chat, member: Member, source: str = "join") -> PendingJoin | None:
        """DM the owner/admins asking how long ``member`` should stay in ``chat``."""
        recipients = await self.prompt_recipients(chat)
        if not recipients:
            log.info("No prompt recipients for chat %s", chat.chat_id)
            return None
        pending = await self.db.create_pending(
            chat.chat_id, member.user_id, member.full_name, member.username, source=source
        )
        text = self.join_prompt_text(chat, pending, member)
        kb = join_prompt_keyboard(pending.id, self.effective_duration(chat), source == "request")
        delivered = await self.deliver_prompt(pending.id, recipients, text, kb)
        if not delivered:
            # nobody can be reached (owner never started the bot) → keep default silently
            await self.db.resolve_pending(pending.id, None, "undeliverable", status="cancelled")
            await self.db.add_log(chat.chat_id, member.user_id, "prompt_undeliverable", None)
            return None
        await self.db.add_log(chat.chat_id, member.user_id, "prompt_sent", f"to {delivered} admin(s)")
        return pending

    async def deliver_prompt(
        self, pending_id: int, recipients: list[int], text: str, kb: InlineKeyboardMarkup
    ) -> int:
        """Send the same prompt to every recipient in parallel; returns how many got it."""
        results = await asyncio.gather(
            *(self.safe_send(admin_id, text, reply_markup=kb) for admin_id in recipients)
        )
        delivered = 0
        for admin_id, msg in zip(recipients, results):
            if msg:
                delivered += 1
                await self.db.add_prompt_message(pending_id, admin_id, msg.message_id)
        return delivered

    def join_prompt_text(self, chat: Chat, pending: PendingJoin, member: Member | None) -> str:
        icon = "📢" if chat.is_channel else "👥"
        default_label = describe_duration(self.effective_duration(chat))
        current = (
            format_dt(member.expires_at, self.settings.tz) if member else "—"
        )
        src = {
            "join": "joined",
            "request": "requested to join",
            "manual": "was added",
        }.get(pending.source, "joined")
        uname = f" · @{escape(pending.username)}" if pending.username else ""
        title = "Join request" if pending.source == "request" else f"New member {src}"
        return (
            f"🔔 <b>{title}</b>\n\n"
            f"👤 {pending.mention_html}{uname}\n"
            f"🆔 <code>{pending.user_id}</code>\n"
            f"{icon} {escape(chat.display)}\n\n"
            f"How long may they stay?\n"
            f"<i>Default <b>{default_label}</b> (until {current}) is kept if you don't answer within "
            f"{self.settings.ask_timeout_hours}h.</i>"
        )

    async def apply_join_decision(
        self, pending: PendingJoin, decision: str, actor_id: int, actor_name: str = ""
    ) -> tuple[bool, str]:
        """Apply an admin's decision to a pending join.

        ``decision`` is a duration text, ``never``, ``default`` or ``remove``.
        Returns (ok, human message).
        """
        chat = await self.db.get_chat(pending.chat_id)
        if not chat:
            await self.db.resolve_pending(pending.id, actor_id, "chat_missing", status="cancelled")
            return False, "Chat no longer tracked."

        # claim the pending row first so two admins cannot both act on it
        if not await self.db.resolve_pending(pending.id, actor_id, decision):
            return False, "Already handled by another admin."

        member = await self.db.get_member(chat.chat_id, pending.user_id)
        if member is None:
            member = await self.db.upsert_member(
                chat.chat_id, pending.user_id, pending.full_name, pending.username, None, actor_id, source=pending.source
            )

        if decision == "remove":
            ok, msg = await self.remove_member(chat, member, reason="manual", actor_id=actor_id)
            outcome = "🚫 Removed from the chat." if ok else f"❌ Could not remove: {msg}"
            await self._finalize_prompt(pending, chat, member, outcome, actor_name)
            return ok, outcome

        if decision == "default":
            new_expiry = self.compute_expiry(chat)
            label = describe_duration(self.effective_duration(chat))
        else:
            try:
                new_expiry = self.expiry_from_text(decision)
            except ParseError as exc:
                # roll back claim so the admin can retry
                await self.db._exec(
                    "UPDATE pending_joins SET status='pending', decided_at=NULL, decided_by=NULL, decision=NULL WHERE id=?",
                    (pending.id,),
                )
                return False, f"Invalid duration/date: {exc}"
            label = describe_duration(decision) if not is_permanent(decision) else "Lifetime"
            if new_expiry is not None and label == decision:
                label = format_dt(new_expiry, self.settings.tz)

        await self.db.set_member_expiry(chat.chat_id, member.user_id, new_expiry)
        await self.db.add_log(
            chat.chat_id,
            member.user_id,
            "join_decision",
            f"{decision} -> {new_expiry.isoformat() if new_expiry else 'never'}",
            actor_id,
        )
        outcome = f"✅ Set to <b>{escape(label)}</b> — until <b>{format_dt(new_expiry, self.settings.tz)}</b>"
        await self._finalize_prompt(pending, chat, member, outcome, actor_name)
        await self.send_log(
            chat,
            f"🗓 <b>Duration decided</b> {member.mention_html} [<code>{member.user_id}</code>]\n"
            f"📍 {escape(chat.display)}\n⏳ Until: <b>{format_dt(new_expiry, self.settings.tz)}</b>\n"
            f"👮 By: <code>{actor_id}</code>",
        )
        if chat.notify_user and member.status == "active":
            if new_expiry is None:
                body = "has <b>lifetime</b> access 🎉"
            else:
                body = f"is valid until <b>{format_dt(new_expiry, self.settings.tz)}</b>"
            await self.dm_user(
                member.user_id,
                f"✅ Your membership in <b>{escape(chat.display)}</b> {body}.",
            )
        return True, outcome

    async def apply_request_decision(
        self, pending: PendingJoin, decision: str, actor_id: int, actor_name: str = ""
    ) -> tuple[bool, str]:
        """Approve (with a duration) or reject a *join request* that admins were asked about.

        ``decision`` is a duration text, ``never``, ``default`` or ``remove`` (=reject).
        """
        chat = await self.db.get_chat(pending.chat_id)
        if not chat:
            await self.db.resolve_pending(pending.id, actor_id, "chat_missing", status="cancelled")
            return False, "Chat no longer tracked."

        # validate before claiming so the admin can retry on typos
        expires_at: datetime | None = None
        label = ""
        if decision == "default":
            expires_at = self.compute_expiry(chat)
            label = describe_duration(self.effective_duration(chat))
        elif decision != "remove":
            try:
                expires_at = self.expiry_from_text(decision)
            except ParseError as exc:
                return False, f"Invalid duration/date: {exc}"
            label = "Lifetime" if is_permanent(decision) else describe_duration(decision)
            if expires_at is not None and label == decision:
                label = format_dt(expires_at, self.settings.tz)

        if not await self.db.resolve_pending(pending.id, actor_id, decision):
            return False, "Already handled by another admin."

        placeholder = Member(
            chat_id=chat.chat_id,
            user_id=pending.user_id,
            full_name=pending.full_name,
            username=pending.username,
            joined_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            status="active",
            note=None,
            reminders_sent=set(),
            added_by=actor_id,
        )

        if decision == "remove":
            try:
                await self.bot.decline_chat_join_request(chat.chat_id, pending.user_id)
                outcome = "🚫 Join request rejected."
                ok = True
            except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as exc:
                outcome = f"❌ Could not reject: {escape(str(getattr(exc, 'message', exc)))}"
                ok = False
            await self.db.add_log(chat.chat_id, pending.user_id, "request_rejected", None, actor_id)
            await self._finalize_prompt(pending, chat, placeholder, outcome, actor_name)
            return ok, outcome

        try:
            await self.bot.approve_chat_join_request(chat.chat_id, pending.user_id)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as exc:
            msg = str(getattr(exc, "message", exc)).lower()
            if "user_already_participant" in msg or "already" in msg:
                pass  # they got in another way; still record the membership
            else:
                outcome = f"❌ Could not approve: {escape(str(getattr(exc, 'message', exc)))}"
                await self._finalize_prompt(pending, chat, placeholder, outcome, actor_name)
                return False, outcome

        member = await self.db.upsert_member(
            chat.chat_id, pending.user_id, pending.full_name, pending.username, expires_at, actor_id, source="request"
        )
        await self.db.add_log(
            chat.chat_id,
            pending.user_id,
            "request_approved",
            f"{decision} -> {expires_at.isoformat() if expires_at else 'never'}",
            actor_id,
        )
        outcome = f"✅ Approved — <b>{escape(label)}</b>, until <b>{format_dt(expires_at, self.settings.tz)}</b>"
        await self._finalize_prompt(pending, chat, member, outcome, actor_name)
        await self.send_log(
            chat,
            f"🙋 <b>Request approved</b> {member.mention_html} [<code>{member.user_id}</code>]\n"
            f"📍 {escape(chat.display)}\n⏳ Until: <b>{format_dt(expires_at, self.settings.tz)}</b>\n"
            f"👮 By: <code>{actor_id}</code>",
        )
        if chat.notify_user:
            body = "has <b>lifetime</b> access 🎉" if expires_at is None else (
                f"is valid until <b>{format_dt(expires_at, self.settings.tz)}</b>"
            )
            await self.dm_user(
                member.user_id,
                f"✅ You were approved to join <b>{escape(chat.display)}</b>. Your membership {body}.",
            )
        return True, outcome

    async def _finalize_prompt(
        self, pending: PendingJoin, chat: Chat, member: Member, outcome: str, actor_name: str
    ) -> None:
        """Edit every admin's prompt message to show the decision and drop the buttons."""
        uname = f" (@{escape(pending.username)})" if pending.username else ""
        text = (
            f"🔔 <b>Member decision</b>\n\n"
            f"👤 {pending.mention_html}{uname}\n🆔 <code>{pending.user_id}</code>\n"
            f"📍 <b>{escape(chat.display)}</b>\n\n{outcome}"
            + (f"\n👮 Decided by: {escape(actor_name)}" if actor_name else "")
        )
        targets = await self.db.prompt_messages(pending.id)
        if not targets:
            return
        # edit every admin's copy concurrently — the prompt vanishes for all at once
        await asyncio.gather(
            *(
                tg_try(
                    self.bot.edit_message_text,
                    text,
                    chat_id=admin_id,
                    message_id=message_id,
                    reply_markup=None,
                    disable_web_page_preview=True,
                    retries=1,
                    label="edit_prompt",
                )
                for admin_id, message_id in targets
            )
        )

    async def expire_stale_prompts(self) -> int:
        """Prompts unanswered for too long keep the default: close them quietly."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.ask_timeout_hours)
        count = 0
        for pending in await self.db.stale_pending(cutoff):
            if not await self.db.resolve_pending(pending.id, None, "timeout", status="expired"):
                continue
            count += 1
            chat = await self.db.get_chat(pending.chat_id)
            member = await self.db.get_member(pending.chat_id, pending.user_id)
            if chat and member:
                await self._finalize_prompt(
                    pending,
                    chat,
                    member,
                    f"⏱ No answer — default kept (until <b>{format_dt(member.expires_at, self.settings.tz)}</b>).",
                    "",
                )
        return count

    # -------------------------------------------------------------- leave/kick
    async def track_leave(self, chat: Chat, user: User, status: str = "left") -> None:
        member = await self.db.get_member(chat.chat_id, user.id)
        if not member:
            return
        await self.db.set_member_status(chat.chat_id, user.id, status)
        await self.db.add_log(chat.chat_id, user.id, status)
        pending = await self.db.get_pending_for(chat.chat_id, user.id)
        if pending:
            await self.db.resolve_pending(pending.id, None, "left", status="cancelled")
            await self._finalize_prompt(pending, chat, member, "⚪ Member left before a decision was made.", "")
        await self.send_log(
            chat,
            f"➖ <b>Left</b> {member.mention_html} [<code>{user.id}</code>]\n📍 {escape(chat.display)}",
        )

    async def remove_member(
        self, chat: Chat, member: Member, reason: str = "expired", actor_id: int | None = None
    ) -> tuple[bool, str]:
        """Kick or ban a member. Returns (success, message)."""
        try:
            await tg_call(self.bot.ban_chat_member, chat.chat_id, member.user_id, retries=2, label="ban")
            if chat.kick_mode == "kick":
                # unban so they can rejoin later (kick semantics)
                try:
                    await tg_call(
                        self.bot.unban_chat_member,
                        chat.chat_id,
                        member.user_id,
                        only_if_banned=True,
                        retries=2,
                        label="unban",
                    )
                except TelegramBadRequest as exc:
                    log.debug("unban after kick failed for %s: %s", member.user_id, exc)
        except TelegramRetryAfter as exc:
            return False, f"rate limited, retry in {exc.retry_after}s"
        except (TelegramNetworkError, TelegramUnavailable) as exc:
            return False, f"network error: {exc}"
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
            if "chat not found" in msg or "bot is not a member" in msg:
                return False, f"bot not in chat: {exc.message}"
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

    # ------------------------------------------------------------- extensions
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
        await self.db.set_member_expiry(chat.chat_id, member.user_id, new_expiry, count_renewal=True)
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
        new_expiry = self.expiry_from_text(expiry_text)
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

    async def notify_extension(self, chat: Chat, user_id: int, new_expiry: datetime | None) -> None:
        if not chat.notify_user:
            return
        if new_expiry is None:
            text = f"🎉 Your membership in <b>{escape(chat.display)}</b> is now <b>lifetime</b>."
        else:
            text = (
                f"🎉 Your membership in <b>{escape(chat.display)}</b> has been extended until "
                f"<b>{format_dt(new_expiry, self.settings.tz)}</b>."
            )
        await self.dm_user(user_id, text)

    # ----------------------------------------------------------- invite links
    async def create_invite_link(
        self, chat: Chat, duration: str, name: str | None, actor_id: int, join_request: bool = False
    ) -> tuple[str | None, str]:
        """Create a Telegram invite link bound to a membership duration."""
        if not is_permanent(duration):
            try:
                self.expiry_from_text(duration)
            except ParseError as exc:
                return None, f"Invalid duration: {exc}"
        label = (name or f"{describe_duration(duration)}")[:32]
        try:
            link = await tg_call(
                self.bot.create_chat_invite_link,
                chat.chat_id,
                name=label,
                creates_join_request=join_request,
                retries=2,
                label="create_invite",
            )
        except TelegramRetryAfter as exc:
            return None, f"Rate limited, retry in {exc.retry_after}s"
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            return None, f"Telegram refused: {exc.message} (need 'Invite users via link' admin right)"
        except (TelegramNetworkError, TelegramUnavailable) as exc:
            return None, f"Network error: {exc}"
        await self.db.add_invite_link(link.invite_link, chat.chat_id, label, duration, actor_id)
        await self.db.add_log(chat.chat_id, None, "invite_created", f"{label}={duration}", actor_id)
        return link.invite_link, label

    async def revoke_invite_link(self, chat: Chat, invite_link: str, actor_id: int) -> tuple[bool, str]:
        try:
            await self.bot.revoke_chat_invite_link(chat.chat_id, invite_link)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # still mark as revoked locally so it stops applying presets
            await self.db.revoke_invite_link(invite_link)
            return False, exc.message
        await self.db.revoke_invite_link(invite_link)
        await self.db.add_log(chat.chat_id, None, "invite_revoked", invite_link[-12:], actor_id)
        return True, "ok"

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
            "manual": "🚫",
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
        if member.source and member.source != "join":
            lines.append(f"🔗 Source: {escape(member.source)}")
        if member.renewals:
            lines.append(f"🔁 Renewals: {member.renewals}")
        if member.note:
            lines.append(f"📝 Note: {escape(member.note)}")
        return "\n".join(lines)

    async def resolve_user(self, chat_id: int, token: str) -> tuple[int | None, str | None]:
        """Resolve a user ID or @username to a user ID using the DB, then Telegram."""
        token = token.strip()
        if token.lstrip("-").isdigit():
            return int(token), None
        uname = token.lstrip("@")
        if not uname:
            return None, "Empty username."
        found = await self.db.find_member_by_username(chat_id, uname)
        if found:
            return found.user_id, None
        # fallback: try public resolution (works only for users with public usernames)
        chat = await tg_try(self.bot.get_chat, f"@{uname}", retries=1, label="resolve_username")
        if chat is not None and chat.type == "private":
            return chat.id, None
        return None, "User not found. Use a numeric user ID or reply to the user's message."

    async def sync_chat_members(self, chat: Chat) -> dict[str, int]:
        """Cross-check tracked members with Telegram (cheap consistency pass)."""
        result = {"checked": 0, "gone": 0, "errors": 0}
        members = await self.db.list_members(chat.chat_id, "active", limit=5000)
        sem = asyncio.Semaphore(6)

        async def check(member: Member) -> None:
            async with sem:
                result["checked"] += 1
                try:
                    cm = await tg_call(
                        self.bot.get_chat_member, chat.chat_id, member.user_id, retries=2, label="get_member"
                    )
                except TelegramBadRequest:
                    result["gone"] += 1
                    await self.db.set_member_status(chat.chat_id, member.user_id, "left")
                    return
                except (TelegramForbiddenError, TelegramRetryAfter, TelegramUnavailable):
                    result["errors"] += 1
                    return
                if cm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                    result["gone"] += 1
                    await self.db.set_member_status(
                        chat.chat_id,
                        member.user_id,
                        "kicked" if cm.status == ChatMemberStatus.KICKED else "left",
                    )
                elif cm.user.full_name != member.full_name or cm.user.username != member.username:
                    await self.db.update_member_profile(
                        chat.chat_id, member.user_id, cm.user.full_name, cm.user.username
                    )

        await asyncio.gather(*(check(m) for m in members))
        await self.db.add_log(chat.chat_id, None, "sync", str(result))
        return result

    async def broadcast(
        self, chat: Chat, text: str, actor_id: int, progress=None
    ) -> tuple[int, int]:
        """DM every active member concurrently (bounded). Returns (sent, total)."""
        members = await self.db.list_members(chat.chat_id, "active", limit=10000)
        total = len(members)
        sent = 0
        done = 0
        body = f"📣 <b>{escape(chat.display)}</b>\n\n{text}"

        async def one(m: Member) -> None:
            nonlocal sent, done
            if await self.safe_send(m.user_id, body):
                sent += 1
            done += 1
            if progress and done % 50 == 0:
                await progress(done, total)

        await asyncio.gather(*(one(m) for m in members))
        await self.db.add_log(chat.chat_id, None, "broadcast", f"{sent}/{total}", actor_id)
        return sent, total
