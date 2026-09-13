# 🤖 Telegram Member Tracker Bot `v1`

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
| **Ask-on-join prompt** | When someone joins, the bot DMs the **owner** (or all admins) a compact prompt: *Keep default*, three quick picks, *More options* (all presets + custom date) and *Remove*. Unanswered prompts fall back to the default duration after `ASK_TIMEOUT_HOURS`. |
| **Invite links with preset duration** | `/invite 3m VIP` creates a Telegram invite link; anyone joining through it gets 3 months automatically – no prompt needed. |
| **Auto-remove on expiry** | Background scheduler removes expired members every 60 s (configurable). **Kick mode** (user can rejoin) or **Ban mode** (permanent). |
| **Flexible durations** | `30d`, `1m` (calendar month), `2w`, `12h`, `1y`, `1m 15d`, or an **exact date** `2025-12-31`, `31/12/2025 18:30`, or `never`. |
| **Per-chat defaults** | Each group/channel can have its own default duration; falls back to a global default. |
| **Expiry reminders** | DMs users 72h / 24h / 1h before expiry (configurable). Expiry notice on removal. |
| **Whitelist / VIP** | Whitelisted users and chat admins are never removed. |
| **Button-first dashboard** | Home → chat → dashboard → settings. Only the essentials on each screen; rarely used switches live under *Advanced*. |
| **Manage channels from private chat** | Channels have no chat commands, so `/chats` lets admins pick a chat and manage it via DM. |
| **Member cards** | `/info` shows a card with one-tap **+7d / +1m / +3m / ♾ / Remove / Whitelist** buttons. |
| **Audit logs** | Every action is stored and optionally mirrored to a log channel (`/setlog`). |
| **Stats & reports** | `/stats`, `/list`, `/expiring 3d`, `/logs`, global stats for super-admins. |
| **Welcome messages** | Optional templated welcome with `{mention} {name} {expires} {chat}`. |
| **Broadcast** | DM all active members of a chat. |
| **Notes** | Attach notes to members (e.g. payment reference). |
| **Search & sync** | `/search` by name/username/ID; `/sync` cross-checks stored members against Telegram. |
| **Maintenance** | Daily log pruning + SQLite backup (optionally sent to a chat), `/backup` and `/health` for super-admins, rotating log files. |
| **Protection** | Per-user anti-spam throttling for commands/buttons; unhandled errors are DM'd to super-admins. |
| **Robust** | Rate-limit aware, retries failed removals with backoff, permission checks, admin-cache refresh, WAL SQLite, schema auto-migration. |
| **Fast** | In-memory TTL caches for chat settings / admin & whitelist checks, covering SQLite indexes, concurrent (bounded) removals, reminders, prompts and broadcasts, instant callback acks with double-tap de-duplication. |
| **Ops-ready** | Long polling **or webhook** mode, optional HTTP side-car with `/healthz` · `/readyz` · `/metrics` (Prometheus), JSON logs, graceful SIGTERM shutdown, startup retries when Telegram is unreachable, Docker `HEALTHCHECK`. |

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
| `LOG_DIR` | `logs` | Directory for rotating log files |
| `ASK_ON_JOIN` | `true` | Ask the owner how long each new member may stay (per-chat override in `/panel`) |
| `ASK_TIMEOUT_HOURS` | `24` | Hours to wait for an answer before keeping the default duration |
| `BACKUP_CHAT_ID` | – | Optional chat/user ID that receives the daily DB backup |
| `BACKUP_HOUR_UTC` | `3` | Hour (UTC) for daily maintenance (log pruning + backup) |
| `NOTIFY_ADMINS_ON_ERROR` | `true` | DM super-admins on unhandled errors |
| `THROTTLE_RATE` / `THROTTLE_BURST` | `0.5` / `5` | Anti-spam: allow `BURST` actions, then min `RATE` seconds between actions |
| `HTTP_PORT` / `HTTP_HOST` | – / `0.0.0.0` | Enable the HTTP side-car (`/healthz`, `/readyz`, `/metrics`). Empty = disabled |
| `WEBHOOK_URL` | – | Public `https://` base URL → switch from polling to webhook mode (implies `HTTP_PORT=8080`) |
| `WEBHOOK_PATH` / `WEBHOOK_SECRET` | `/webhook` / – | Webhook receiver path and secret token Telegram must echo back |
| `LOG_JSON` | `false` | Structured JSON log lines instead of human-readable text |
| `DROP_PENDING_UPDATES` | `false` | Discard updates queued while the bot was offline |
| `CACHE_TTL` | `120` | Seconds to cache chat settings / admin checks in memory (`0` disables) |

