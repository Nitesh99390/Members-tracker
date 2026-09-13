"""Background jobs: expiry enforcement, reminders, prompt timeouts, maintenance."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Awaitable, Callable

from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import Settings
from bot.services.database import Chat, Database, Member
from bot.services.membership import MembershipService
from bot.services.metrics import Metrics
from bot.utils.permissions import refresh_chat_admins
from bot.utils.timeparse import format_dt, humanize_delta

log = logging.getLogger(__name__)

# after this many consecutive failures we stop retrying every hour and wait a day
MAX_FAILS_BEFORE_BACKOFF = 5
# how many removals / reminder DMs run at the same time
REMOVE_CONCURRENCY = 4
REMIND_CONCURRENCY = 8


class ExpiryScheduler:
    def __init__(
        self,
        service: MembershipService,
        db: Database,
        settings: Settings,
        metrics: Metrics | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self.service = service
        self.db = db
        self.settings = settings
        self.metrics = metrics or Metrics()
        self.on_tick = on_tick
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._lock = asyncio.Lock()
        self.last_run: datetime | None = None
        self.last_run_duration: float = 0.0

    def start(self) -> None:
        now = datetime.now(timezone.utc)
        self.scheduler.add_job(
            self.run_once,
            IntervalTrigger(seconds=self.settings.check_interval),
            id="expiry_check",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self.settings.check_interval,
            next_run_time=now + timedelta(seconds=5),
        )
        self.scheduler.add_job(
            self.refresh_admins,
            IntervalTrigger(hours=6),
            id="refresh_admins",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
            next_run_time=now + timedelta(seconds=20),
        )
        self.scheduler.add_job(
            self.housekeeping,
            IntervalTrigger(minutes=10),
            id="housekeeping",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self.scheduler.add_job(
            self.maintenance,
            CronTrigger(hour=self.settings.backup_hour_utc, minute=15),
            id="maintenance",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        log.info("Scheduler started (interval=%ss)", self.settings.check_interval)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return self.scheduler.running

    # --------------------------------------------------------------- jobs
    async def run_once(self) -> dict[str, int]:
        """Process expired members, reminders and stale prompts. Returns counters."""
        if self._lock.locked():
            self.metrics.inc("scheduler_skipped")
            return {"skipped": 1}
        started = time.perf_counter()
        async with self._lock:
            removed, reminded, prompts_expired = await asyncio.gather(
                self._guard(self._process_expired, 0),
                self._guard(self._process_reminders, 0),
                self._guard(self.service.expire_stale_prompts, 0),
            )
            self.last_run = datetime.now(timezone.utc)
            self.last_run_duration = time.perf_counter() - started
            await self.db.kv_set("last_check", self.last_run.isoformat())
        self.metrics.inc("scheduler_ticks")
        self.metrics.inc("members_removed", removed)
        self.metrics.inc("reminders_sent", reminded)
        self.metrics.inc("prompts_expired", prompts_expired)
        self.metrics.observe("scheduler_tick", self.last_run_duration)
        if self.on_tick:
            self.on_tick()
        if removed or reminded or prompts_expired:
            log.info(
                "Expiry run (%.2fs): removed=%s reminded=%s prompts_expired=%s",
                self.last_run_duration, removed, reminded, prompts_expired,
            )
        return {"removed": removed, "reminded": reminded, "prompts_expired": prompts_expired}

    async def _guard(self, coro_fn: Callable[[], Awaitable[int]], default: int) -> int:
        """Run a job step and never let one failing step kill the whole tick."""
        try:
            return await coro_fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler step %s failed: %s", getattr(coro_fn, "__name__", coro_fn), exc)
            self.metrics.inc("scheduler_step_errors")
            return default

    # ---------------------------------------------------------- expiry
    async def _process_expired(self) -> int:
        expired = await self.db.expired_members()
        if not expired:
            return 0
        sem = asyncio.Semaphore(REMOVE_CONCURRENCY)
        stop = asyncio.Event()  # set when we hit a flood limit → stop this tick
        removed = 0

        async def handle(member: Member) -> None:
            nonlocal removed
            if stop.is_set():
                return
            async with sem:
                if stop.is_set():
                    return
                ok = await self._expire_one(member, stop)
                if ok:
                    removed += 1

        await asyncio.gather(*(handle(m) for m in expired))
        return removed

    async def _expire_one(self, member: Member, stop: asyncio.Event) -> bool:
        chat = await self.db.get_chat(member.chat_id)
        if not chat or not chat.tracking_enabled:
            return False
        if await self.db.is_whitelisted(chat.chat_id, member.user_id) or await self.db.is_chat_admin(
            chat.chat_id, member.user_id
        ):
            await self.db.set_member_expiry(chat.chat_id, member.user_id, None)
            return False
        if chat.grace_hours and member.expires_at:
            grace_until = member.expires_at + timedelta(hours=chat.grace_hours)
            if grace_until > datetime.now(timezone.utc):
                return False
        if not chat.auto_kick:
            await self.db.set_member_status(chat.chat_id, member.user_id, "expired")
            await self.service.send_log(
                chat,
                f"⌛ <b>Expired (auto-remove off)</b> {member.mention_html} "
                f"[<code>{member.user_id}</code>]\n📍 {escape(chat.display)}",
            )
            return False

        ok, msg = await self.service.remove_member(chat, member, reason="expired")
        if ok:
            return True

        log.warning("Failed to remove %s from %s: %s", member.user_id, chat.chat_id, msg)
        await self.db.add_log(chat.chat_id, member.user_id, "remove_failed", msg)
        self.metrics.inc("remove_failed")
        if "rate limited" in msg:
            stop.set()  # try again next tick
            return False
        await self._schedule_retry(chat, member, msg)
        return False

    async def _schedule_retry(self, chat: Chat, member: Member, msg: str) -> None:
        fails = await self.db.bump_fail_count(chat.chat_id, member.user_id)
        delay = timedelta(hours=1) if fails < MAX_FAILS_BEFORE_BACKOFF else timedelta(days=1)
        await self.db.set_member_expiry(chat.chat_id, member.user_id, datetime.now(timezone.utc) + delay)
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

    # ------------------------------------------------------- reminders
    async def _process_reminders(self) -> int:
        if not self.settings.reminder_hours:
            return 0
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=max(self.settings.reminder_hours))
        candidates = await self.db.expiring_members(horizon)
        if not candidates:
            return 0
        sem = asyncio.Semaphore(REMIND_CONCURRENCY)
        reminded = 0

        async def remind(member: Member) -> None:
            nonlocal reminded
            if member.expires_at is None:
                return
            chat = await self.db.get_chat(member.chat_id)
            if not chat or not chat.tracking_enabled or not chat.notify_user:
                return
            remaining = member.expires_at - now
            # the most specific (smallest) threshold we've already crossed
            crossed = [h for h in self.settings.reminder_hours if remaining <= timedelta(hours=h)]
            if not crossed:
                return
            target = min(crossed)
            if target in member.reminders_sent:
                return
            async with sem:
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

        await asyncio.gather(*(remind(m) for m in candidates))
        return reminded

    async def refresh_admins(self) -> None:
        for chat in await self.db.list_chats(only_tracking=True):
            await refresh_chat_admins(self.service.bot, self.db, chat.chat_id)
            await self.service.get_owner_id(chat, refresh=True)
            await asyncio.sleep(0.2)

    # ------------------------------------------------------ housekeeping
    async def housekeeping(self) -> None:
        """Cheap periodic cleanups: expired cache entries, throttle buckets, gauges."""
        purged = self.db.purge_caches()
        if self.on_tick:
            self.on_tick()
        try:
            stats = await self.db.global_stats()
            self.metrics.set("active_members", stats["active"])
            self.metrics.set("tracked_chats", stats["tracking"])
            self.metrics.set("pending_decisions", stats["pending"])
            self.metrics.set("db_size_bytes", self.db.db_size_bytes())
        except Exception as exc:  # noqa: BLE001
            log.debug("housekeeping stats failed: %s", exc)
        if purged:
            log.debug("Purged %s expired cache entries", purged)

    # -------------------------------------------------------- maintenance
    async def maintenance(self) -> None:
        """Daily: prune old logs, optimize DB, backup, send backup to admin chat."""
        pruned = await self._guard(lambda: self.db.prune_logs(keep_days=90), 0)
        if pruned:
            log.info("Pruned %s old log rows", pruned)
        await self.db.optimize()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_dir = Path(self.settings.database_path).parent / "backups"
        dest = backup_dir / f"bot_{stamp}.db"
        try:
            await self.db.backup_to(str(dest))
        except Exception as exc:  # noqa: BLE001
            log.exception("Backup failed: %s", exc)
            self.metrics.inc("backup_failed")
            return
        self.metrics.inc("backups_total")
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
