"""Tiny in-process TTL cache used for hot read paths (chat settings, admin checks…).

Design goals:
* zero dependencies, O(1) get/set, bounded memory (LRU eviction at ``maxsize``)
* per-entry TTL override (e.g. short negative caching)
* cheap ``invalidate_where`` for "forget everything about chat X"
* hit/miss counters for the ``/health`` command and ``/metrics`` endpoint
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar
from collections.abc import Callable, Hashable

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_MISSING: Any = object()


class TTLCache(Generic[K, V]):
    def __init__(self, ttl: float = 60.0, maxsize: int = 1024, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = max(0.0, float(ttl))
        self.maxsize = max(1, int(maxsize))
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    # ----------------------------------------------------------------- core
    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: K) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def get(self, key: K, default: Any = None) -> Any:
        """Return the cached value or ``default``; expired entries count as misses."""
        if self.ttl == 0:
            self.misses += 1
            return default
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return default
        expires_at, value = item
        if expires_at <= self._clock():
            self._data.pop(key, None)
            self.misses += 1
            return default
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def peek(self, key: K, default: Any = None) -> Any:
        """Like :meth:`get` but does not touch hit/miss counters or LRU order."""
        item = self._data.get(key)
        if item is None or item[0] <= self._clock():
            return default
        return item[1]

    def set(self, key: K, value: V, ttl: float | None = None) -> V:
        """Store ``value`` and return it (handy for ``return cache.set(k, compute())``)."""
        effective = self.ttl if ttl is None else max(0.0, float(ttl))
        if effective == 0:
            self._data.pop(key, None)
            return value
        self._data[key] = (self._clock() + effective, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
            self.evictions += 1
        return value

    def pop(self, key: K, default: Any = None) -> Any:
        item = self._data.pop(key, None)
        return default if item is None else item[1]

    def clear(self) -> None:
        self._data.clear()

    # ----------------------------------------------------------- maintenance
    def invalidate_where(self, predicate: Callable[[K], bool]) -> int:
        """Drop every key for which ``predicate(key)`` is true. Returns the count."""
        doomed = [k for k in self._data if predicate(k)]
        for k in doomed:
            self._data.pop(k, None)
        return len(doomed)

    def purge_expired(self) -> int:
        now = self._clock()
        doomed = [k for k, (exp, _) in self._data.items() if exp <= now]
        for k in doomed:
            self._data.pop(k, None)
        return len(doomed)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "maxsize": self.maxsize,
            "ttl": self.ttl,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_ratio": (self.hits / total) if total else 0.0,
        }
