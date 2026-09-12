"""Entry point: Telegram Member Tracker Bot."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats

from bot.config import Settings
from bot.handlers import admin, callbacks, common, tracking
from bot.middlewares import DependenciesMiddleware
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler

log = logging.getLogger("main")

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Start / help"),
    BotCommand(command="chats", description="Select a chat to manage"),
    BotCommand(command="panel", description="Settings panel for selected chat"),
    BotCommand(command="stats", description="Membership statistics"),
    BotCommand(command="list", description="Active members"),
    BotCommand(command="expiring", description="Members expiring soon"),
    BotCommand(command="info", description="Member details"),
    BotCommand(command="add", description="Track a member manually"),
    BotCommand(command="extend", description="Extend a membership"),
    BotCommand(command="setexpiry", description="Set exact expiry date"),
    BotCommand(command="remove", description="Remove a member now"),
    BotCommand(command="whitelist", description="Never auto-remove a user"),
    BotCommand(command="mystatus", description="Your memberships"),
    BotCommand(command="help", description="Full command list"),
]

GROUP_COMMANDS = [
    BotCommand(command="panel", description="Settings panel"),
    BotCommand(command="stats", description="Membership statistics"),
    BotCommand(command="list", description="Active members"),
    BotCommand(command="info", description="Member details (reply)"),
    BotCommand(command="extend", description="Extend membership (reply)"),
    BotCommand(command="remove", description="Remove member (reply)"),
    BotCommand(command="mystatus", description="Your membership"),
    BotCommand(command="id", description="Show IDs"),
]


async def run() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    db = Database(settings.database_path)
    await db.connect()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    service = MembershipService(bot, db, settings)
    scheduler = ExpiryScheduler(service, db, settings)

    dp = Dispatcher()
    deps = DependenciesMiddleware(db=db, settings=settings, service=service, scheduler=scheduler)
    dp.update.outer_middleware(deps)

    # order matters: tracking handles service messages; admin/common handle commands
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(callbacks.router)
    dp.include_router(tracking.router)

    me = await bot.get_me()
    log.info("Starting as @%s (id=%s)", me.username, me.id)
    await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

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
