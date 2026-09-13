"""Entry point: Telegram Member Tracker Bot.

Runtime features
----------------
* long polling (default) **or** webhook mode (``WEBHOOK_URL``)
* optional HTTP side-car with ``/healthz``, ``/readyz``, ``/metrics`` (``HTTP_PORT``)
* structured JSON logs (``LOG_JSON=true``) or human-readable rotating logs
* graceful shutdown on SIGTERM/SIGINT: stop accepting updates → finish
  in-flight handlers → stop scheduler → close DB
* startup retries when Telegram is temporarily unreachable
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from bot import VERSION_TAG, __version__
from bot.config import Settings
from bot.handlers import admin, callbacks, common, menu, tracking
from bot.middlewares import (
    CallbackAckMiddleware,
    DependenciesMiddleware,
    ErrorReportingMiddleware,
    MetricsMiddleware,
    ThrottlingMiddleware,
)
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.services.metrics import Metrics
from bot.services.scheduler import ExpiryScheduler

log = logging.getLogger("main")

ALLOWED_UPDATES = ["message", "callback_query", "chat_member", "my_chat_member", "chat_join_request"]

# Keep the "/" menu short: everything else is reachable through buttons.
PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Home"),
    BotCommand(command="chats", description="Manage a group or channel"),
    BotCommand(command="pending", description="Joins waiting for your decision"),
    BotCommand(command="help", description="Help"),
]

GROUP_COMMANDS = [
    BotCommand(command="info", description="Member details (reply)"),
    BotCommand(command="extend", description="Extend membership (reply)"),
    BotCommand(command="remove", description="Remove member (reply)"),
    BotCommand(command="mystatus", description="Your membership"),
]

SUPER_ADMIN_EXTRA = [
    BotCommand(command="gstats", description="Global statistics"),
    BotCommand(command="health", description="Bot health check"),
]


# ------------------------------------------------------------------ logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(settings: Settings) -> None:
    fmt: logging.Formatter
    if settings.log_json:
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(settings.log_dir) / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - read-only FS
        log.warning("File logging disabled: %s", exc)

    for noisy in ("apscheduler", "aiogram.event", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ startup
async def wait_for_telegram(bot: Bot, attempts: int = 8):
    """``getMe`` with backoff so a flaky network at boot doesn't crash-loop the container."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            raise RuntimeError("BOT_TOKEN was rejected by Telegram (401). Check your .env.") from None
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 30))
        except TelegramNetworkError as exc:
            if attempt == attempts:
                raise
            log.warning("Telegram unreachable (%s), retry %s/%s in %.0fs", exc, attempt, attempts, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("unreachable")  # pragma: no cover


async def register_commands(bot: Bot, settings: Settings) -> None:
    try:
        await asyncio.gather(
            bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats()),
            bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats()),
        )
        results = await asyncio.gather(
            *(
                bot.set_my_commands(PRIVATE_COMMANDS + SUPER_ADMIN_EXTRA, scope=BotCommandScopeChat(chat_id=uid))
                for uid in settings.super_admins
            ),
            return_exceptions=True,
        )
        for uid, res in zip(settings.super_admins, results):
            if isinstance(res, Exception):
                log.debug("Cannot set commands for %s: %s", uid, res)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot set bot commands: %s", exc)


def build_dispatcher(
    settings: Settings,
    db: Database,
    service: MembershipService,
    scheduler: ExpiryScheduler,
    metrics: Metrics,
    bot: Bot,
) -> tuple[Dispatcher, ThrottlingMiddleware]:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(
        ErrorReportingMiddleware(
            settings.super_admins if settings.notify_admins_on_error else [], lambda: bot, metrics
        )
    )
    dp.update.outer_middleware(MetricsMiddleware(metrics))
    dp.update.outer_middleware(
        DependenciesMiddleware(db=db, settings=settings, service=service, scheduler=scheduler, metrics=metrics)
    )
    throttle = ThrottlingMiddleware(rate=settings.throttle_rate, burst=settings.throttle_burst, metrics=metrics)
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)
    dp.callback_query.middleware(CallbackAckMiddleware(metrics))

    # order matters: reply-menu taps first (exact labels only, so they also cancel a pending
    # FSM input), then callbacks (FSM text input), then commands, then service events
    dp.include_router(menu.router)
    dp.include_router(callbacks.router)
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(tracking.router)
    return dp, throttle


# ---------------------------------------------------------------------- run
async def run() -> None:
    settings = Settings.from_env()
    setup_logging(settings)
    metrics = Metrics()
    boot = time.perf_counter()

    db = Database(settings.database_path, cache_ttl=settings.cache_ttl)
    await db.connect()

    session = AiohttpSession(timeout=60)
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings, metrics)
    dp, throttle = build_dispatcher(settings, db, service, scheduler, metrics, bot)
    scheduler.on_tick = lambda: throttle.cleanup()

    me = await wait_for_telegram(bot)
    log.info("Member Tracker %s (%s) authenticated as @%s (id=%s)", VERSION_TAG, __version__, me.username, me.id)
    await register_commands(bot, settings)

    # optional HTTP side-car (health / metrics / webhook receiver)
    runner = None
    state = None
    if settings.http_port:
        from bot.web import AppState, build_app, start_http

        state = AppState()
        app = build_app(settings, db, metrics, scheduler, state, dp=dp, bot=bot)
        runner = await start_http(app, settings.http_host, settings.http_port)

    scheduler.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass

    log.info("Startup complete in %.2fs", time.perf_counter() - boot)
    try:
        if settings.webhook_full_url:
            await bot.set_webhook(
                settings.webhook_full_url,
                allowed_updates=ALLOWED_UPDATES,
                secret_token=settings.webhook_secret or None,
                drop_pending_updates=settings.drop_pending_updates,
                max_connections=40,
            )
            if state:
                state.ready = True
            log.info("Webhook mode: %s", settings.webhook_full_url)
            await stop_event.wait()
        else:
            await bot.delete_webhook(drop_pending_updates=settings.drop_pending_updates)
            if state:
                state.ready = True
            polling = asyncio.create_task(
                dp.start_polling(
                    bot,
                    allowed_updates=ALLOWED_UPDATES,
                    handle_signals=False,
                    polling_timeout=30,
                )
            )
            stopper = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if polling in done:
                polling.result()  # re-raise polling failure
            else:
                log.info("Shutdown signal received, stopping polling…")
                await dp.stop_polling()
                try:
                    await asyncio.wait_for(polling, timeout=15)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    polling.cancel()
    finally:
        if state:
            state.ready = False
        scheduler.shutdown()
        if runner:
            await runner.cleanup()
        await db.close()
        await bot.session.close()
        log.info("Bot stopped cleanly")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
    except RuntimeError as exc:
        logging.getLogger("main").error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
