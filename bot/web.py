"""Optional HTTP side-car: health probes, metrics and the webhook receiver.

Enabled with ``HTTP_PORT`` (and automatically when ``WEBHOOK_URL`` is set).

Endpoints
---------
``GET /healthz``  – liveness: process is up and the DB answers ``SELECT 1``
``GET /readyz``   – readiness: bot authenticated + polling/webhook active
``GET /metrics``  – Prometheus text format (``?format=json`` for JSON)
``POST <WEBHOOK_PATH>`` – Telegram webhook receiver (only in webhook mode)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from bot.config import Settings
from bot.services.database import Database
from bot.services.metrics import Metrics
from bot.services.scheduler import ExpiryScheduler

log = logging.getLogger(__name__)

# readiness fails if the scheduler hasn't ticked for this many intervals
STALE_TICKS = 5


@dataclass
class AppState:
    ready: bool = False
    started_at: float = field(default_factory=time.time)


def build_app(
    settings: Settings,
    db: Database,
    metrics: Metrics,
    scheduler: ExpiryScheduler,
    state: AppState,
    dp: Any | None = None,
    bot: Any | None = None,
) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)

    async def healthz(_: web.Request) -> web.Response:
        ok = await db.healthcheck()
        payload = {"status": "ok" if ok else "degraded", "database": ok, "uptime": round(time.time() - state.started_at)}
        return web.json_response(payload, status=200 if ok else 503)

    async def readyz(_: web.Request) -> web.Response:
        problems: list[str] = []
        if not state.ready:
            problems.append("bot not started")
        if not scheduler.running:
            problems.append("scheduler stopped")
        if scheduler.last_run is not None:
            age = time.time() - scheduler.last_run.timestamp()
            if age > settings.check_interval * STALE_TICKS:
                problems.append(f"scheduler stale ({age:.0f}s since last tick)")
        if not await db.healthcheck():
            problems.append("database unavailable")
        payload = {
            "status": "ready" if not problems else "not_ready",
            "problems": problems,
            "mode": "webhook" if settings.webhook_url else "polling",
        }
        return web.json_response(payload, status=200 if not problems else 503)

    async def metrics_view(request: web.Request) -> web.Response:
        _refresh_gauges(db, metrics, scheduler)
        if request.query.get("format") == "json":
            return web.json_response(metrics.snapshot())
        return web.Response(text=metrics.render_prometheus(), content_type="text/plain", charset="utf-8")

    async def index(_: web.Request) -> web.Response:
        return web.json_response(
            {"service": "telegram-member-tracker", "endpoints": ["/healthz", "/readyz", "/metrics"]}
        )

    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics_view)

    if settings.webhook_url and dp is not None and bot is not None:
        # imported lazily so the side-car works even if aiogram's webhook extras change
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=settings.webhook_secret or None,
            handle_in_background=True,
        ).register(app, path=settings.webhook_path)
        # wires dispatcher startup/shutdown into the aiohttp lifecycle
        setup_application(app, dp, bot=bot)
        log.info("Webhook receiver mounted at %s", settings.webhook_path)

    return app


def _refresh_gauges(db: Database, metrics: Metrics, scheduler: ExpiryScheduler) -> None:
    cache = db.cache_stats()
    for name in ("chats", "admins", "whitelist", "context"):
        metrics.set(f"cache_{name}_size", cache[name]["size"])
        metrics.set(f"cache_{name}_hit_ratio", cache[name]["hit_ratio"])
    metrics.set("db_queries_total", cache["queries"])
    metrics.set("db_writes_total", cache["writes"])
    metrics.set("db_size_bytes", db.db_size_bytes())
    metrics.set("scheduler_running", 1 if scheduler.running else 0)
    metrics.set("scheduler_last_tick_seconds", scheduler.last_run_duration)


async def start_http(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port, reuse_address=True)
    await site.start()
    log.info("HTTP side-car listening on http://%s:%s", host, port)
    return runner
