"""Dispatcher middlewares: dependency injection, throttling and error reporting."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

log = logging.getLogger(__name__)


class DependenciesMiddleware(BaseMiddleware):
    """Inject shared services into every handler call."""

    def __init__(self, **deps: Any) -> None:
        self.deps = deps

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.update(self.deps)
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Simple per-user token bucket for commands and button presses.

    A user may perform ``burst`` actions freely; after that, actions closer
    together than ``rate`` seconds are dropped with a short notice.
    Service messages (joins/leaves) are never throttled.
    """

    def __init__(self, rate: float = 0.5, burst: int = 5) -> None:
        self.rate = rate
        self.burst = burst
        self._hits: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=burst))
        self._warned: dict[int, float] = {}

    def _allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        hits = self._hits[user_id]
        # drop hits outside the window
        window = self.rate * self.burst
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= self.burst:
            return False
        hits.append(now)
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
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

    # periodically forget idle users to keep memory bounded
    def cleanup(self) -> None:
        now = time.monotonic()
        window = self.rate * self.burst
        for uid in [u for u, h in self._hits.items() if not h or now - h[-1] > window * 4]:
            self._hits.pop(uid, None)
            self._warned.pop(uid, None)


class ErrorReportingMiddleware(BaseMiddleware):
    """Log unhandled exceptions with update context and optionally DM super-admins."""

    def __init__(self, notify_ids: list[int], bot_getter: Callable[[], Any]) -> None:
        self.notify_ids = notify_ids
        self.bot_getter = bot_getter
        self._last_sent: dict[str, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001
            update_id = getattr(event, "update_id", None) if isinstance(event, Update) else None
            log.exception("Unhandled error in update %s: %s", update_id, exc)
            key = f"{type(exc).__name__}:{str(exc)[:80]}"
            now = time.monotonic()
            if self.notify_ids and now - self._last_sent.get(key, 0) > 300:
                self._last_sent[key] = now
                bot = self.bot_getter()
                text = f"🐞 <b>Bot error</b>\n<code>{type(exc).__name__}: {str(exc)[:600]}</code>"
                for uid in self.notify_ids:
                    try:
                        await bot.send_message(uid, text)
                    except Exception:  # noqa: BLE001
                        pass
            return None
