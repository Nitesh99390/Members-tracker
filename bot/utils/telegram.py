"""Resilient wrappers around Bot API calls.

* :func:`tg_call` – call a Bot method with bounded retries for *transient*
  failures (flood limits, network blips, 5xx from Telegram).  Permanent
  errors (``TelegramBadRequest``, ``TelegramForbiddenError``…) are raised
  immediately so callers can react to them.  When retries are exhausted a
  :class:`TelegramUnavailable` is raised (or the original ``TelegramRetryAfter``
  when the flood wait is too long to be worth sleeping through).
* :func:`tg_try`  – same, but never raises: returns ``None`` on any Telegram error.
* :func:`is_not_modified` / :func:`error_text` – small helpers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TypeVar
from collections.abc import Awaitable, Callable

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# longest flood wait we are willing to sleep through inside a single call
MAX_FLOOD_WAIT = 30
# base delay for network / server-error retries (doubles each attempt)
BASE_BACKOFF = 1.0
MAX_BACKOFF = 15.0


class TelegramUnavailable(Exception):
    """Telegram could not be reached after all retries (network or 5xx)."""

    def __init__(self, label: str, attempts: int, last: BaseException) -> None:
        self.label = label
        self.attempts = attempts
        self.last = last
        super().__init__(f"{label}: Telegram unavailable after {attempts} attempt(s): {error_text(last)}")


def error_text(exc: BaseException) -> str:
    """Human-readable message for any aiogram/network exception."""
    msg = getattr(exc, "message", None)
    if isinstance(msg, str) and msg:
        return msg
    text = str(exc).strip()
    return text or type(exc).__name__


def is_not_modified(exc: BaseException) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in error_text(exc).lower()


async def tg_call(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    retries: int = 2,
    label: str | None = None,
    max_flood_wait: int = MAX_FLOOD_WAIT,
    **kwargs: Any,
) -> T:
    """Invoke ``fn(*args, **kwargs)`` with retries for transient Telegram failures.

    ``retries`` is the number of *additional* attempts after the first one.
    """
    name = label or getattr(fn, "__name__", "telegram_call")
    attempts = retries + 1
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as exc:
            last = exc
            wait = int(exc.retry_after)
            if wait > max_flood_wait or attempt == attempts:
                log.warning("%s: flood limit %ss (attempt %s/%s) — giving up", name, wait, attempt, attempts)
                raise
            log.warning("%s: flood limit, sleeping %ss (attempt %s/%s)", name, wait, attempt, attempts)
            await asyncio.sleep(wait + 0.5)
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as exc:
            last = exc
            if attempt == attempts:
                break
            delay = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
            log.warning(
                "%s: %s (attempt %s/%s), retrying in %.1fs", name, error_text(exc), attempt, attempts, delay
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise TelegramUnavailable(name, attempts, last)


async def tg_try(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    retries: int = 1,
    label: str | None = None,
    **kwargs: Any,
) -> T | None:
    """Like :func:`tg_call` but swallows every Telegram error and returns ``None``."""
    name = label or getattr(fn, "__name__", "telegram_call")
    try:
        return await tg_call(fn, *args, retries=retries, label=name, **kwargs)
    except TelegramBadRequest as exc:
        if not is_not_modified(exc):
            log.debug("%s: bad request: %s", name, error_text(exc))
    except (TelegramAPIError, TelegramUnavailable, asyncio.TimeoutError) as exc:
        log.debug("%s: %s", name, error_text(exc))
    return None