### Polling vs. webhook

* **Polling** (default) – zero configuration, works behind NAT. Leave `WEBHOOK_URL` empty.
* **Webhook** – lower latency on busy bots. Put the bot behind an HTTPS reverse proxy
  (Caddy / nginx / Cloudflare Tunnel) and set:

  ```env
  WEBHOOK_URL=https://bot.example.com
  WEBHOOK_SECRET=some-random-string
  HTTP_PORT=8080
  ```

  The bot registers `https://bot.example.com/webhook` with Telegram on start-up and
  serves it from the same aiohttp side-car as `/healthz`.

### Health & metrics

With `HTTP_PORT` set:

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Liveness – process up, SQLite answers |
| `GET /readyz` | Readiness – authenticated, polling/webhook active, scheduler not stale |
| `GET /metrics` | Prometheus text format (`?format=json` for JSON): updates, errors, throttled taps, handler latency p95, removals, reminders, cache hit-rates, DB size… |

`/health` in Telegram (super-admins) shows the same numbers inline.

---

## 🧭 Setup in Telegram

1. **@BotFather** → create bot → copy token.
   Optionally `/setprivacy` → **Disable** so the bot sees group messages (helps pick up
   pre-existing members).
2. Add the bot to your **group or channel** as **Administrator** with the
   **Ban users** (restrict members) permission. For channels also give *Add subscribers* if you
   want `/add` to look up members.
3. The **owner must `/start` the bot** in private chat once, so it can DM join prompts.
4. Open the bot's private chat → `/chats` → tap the chat → configure with the panel.
5. Done. On every join you get a prompt asking how long the member may stay; members are
   removed automatically when their time is up.

> ⚠️ Telegram only sends member join/leave events to bots that are **admins** of the chat.

---

## 🖱 Button-first UI

The bot is designed so that an owner almost never types a command:

| Screen | What you see |
|--------|--------------|
| **Bottom menu** (persistent) | `📊 Dashboard` `👥 Members` · `🔔 Pending` `🔗 Invite links` · `⚙️ Settings` `📂 My chats` · `📖 Help` |
| **Home** (`/start`) | `📂 My chats` · `➕ Add to group` · `📖 Help` |
| **Dashboard** | `👥 Members` `📊 Overview` · `🔔 Pending (n)` `🔗 Invite links` · `⚙️ Settings` `📜 Activity` |
| **Settings** | Duration · Tracking · Auto-remove · Ask on join · Kick/Ban · `🔧 Advanced` |
| **Join prompt** (DM) | `✅ Keep · 1 month` · three quick picks · `⋯ More options` / `🚫 Remove` |
| **Member card** | `+1 month` `+3 months` `♾ Lifetime` · `✏️ Custom` `⋯ More` |
| **Help** | Short topic pages instead of a command wall |

The **bottom menu** is a Telegram reply keyboard installed on `/start` (and refreshed the moment your first
chat gets tracked). It adapts to the user: regular users see `📇 My memberships · 📖 Help`, admins without
a chat see `➕ Add to group · 📖 Help`, admins with chats get the full menu. Every tap opens the same screen
as the matching inline button and cancels any pending text input, so you can never get "stuck" in a flow.
With one tracked chat it is auto-selected; with several, `📂 My chats` / a chat picker appears.

