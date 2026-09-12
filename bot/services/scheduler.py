"""Background jobs: expiry enforcement, reminders, prompt timeouts, maintenance."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import Settings
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.utils.permissions import refresh_chat_admins
from bot.utils.timeparse import format_dt, humanize_delta

log = logging.getLogger(__name__)

# after this many consecutive failures we stop retrying every hour and wait a day
MAX_FAILS_BEFORE_BACKOFF = 5


class ExpiryScheduler:
    def __init__(self, service: MembershipService, db: Database, settings: Settings) -> None:
        self.service = service
        self.db = db
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._lock = asyncio.Lock()

    def start(self) -> None:
        now = datetime.now(timezone.utc)
        self.scheduler.add_job(
            self.run_once,
            IntervalTrigger(seconds=self.settings.check_interval),
            id="expiry_check",
            max_instances=1,
            coalesce=True,
            next_run_time=now + timedelta(seconds=5),
        )
        self.scheduler.add_job(
            self.refresh_admins,
            IntervalTrigger(hours=6),
            id="refresh_admins",
            max_instances=1,
            coalesce=True,
            next_run_time=now + timedelta(seconds=20),
        )
        self.scheduler.add_job(
            self.maintenance,
            CronTrigger(hour=self.settings.backup_hour_utc, minute=15),
            id="maintenance",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        log.info("Scheduler started (interval=%ss)", self.settings.check_interval)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # --------------------------------------------------------------- jobs
    async def run_once(self) -> dict[str, int]:
        """Process expired members, reminders and stale prompts. Returns counters."""
        if self._lock.locked():
            return {"skipped": 1}
        async with self._lock:
            removed = await self._guard(self._process_expired, 0)
            reminded = await self._guard(self._process_reminders, 0)
            prompts_expired = await self._guard(self.service.expire_stale_prompts, 0)
            await self.db.kv_set("last_check", datetime.now(timezone.utc).isoformat())
        if removed or reminded or prompts_expired:
            log.info(
                "Expiry run: removed=%s reminded=%s prompts_expired=%s", removed, reminded, prompts_expired
            )
        return {"removed": removed, "reminded": reminded, "prompts_expired": prompts_expired}

    async def _guard(self, coro_fn, default):
        """Run a job step and never let one failing step kill the whole tick."""
        try:
            return await coro_fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler step %s failed: %s", getattr(coro_fn, "__name__", coro_fn), exc)
            return default

    async def _process_expired(self) -> int:
        removed = 0
        for member in await self.db.expired_members():
            chat = await self.db.get_chat(member.chat_id)
            if not chat or not chat.tracking_enabled:
                continue
            if await self.db.is_whitelisted(chat.chat_id, member.user_id) or await self.db.is_chat_admin(
                chat.chat_id, member.user_id
            ):
                await self.db.set_member_expiry(chat.chat_id, member.user_id, None)
                continue
            if not chat.auto_kick:
                # only mark as expired, do not remove
                await self.db.set_member_status(chat.chat_id, member.user_id, "expired")
                await self.service.send_log(
                    chat,
                    f"⌛ <b>Expired (auto-remove off)</b> {member.mention_html} "
                    f"[<code>{member.user_id}</code>]\n📍 {escape(chat.display)}",
                )
                continue
            ok, msg = await self.service.remove_member(chat, member, reason="expired")
            if ok:
                removed += 1
            else:
                log.warning("Failed to remove %s from %s: %s", member.user_id, chat.chat_id, msg)
                await self.db.add_log(chat.chat_id, member.user_id, "remove_failed", msg)
                if "rate limited" in msg:
                    break  # try again next tick
                fails = await self.db.bump_fail_count(chat.chat_id, member.user_id)
                delay = timedelta(hours=1) if fails < MAX_FAILS_BEFORE_BACKOFF else timedelta(days=1)
                await self.db.set_member_expiry(
                    chat.chat_id, member.user_id, datetime.now(timezone.utc) + delay
                )
                # set_member_expiry resets fail_count; restore it
                await self.db._exec(
                    "UPDATE members SET fail_count=? WHERE chat_id=? AND user_id=?",
                    (fails, chat.chat_id, member.user_id),
                )
                if fails in (1, MAX_FAILS_BEFORE_BACKOFF):
                    await self.service.send_log(
                        chat,
                        f"⚠️ <b>Could not remove</b> {member.mention_html} "
                        f"[<code>{member.user_id}</code>]\n📍 {escape(chat.display)}\n"
                        f"❗ {escape(msg)}\nRetrying in {'1 hour' if fails < MAX_FAILS_BEFORE_BACKOFF else '24 hours'}.",
                    )
                    if self.settings.notify_admins_on_error and fails == MAX_FAILS_BEFORE_BACKOFF:
                        owner = await self.service.get_owner_id(chat)
                        if owner:
                            await self.service.dm_user(
                                owner,
                                f"⚠️ I keep failing to remove {member.mention_html} from "
                                f"<b>{escape(chat.display)}</b>: {escape(msg)}\n"
                                f"Please check my admin rights (/permissions).",
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
            await self.db.mark_reminder_sent(
                chat.chat_id, member.user_id, [h for h in self.settings.reminder_hours if h >= target]
            )
            if sent:
                reminded += 1
        return reminded

    async def refresh_admins(self) -> None:
        for chat in await self.db.list_chats(only_tracking=True):
            await refresh_chat_admins(self.service.bot, self.db, chat.chat_id)
            await self.service.get_owner_id(chat, refresh=True)
            await asyncio.sleep(0.2)

    # -------------------------------------------------------- maintenance
    async def maintenance(self) -> None:
        """Daily: prune old logs, VACUUM-free backup, send backup to admin chat."""
        pruned = await self._guard(lambda: self.db.prune_logs(keep_days=90), 0)
        if pruned:
            log.info("Pruned %s old log rows", pruned)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_dir = Path(self.settings.database_path).parent / "backups"
        dest = backup_dir / f"bot_{stamp}.db"
        try:
            await self.db.backup_to(str(dest))
        except Exception as exc:  # noqa: BLE001
            log.exception("Backup failed: %s", exc)
            return
        # keep only the last 7 local backups
        for old in sorted(backup_dir.glob("bot_*.db"))[:-7]:
            old.unlink(missing_ok=True)
        if self.settings.backup_chat_id:
            try:
                await self.service.bot.send_document(
                    self.settings.backup_chat_id,
                    FSInputFile(str(dest)),
                    caption=f"🗄 Daily backup {stamp} ({dest.stat().st_size // 1024} KB)",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot send backup to %s: %s", self.settings.backup_chat_id, exc)
