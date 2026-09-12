# 🤖 Telegram Member Tracker Bot

Professional membership-management bot for Telegram **groups and channels**.
It tracks every member from the moment they join, and automatically removes them after
**1 month (default)** or any **custom duration / exact date** you set. Ideal for paid
communities, subscription channels, trial groups and time-limited access.

Built with Python 3.11+, [aiogram 3](https://docs.aiogram.dev/), SQLite and APScheduler.

---

## ✨ Features

| Area | Details |
|------|---------|
| **Automatic tracking** | Detects joins/leaves in groups, supergroups and channels (via `chat_member` updates). Members present before the bot was added are picked up as soon as they post. |
| **Auto-remove on expiry** | Background scheduler removes expired members every 60 s (configurable). **Kick mode** (user can rejoin) or **Ban mode** (permanent). |
| **Flexible durations** | `30d`, `1m` (calendar month), `2w`, `12h`, `1y`, `1m 15d`, or an **exact date** `2025-12-31`, `31/12/2025 18:30`, or `never`. |
| **Per-chat defaults** | Each group/channel can have its own default duration; falls back to a global default. |
| **Expiry reminders** | DMs users 72h / 24h / 1h before expiry (configurable). Expiry notice on removal. |
| **Whitelist / VIP** | Whitelisted users and chat admins are never removed. |
| **Inline settings panel** | Toggle tracking, auto-kick, kick/ban mode, notifications, welcome message, auto-approve join requests, default duration – all via buttons. |
| **Manage channels from private chat** | Channels have no chat commands, so `/chats` lets admins pick a chat and manage it via DM. |
| **Member cards** | `/info` shows a card with one-tap **+7d / +1m / +3m / ♾ / Remove / Whitelist** buttons. |
| **Audit logs** | Every action is stored and optionally mirrored to a log channel (`/setlog`). |
| **Stats & reports** | `/stats`, `/list`, `/expiring 3d`, `/logs`, global stats for super-admins. |
| **Welcome messages** | Optional templated welcome with `{mention} {name} {expires} {chat}`. |
| **Broadcast** | DM all active members of a chat. |
| **Notes** | Attach notes to members (e.g. payment reference). |
| **Robust** | Rate-limit aware, retries failed removals, permission checks, admin-cache refresh, WAL SQLite. |

---

## 🚀 Quick start

```bash
git clone <this-repo> && cd webapp
cp .env.example .env         # put your BOT_TOKEN and SUPER_ADMINS
pip install -r requirements.txt
python main.py
```

### Docker

```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

### PM2

```bash
pm2 start ecosystem.config.cjs
pm2 logs member-tracker-bot --nostream
```

---

## ⚙️ Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | – | Token from [@BotFather](https://t.me/BotFather) |
| `SUPER_ADMINS` | – | Comma-separated user IDs with full control over every chat |
| `DEFAULT_DURATION` | `1m` | Default membership length for new members |
| `CHECK_INTERVAL` | `60` | Seconds between expiry checks |
| `TIMEZONE` | `Asia/Kolkata` | Timezone for displaying/parsing dates |
| `REMINDER_HOURS` | `72,24,1` | Reminder DM thresholds before expiry (empty = off) |
| `DATABASE_PATH` | `data/bot.db` | SQLite file |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## 🧭 Setup in Telegram

1. **@BotFather** → create bot → copy token.
   Optionally `/setprivacy` → **Disable** so the bot sees group messages (helps pick up
   pre-existing members).
2. Add the bot to your **group or channel** as **Administrator** with the
   **Ban users** (restrict members) permission. For channels also give *Add subscribers* if you
   want `/add` to look up members.
3. Open the bot's private chat → `/chats` → tap the chat → configure with the panel.
4. Done. New members are tracked automatically and removed when their time is up.

> ⚠️ Telegram only sends member join/leave events to bots that are **admins** of the chat.

---

## 📖 Commands

### Chat management
| Command | Description |
|---------|-------------|
| `/chats` | Choose which group/channel to manage (private chat) |
| `/panel` | Inline settings panel |
| `/stats` | Membership statistics |
| `/list [page]` | Active members sorted by expiry |
| `/expiring [7d]` | Members expiring within a window |
| `/logs` | Recent activity |
| `/permissions` | Verify bot can ban users |
| `/forcecheck` | Run expiry check immediately |
| `/setduration <30d\|1m\|never\|global>` | Default duration for this chat |
| `/setlog <chat_id\|here\|off>` | Log destination |
| `/setwelcome <text>` | Welcome template |

### Member management (`<user>` = ID, `@username`, or reply to a message)
| Command | Description |
|---------|-------------|
| `/add <user> [duration\|date\|never]` | Start tracking a user manually |
| `/info <user>` | Member card with quick-action buttons |
| `/extend <user> <1m\|15d\|date>` | Extend from current expiry |
| `/setexpiry <user> <date\|duration\|never>` | Set exact expiry |
| `/remove <user>` | Remove now |
| `/untrack <user>` | Stop tracking without removing |
| `/whitelist [user]` / `/unwhitelist <user>` | VIP protection |
| `/note <user> <text>` | Attach a note |
| `/broadcast <text>` | DM all active members |

### Users
| Command | Description |
|---------|-------------|
| `/mystatus` | Your memberships & expiry |
| `/id` | Show user / chat IDs |
| `/help` | Help |

---

## 🗂 Project structure

```
main.py                      # entry point, polling, command menus
bot/
  config.py                  # env settings
  middlewares.py             # dependency injection
  handlers/
    common.py                # /start /help /mystatus /id
    admin.py                 # admin commands
    callbacks.py             # inline button handlers
    tracking.py              # join/leave/bot-added/join-request events
  services/
    database.py              # aiosqlite persistence layer
    membership.py            # business logic (track, remove, extend, cards)
    scheduler.py             # APScheduler jobs: expiry + reminders
  utils/
    timeparse.py             # durations & dates parsing
    keyboards.py             # inline keyboards
    permissions.py           # admin checks
tests/                       # pytest suite (25 tests)
```

Run tests: `python -m pytest -q`

---

## 📝 Notes

* **Channels**: Telegram does not deliver commands inside channels, so manage them from the
  bot's private chat (`/chats`). Join/leave tracking works normally.
* **Calendar months**: `1m` from 31 Jan → 28/29 Feb (day is clamped).
* **Dates without time** are treated as end-of-day (23:59) in the configured timezone.
* Failed removals (e.g. missing rights) are logged and retried after 1 hour.

## License

MIT
