"""Application configuration loaded from environment variables / .env file.

Every value is validated and falls back to a safe default so that a typo in
``.env`` never crashes the bot at startup (except a missing BOT_TOKEN).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def _parse_int_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            log.warning("Ignoring non-integer value %r in list setting", part)
    return result


def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer, using default %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("%s=%s below minimum %s, clamping", name, value, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        log.warning("%s=%s above maximum %s, clamping", name, value, maximum)
        value = maximum
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number, using default %s", name, raw, default)
        return default


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer, ignoring", name, raw)
        return None


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
    log_dir: str = "logs"
    # --- join prompt ---------------------------------------------------------
    ask_on_join_default: bool = True
    ask_timeout_hours: int = 24
    # --- maintenance ---------------------------------------------------------
    backup_chat_id: int | None = None
    backup_hour_utc: int = 3
    notify_admins_on_error: bool = True
    digest_hour: int = 9  # local hour (TIMEZONE) for the daily expiring digest
    # --- protection ----------------------------------------------------------
    throttle_rate: float = 0.5  # min seconds between actions per user
    throttle_burst: int = 5  # actions allowed in a burst before throttling
    # --- runtime / ops ---------------------------------------------------------
    http_host: str = "0.0.0.0"
    http_port: int | None = None  # enable /healthz + /metrics side-car
    webhook_url: str | None = None  # public base URL → switch from polling to webhook
    webhook_path: str = "/webhook"
    webhook_secret: str = ""
    log_json: bool = False
    drop_pending_updates: bool = False
    cache_ttl: float = 120.0

    @property
    def webhook_full_url(self) -> str | None:
        if not self.webhook_url:
            return None
        return self.webhook_url.rstrip("/") + self.webhook_path

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - invalid tz fallback
            return ZoneInfo("UTC")

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and fill in your token."
            )
        if not _TOKEN_RE.match(token):
            log.warning("BOT_TOKEN does not look like a valid Telegram token — double-check it.")

        db_path = os.getenv("DATABASE_PATH", "data/bot.db").strip() or "data/bot.db"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        log_dir = os.getenv("LOG_DIR", "logs").strip() or "logs"
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        timezone = os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("TIMEZONE=%r is invalid, falling back to UTC", timezone)
            timezone = "UTC"

        default_duration = os.getenv("DEFAULT_DURATION", "1m").strip() or "1m"
        # validate lazily-imported parser to avoid circular import at module load
        from bot.utils.timeparse import ParseError, is_permanent, parse_duration

        if not is_permanent(default_duration):
            try:
                parse_duration(default_duration)
            except ParseError:
                log.warning("DEFAULT_DURATION=%r is invalid, using 1m", default_duration)
                default_duration = "1m"

        level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            level = "INFO"

        webhook_url = os.getenv("WEBHOOK_URL", "").strip() or None
        if webhook_url and not webhook_url.startswith("https://"):
            log.warning("WEBHOOK_URL must be https:// — falling back to long polling")
            webhook_url = None
        webhook_path = os.getenv("WEBHOOK_PATH", "/webhook").strip() or "/webhook"
        if not webhook_path.startswith("/"):
            webhook_path = "/" + webhook_path
        webhook_secret = re.sub(r"[^A-Za-z0-9_-]", "", os.getenv("WEBHOOK_SECRET", ""))[:256]
        http_port = _env_optional_int("HTTP_PORT")
        if webhook_url and http_port is None:
            http_port = 8080
            log.info("WEBHOOK_URL set without HTTP_PORT — listening on %s", http_port)
        try:
            cache_ttl = max(0.0, float(os.getenv("CACHE_TTL", "120") or 120))
        except ValueError:
            cache_ttl = 120.0

        return cls(
            bot_token=token,
            super_admins=_parse_int_list(os.getenv("SUPER_ADMINS", "")),
            database_path=db_path,
            default_duration=default_duration,
            check_interval=_env_int("CHECK_INTERVAL", 60, minimum=15, maximum=3600),
            timezone=timezone,
            reminder_hours=sorted(
                {h for h in _parse_int_list(os.getenv("REMINDER_HOURS", "72,24,1")) if h > 0},
                reverse=True,
            ),
            log_level=level,
            log_dir=log_dir,
            ask_on_join_default=_env_bool("ASK_ON_JOIN", True),
            ask_timeout_hours=_env_int("ASK_TIMEOUT_HOURS", 24, minimum=1, maximum=24 * 30),
            backup_chat_id=_env_optional_int("BACKUP_CHAT_ID"),
            backup_hour_utc=_env_int("BACKUP_HOUR_UTC", 3, minimum=0, maximum=23),
            notify_admins_on_error=_env_bool("NOTIFY_ADMINS_ON_ERROR", True),
            digest_hour=_env_int("DIGEST_HOUR", 9, minimum=0, maximum=23),
            throttle_rate=max(0.1, _env_float("THROTTLE_RATE", 0.5)),
            throttle_burst=_env_int("THROTTLE_BURST", 5, minimum=1, maximum=50),
            http_host=os.getenv("HTTP_HOST", "0.0.0.0").strip() or "0.0.0.0",
            http_port=http_port,
            webhook_url=webhook_url,
            webhook_path=webhook_path,
            webhook_secret=webhook_secret,
            log_json=_env_bool("LOG_JSON", False),
            drop_pending_updates=_env_bool("DROP_PENDING_UPDATES", False),
            cache_ttl=cache_ttl,
        )