Typing a **user ID or @username** in the private chat opens that member's card directly.
The Telegram "/" menu only lists `start`, `chats`, `pending`, `help` (and reply-shortcuts in groups).

## 📖 Commands (power users)

All commands still work; they are simply not advertised in the menu.

### Chat management
| Command | Description |
|---------|-------------|
| `/chats` (`/menu`) | Open the dashboard for a group/channel |
| `/pending` | Joins waiting for your duration decision |
| `/stats` · `/list [page]` · `/expiring [7d]` · `/logs` | Reports |
| `/search <name\|@user\|id>` | Find a member |
| `/permissions` · `/forcecheck` · `/sync` | Diagnostics |
| `/setduration <30d\|1m\|never\|global>` | Default duration for this chat |
| `/setlog <chat_id\|here\|off>` · `/setwelcome <text>` | Log destination / welcome template |

### Member management (`<user>` = ID, `@username`, or reply to a message)
| Command | Description |
|---------|-------------|
| `/add <user> [duration\|date\|never]` | Start tracking a user manually |
| `/info <user>` | Member card with quick-action buttons |
| `/ask <user>` | (Re)send the duration prompt to the owner/admins |
| `/extend <user> <1m\|15d\|date>` · `/setexpiry <user> <date\|never>` | Change expiry |
| `/remove <user>` · `/untrack <user>` | Remove now / stop tracking |
| `/whitelist [user]` · `/unwhitelist <user>` · `/note <user> <text>` | VIP & notes |
| `/broadcast <text>` | DM all active members |

### Invite links
| Command | Description |
|---------|-------------|
| `/invite <3m\|1y\|never> [label]` | Link with a preset membership length |
| `/invites` · `/revoke <link>` | List / revoke |

### Super-admins: `/gstats` · `/backup` · `/health` &nbsp;&nbsp; Users: `/mystatus` · `/id` · `/help`

---

## 🗂 Project structure

```
main.py                      # entry point: polling/webhook, HTTP side-car, graceful shutdown
bot/
  config.py                  # env settings
  middlewares.py             # DI, metrics, throttling, callback ack/de-dup, error reporting
  web.py                     # aiohttp side-car: /healthz /readyz /metrics + webhook receiver
  handlers/
    common.py                # /start /help /mystatus /id
    menu.py                  # persistent bottom-menu (reply keyboard) taps
    admin.py                 # admin commands
    callbacks.py             # inline button handlers
    tracking.py              # join/leave/bot-added/join-request/invite-link events
  services/
    database.py              # aiosqlite persistence layer
    membership.py            # business logic (track, remove, extend, cards)
    scheduler.py             # APScheduler jobs: expiry, reminders, prompt timeouts, housekeeping, maintenance
    metrics.py               # counters / gauges / latency + Prometheus exposition
  utils/
    timeparse.py             # durations & dates parsing
    keyboards.py             # inline keyboards + bottom reply menu
    permissions.py           # admin checks
    cache.py                 # TTL + LRU cache for hot read paths
    telegram.py              # tg_call / tg_try: retries for flood limits & network blips
tests/                       # pytest suite (78 tests)
```

Run tests: `python -m pytest -q`

---

## 📝 Notes

* **Channels**: Telegram does not deliver commands inside channels, so manage them from the
  bot's private chat (`/chats`). Join/leave tracking works normally.
* **Calendar months**: `1m` from 31 Jan → 28/29 Feb (day is clamped).
* **Dates without time** are treated as end-of-day (23:59) in the configured timezone.
* Failed removals (e.g. missing rights) are logged and retried after 1 hour; after 5 failures
  the retry backs off to once a day and the owner is notified.
* Local backups are kept in `data/backups/` (last 7 days).

## License

MIT
