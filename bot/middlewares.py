"""Dispatcher middlewares.

Outer (update level):
    * :class:`ErrorReportingMiddleware` – log + DM super-admins on unhandled errors
    * :class:`MetricsMiddleware`        – counters / latency per update type
    * :class:`DependenciesMiddleware`   – inject shared services

Inner (message / callback level):
    * :class:`ThrottlingMiddleware`     – per-user token bucket for commands & buttons
    * :class:`CallbackAckMiddleware`    – answer callbacks instantly, drop double taps
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from bot.services.metrics import Metrics

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DependenciesMiddleware(BaseMiddleware):
    """Inject shared services into every handler call."""

    def __init__(self, **deps: Any) -> None:
        self.deps = deps

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        data.update(self.deps)
        return await handler(event, data)


class MetricsMiddleware(BaseMiddleware):
    """Count updates, measure handler latency, remember the last update time."""

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        kind = _update_kind(event)
        started = time.perf_counter()
        self.metrics.inc("updates_total")
        self.metrics.inc(f"updates_{kind}")
        try:
            return await handler(event, data)
        finally:
            elapsed = time.perf_counter() - started
            self.metrics.observe("handler_latency", elapsed)
            self.metrics.last_update_ts = time.time()
            if elapsed > 2.0:
                log.warning("Slow handler for %s: %.2fs", kind, elapsed)


def _update_kind(event: TelegramObject) -> str:
    if isinstance(event, Update):
        return event.event_type
    return type(event).__name__.lower()


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user token bucket for commands and button presses.

    A user may perform ``burst`` actions freely; after that, actions closer
    together than ``rate`` seconds are dropped with a short notice.
    Service messages (joins/leaves) are never throttled.
    """

    def __init__(self, rate: float = 0.5, burst: int = 5, metrics: Metrics | None = None) -> None:
        self.rate = rate
        self.burst = burst
        self.metrics = metrics
        self._hits: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=burst))
        self._warned: dict[int, float] = {}

    def _allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        hits = self._hits[user_id]
        window = self.rate * self.burst
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= self.burst:
            return False
        hits.append(now)
        return True

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = None
        is_command = False
        if isinstance(event, Message):
            user = event.from_user
            is_command = bool(event.text and event.text.startswith("/"))
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            is_command = True
        if user is None or not is_command or user.is_bot:
            return await handler(event, data)
        if self._allowed(user.id):
            return await handler(event, data)
        if self.metrics:
            self.metrics.inc("throttled_total")
        now = time.monotonic()
        if now - self._warned.get(user.id, 0) > 5:
            self._warned[user.id] = now
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Slow down a little…", show_alert=False)
                else:
                    await event.reply("⏳ Too many commands, please wait a moment.")
            except Exception:  # noqa: BLE001
                pass
        return None

    def cleanup(self) -> int:
        """Forget idle users to keep memory bounded. Returns how many were dropped."""
        now = time.monotonic()
        window = self.rate * self.burst
        idle = [u for u, h in self._hits.items() if not h or now - h[-1] > window * 4]
        for uid in idle:
            self._hits.pop(uid, None)
            self._warned.pop(uid, None)
        return len(idle)


class CallbackAckMiddleware(BaseMiddleware):
    """Make buttons feel instant and idempotent.

    * The same (user, message, data) tapped while the previous press is still
      being processed is ignored – no duplicate extensions or removals from
      impatient double taps.
    * If a handler finishes without calling ``call.answer()`` (or raises), we
      answer the callback ourselves so Telegram's loading spinner never hangs.
    """

    def __init__(self, metrics: Metrics | None = None) -> None:
        self.metrics = metrics
        self._inflight: set[tuple[int, int, str]] = set()

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        key = (
            event.from_user.id,
            event.message.message_id if event.message else 0,
            event.data or "",
        )
        if key in self._inflight:
            if self.metrics:
                self.metrics.inc("callback_dedup_total")
            try:
                await event.answer()
            except TelegramBadRequest:
                pass
            return None
        self._inflight.add(key)
        answered = False
        original_answer = event.answer

        async def _answer(*args: Any, **kwargs: Any) -> Any:
            nonlocal answered
            answered = True
            try:
                return await original_answer(*args, **kwargs)
            except TelegramBadRequest as exc:
                # "query is too old" – the user waited > 10s or tapped an ancient button
                log.debug("callback answer failed: %s", exc)
                return None

        # aiogram models are pydantic; bypass validation for the shim
        object.__setattr__(event, "answer", _answer)
        try:
            return await handler(event, data)
        finally:
            self._inflight.discard(key)
            if not answered:
                try:
                    await asyncio.wait_for(original_answer(), timeout=3)
                except Exception:  # noqa: BLE001
                    pass


class ErrorReportingMiddleware(BaseMiddleware):
    """Log unhandled exceptions with update context and optionally DM super-admins."""

    def __init__(
        self, notify_ids: list[int], bot_getter: Callable[[], Any], metrics: Metrics | None = None
    ) -> None:
        self.notify_ids = notify_ids
        self.bot_getter = bot_getter
        self.metrics = metrics
        self._last_sent: dict[str, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001
            update_id = getattr(event, "update_id", None) if isinstance(event, Update) else None
            log.exception("Unhandled error in update %s: %s", update_id, exc)
            if self.metrics:
                self.metrics.inc("errors_total")
                self.metrics.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            key = f"{type(exc).__name__}:{str(exc)[:80]}"
            now = time.monotonic()
            if self.notify_ids and now - self._last_sent.get(key, 0) > 300:
                self._last_sent[key] = now
                bot = self.bot_getter()
                text = (
                    f"🐞 <b>Bot error</b> (update {update_id})\n"
                    f"<code>{type(exc).__name__}: {str(exc)[:600]}</code>"
                )
                await asyncio.gather(
                    *(_quiet_send(bot, uid, text) for uid in self.notify_ids), return_exceptions=True
                )
            return None


async def _quiet_send(bot: Any, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception:  # noqa: BLE001
        pass
