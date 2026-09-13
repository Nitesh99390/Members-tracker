"""Lightweight in-process metrics (counters, gauges, latency summaries).

No Prometheus client dependency: the registry is a few dicts, and
:meth:`Metrics.render_prometheus` emits the text exposition format so the
optional HTTP side-car can expose ``/metrics`` for scraping.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

# keep the last N observations per series for mean / max / p95
_WINDOW = 512


class Metrics:
    def __init__(self, namespace: str = "member_tracker") -> None:
        self.namespace = namespace
        self.started_at = time.time()
        self.counters: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = {}
        self._latency: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_WINDOW))
        self._latency_totals: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
        self.last_update_ts: float | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------- writers
    def inc(self, name: str, value: float = 1) -> None:
        self.counters[name] += value

    def set(self, name: str, value: float) -> None:
        self.gauges[name] = float(value)

    def observe(self, name: str, seconds: float) -> None:
        self._latency[name].append(seconds)
        count, total = self._latency_totals[name]
        self._latency_totals[name] = (count + 1, total + seconds)

    # ------------------------------------------------------------- readers
    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def latency_summary(self, name: str) -> dict[str, float]:
        window = self._latency.get(name)
        count, total = self._latency_totals.get(name, (0, 0.0))
        if not window:
            return {"count": count, "mean_ms": 0.0, "max_ms": 0.0, "p95_ms": 0.0}
        ordered = sorted(window)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return {
            "count": count,
            "mean_ms": round(sum(window) / len(window) * 1000, 1),
            "max_ms": round(ordered[-1] * 1000, 1),
            "p95_ms": round(ordered[idx] * 1000, 1),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "counters": {k: (int(v) if float(v).is_integer() else v) for k, v in sorted(self.counters.items())},
            "gauges": dict(sorted(self.gauges.items())),
            "latency": {name: self.latency_summary(name) for name in sorted(self._latency)},
            "last_update_ts": self.last_update_ts,
            "last_error": self.last_error,
        }

    def render_prometheus(self) -> str:
        """Prometheus text exposition (version 0.0.4)."""
        ns = self.namespace
        lines: list[str] = [
            f"# TYPE {ns}_uptime_seconds gauge",
            f"{ns}_uptime_seconds {self.uptime_seconds:.0f}",
        ]
        for name, value in sorted(self.counters.items()):
            metric = f"{ns}_{_sanitize(name)}"
            lines += [f"# TYPE {metric} counter", f"{metric} {_fmt(value)}"]
        for name, value in sorted(self.gauges.items()):
            metric = f"{ns}_{_sanitize(name)}"
            lines += [f"# TYPE {metric} gauge", f"{metric} {_fmt(value)}"]
        for name in sorted(self._latency):
            metric = f"{ns}_{_sanitize(name)}_seconds"
            count, total = self._latency_totals[name]
            summary = self.latency_summary(name)
            lines += [
                f"# TYPE {metric} summary",
                f'{metric}{{quantile="0.95"}} {summary["p95_ms"] / 1000:.6f}',
                f"{metric}_sum {total:.6f}",
                f"{metric}_count {count}",
            ]
        if self.last_update_ts:
            lines += [
                f"# TYPE {ns}_last_update_timestamp_seconds gauge",
                f"{ns}_last_update_timestamp_seconds {self.last_update_ts:.0f}",
            ]
        return "\n".join(lines) + "\n"


def _sanitize(name: str) -> str:
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6f}"
