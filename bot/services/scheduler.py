"""Background jobs: expiry enforcement, reminders and admin cache refresh."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import Settings
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.utils.permissions import refresh_chat_admins
from bot.utils.timeparse import format_dt, humanize_delta

log = logging.getLogger(__name__)


class ExpiryScheduler:
    def __init__(self, service: MembershipService, db: Database, settings: Settings) -> None:
        self.service = service
        self.db = db
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self.scheduler.add_job(
            self.run_once,
            IntervalTrigger(seconds=self.settings.check_interval),
            id="expiry_check",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        self.scheduler.add_job(
            self.refresh_admins,
            IntervalTrigger(hours=6),
            id="refresh_admins",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
        )
        self.scheduler.start()
        log.info("Scheduler started (interval=%ss)", self.settings.check_interval)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # --------------------------------------------------------------- jobs
    async def run_once(self) -> dict[str, int]:
        """Process expired members + reminders. Returns counters (for /forcecheck)."""
        if self._lock.locked():
            return {"skipped": 1}
        async with self._lock:
            removed = await self._process_expired()
            reminded = await self._process_reminders()
        if removed or reminded:
            log.info("Expiry run: removed=%s reminded=%s", removed, reminded)
        return {"removed": removed, "reminded": reminded}

    async def _process_expired(self) -> int:
        removed = 0
        for member in await self.db.expired_members():
            chat = await self.db.get_chat(member.chat_id)
            if not chat or not chat.tracking_enabled:
                continue
            if await self.db.is_whitelisted(chat.chat_id, member.user_id):
                await self.db.set_member_expiry(chat.chat_id, member.user_id, None)
                continue
            if not chat.auto_kick:
                # only mark as expired, do not remove
                await self.db.set_member_status(chat.chat_id, member.user_id, "expired")
                await self.service.send_log(
                    chat,
                    f"⌛ <b>Expired (auto-kick off)</b> {member.mention_html} "
                    f"[<code>{member.user_id}</code>]\n📍 {escape(chat.display)}",
                )
                continue
            ok, msg = await self.service.remove_member(chat, member, reason="expired")
            if ok:
                removed += 1
            else:
                log.warning("Failed to remove %s from %s: %s", member.user_id, chat.chat_id, msg)
                await self.db.add_log(chat.chat_id, member.user_id, "remove_failed", msg)
                if "rate limited" not in msg:
                    # avoid retry storms for permanent failures; push expiry 1h ahead
                    await self.db.set_member_expiry(
                        chat.chat_id,
                        member.user_id,
                        datetime.now(timezone.utc) + timedelta(hours=1),
                    )
                    await self.service.send_log(
                        chat,
                        f"⚠️ <b>Could not remove</b> {member.mention_html} "
                        f"[<code>{member.user_id}</code>]\n📍 {escape(chat.display)}\n"
                        f"❗ {escape(msg)}\nRetrying in 1 hour.",
                    )
            await asyncio.sleep(0.15)  # be gentle with rate limits
        return removed

    async def _process_reminders(self) -> int:
        if not self.settings.reminder_hours:
            return 0
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=max(self.settings.reminder_hours))
        reminded = 0
        for member in await self.db.expiring_members(horizon):
            if member.expires_at is None:
                continue
            chat = await self.db.get_chat(member.chat_id)
            if not chat or not chat.tracking_enabled or not chat.notify_user:
                continue
            remaining = member.expires_at - now
            # the most specific (smallest) threshold we've already crossed
            crossed = [h for h in self.settings.reminder_hours if remaining <= timedelta(hours=h)]
            if not crossed:
                continue
            target = min(crossed)
            if target in member.reminders_sent:
                continue
            sent = await self.service.dm_user(
                member.user_id,
                f"⏰ <b>Membership reminder</b>\n\n"
                f"Your access to <b>{escape(chat.display)}</b> expires in "
                f"<b>{humanize_delta(remaining)}</b> "
                f"({format_dt(member.expires_at, self.settings.tz)}).\n"
                f"Contact an admin to renew.",
            )
            # mark this and all larger thresholds as sent so each stage fires once
            for h in self.settings.reminder_hours:
                if h >= target:
                    await self.db.mark_reminder_sent(chat.chat_id, member.user_id, h)
            if sent:
                reminded += 1
        return reminded

    async def refresh_admins(self) -> None:
        for chat in await self.db.list_chats():
            await refresh_chat_admins(self.service.bot, self.db, chat.chat_id)
            await asyncio.sleep(0.2)
