"""Entry point: Telegram Member Tracker Bot."""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from bot.config import Settings
from bot.handlers import admin, callbacks, common, tracking
from bot.middlewares import DependenciesMiddleware, ErrorReportingMiddleware, ThrottlingMiddleware
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler

log = logging.getLogger("main")

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


def setup_logging(settings: Settings) -> None:
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

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def run() -> None:
    settings = Settings.from_env()
    setup_logging(settings)

    db = Database(settings.database_path)
    await db.connect()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings)

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(
        ErrorReportingMiddleware(
            settings.super_admins if settings.notify_admins_on_error else [], lambda: bot
        )
    )
    dp.update.outer_middleware(
        DependenciesMiddleware(db=db, settings=settings, service=service, scheduler=scheduler)
    )
    throttle = ThrottlingMiddleware(rate=settings.throttle_rate, burst=settings.throttle_burst)
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)

    # order matters: callbacks first (FSM text input), then commands, then service events
    dp.include_router(callbacks.router)
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(tracking.router)

    me = await bot.get_me()
    log.info("Starting as @%s (id=%s)", me.username, me.id)
    try:
        await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
        for uid in settings.super_admins:
            try:
                await bot.set_my_commands(
                    PRIVATE_COMMANDS + SUPER_ADMIN_EXTRA, scope=BotCommandScopeChat(chat_id=uid)
                )
            except Exception as exc:  # noqa: BLE001 - admin never started the bot
                log.debug("Cannot set commands for %s: %s", uid, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot set bot commands: %s", exc)

    scheduler.start()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "chat_member",
                "my_chat_member",
                "chat_join_request",
            ],
        )
    finally:
        scheduler.shutdown()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")


if __name__ == "__main__":
    main()
