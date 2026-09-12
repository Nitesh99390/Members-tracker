"""Dispatcher middlewares: dependency injection + error logging."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

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
