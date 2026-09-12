"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _parse_int_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class Settings:
    bot_token: str
    super_admins: list[int] = field(default_factory=list)
    database_path: str = "data/bot.db"
    default_duration: str = "1m"
    check_interval: int = 60
    timezone: str = "Asia/Kolkata"
    reminder_hours: list[int] = field(default_factory=lambda: [72, 24, 1])
    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:  # pragma: no cover - invalid tz fallback
            return ZoneInfo("UTC")

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and fill in your token."
            )
        db_path = os.getenv("DATABASE_PATH", "data/bot.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            super_admins=_parse_int_list(os.getenv("SUPER_ADMINS", "")),
            database_path=db_path,
            default_duration=os.getenv("DEFAULT_DURATION", "1m").strip() or "1m",
            check_interval=max(15, int(os.getenv("CHECK_INTERVAL", "60"))),
            timezone=os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata",
            reminder_hours=sorted(
                set(_parse_int_list(os.getenv("REMINDER_HOURS", "72,24,1"))), reverse=True
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
