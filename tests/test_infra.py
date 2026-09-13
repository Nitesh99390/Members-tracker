"""Tests for the performance/reliability layer: TTL cache, metrics, Telegram retry wrapper,
callback de-duplication middleware and the HTTP side-car."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, Message, User

from bot.middlewares import CallbackAckMiddleware
from bot.services.metrics import Metrics
from bot.utils.cache import TTLCache
from bot.utils.telegram import TelegramUnavailable, error_text, is_not_modified, tg_call, tg_try


# ---------------------------------------------------------------- TTLCache
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_cache_get_set_and_expiry():
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl=10, maxsize=10, clock=clock)
    assert cache.get("a") is None
    assert cache.set("a", 1) == 1
    assert cache.get("a") == 1
    clock.now += 11
    assert cache.get("a") is None
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 2


def test_cache_lru_eviction_and_invalidate():
    cache: TTLCache[tuple[int, int], bool] = TTLCache(ttl=60, maxsize=2)
    cache.set((1, 1), True)
    cache.set((1, 2), True)
    cache.set((2, 1), True)  # evicts (1, 1)
    assert cache.peek((1, 1)) is None
    assert cache.stats()["evictions"] == 1
    assert cache.invalidate_where(lambda k: k[0] == 1) == 1
    assert len(cache) == 1
    assert cache.pop((2, 1)) is True
    assert len(cache) == 0


def test_cache_per_entry_ttl_and_purge():
    clock = FakeClock()
    cache: TTLCache[str, str | None] = TTLCache(ttl=100, clock=clock)
    cache.set("neg", None, ttl=5)
    cache.set("pos", "x")
    clock.now += 6
    assert cache.purge_expired() == 1
    assert cache.get("pos") == "x"


def test_cache_disabled_when_ttl_zero():
    cache: TTLCache[str, int] = TTLCache(ttl=0)
    cache.set("a", 1)
    assert cache.get("a") is None
    assert len(cache) == 0


# ----------------------------------------------------------------- Metrics
def test_metrics_snapshot_and_prometheus():
    m = Metrics()
    m.inc("updates_total")
    m.inc("updates_total", 2)
    m.set("active_members", 7)
    m.observe("handler_latency", 0.010)
    m.observe("handler_latency", 0.030)
    snap = m.snapshot()
    assert snap["counters"]["updates_total"] == 3
    assert snap["gauges"]["active_members"] == 7
    lat = snap["latency"]["handler_latency"]
    assert lat["count"] == 2 and lat["max_ms"] == 30.0 and lat["mean_ms"] == 20.0
    text = m.render_prometheus()
    assert "member_tracker_updates_total 3" in text
    assert "member_tracker_active_members 7" in text
    assert "member_tracker_handler_latency_seconds_count 2" in text


# ------------------------------------------------------------- tg_call/try
def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=GetMe(), message="Too Many Requests", retry_after=seconds)


@pytest.mark.asyncio
async def test_tg_call_retries_network_then_succeeds(monkeypatch):
    monkeypatch.setattr("bot.utils.telegram.BASE_BACKOFF", 0.001)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TelegramNetworkError(method=GetMe(), message="boom")
        return "ok"

    assert await tg_call(flaky, retries=2) == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_tg_call_gives_up_with_unavailable(monkeypatch):
    monkeypatch.setattr("bot.utils.telegram.BASE_BACKOFF", 0.001)

    async def dead():
        raise TelegramNetworkError(method=GetMe(), message="down")

    with pytest.raises(TelegramUnavailable) as exc:
        await tg_call(dead, retries=1, label="x")
    assert exc.value.attempts == 2
    assert "down" in str(exc.value)


@pytest.mark.asyncio
async def test_tg_call_sleeps_on_short_flood_and_reraises_long(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr("bot.utils.telegram.asyncio.sleep", fake_sleep)
    calls = 0

    async def flood_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _retry_after(2)
        return "ok"

    assert await tg_call(flood_once, retries=1) == "ok"
    assert slept and slept[0] >= 2

    async def flood_long():
        raise _retry_after(600)

    with pytest.raises(TelegramRetryAfter):
        await tg_call(flood_long, retries=3)


@pytest.mark.asyncio
async def test_tg_call_does_not_retry_bad_request():
    calls = 0

    async def bad():
        nonlocal calls
        calls += 1
        raise TelegramBadRequest(method=GetMe(), message="chat not found")

    with pytest.raises(TelegramBadRequest):
        await tg_call(bad, retries=3)
    assert calls == 1


@pytest.mark.asyncio
async def test_tg_try_swallows_errors():
    async def bad():
        raise TelegramBadRequest(method=GetMe(), message="nope")

    assert await tg_try(bad) is None


def test_error_helpers():
    exc = TelegramBadRequest(method=GetMe(), message="Bad Request: message is not modified")
    assert is_not_modified(exc)
    assert "not modified" in error_text(exc)
    assert error_text(ValueError("x")) == "x"
    assert error_text(ValueError()) == "ValueError"


# ---------------------------------------------------- CallbackAckMiddleware
def _callback(data: str = "d", message_id: int = 5) -> CallbackQuery:
    user = User(id=1, is_bot=False, first_name="a")
    msg = Message(message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"))
    return CallbackQuery(id="x", from_user=user, chat_instance="c", data=data, message=msg)


@pytest.mark.asyncio
async def test_callback_ack_auto_answers_and_dedups():
    metrics = Metrics()
    mw = CallbackAckMiddleware(metrics)
    answers: list[int] = []

    async def fake_answer(*_a, **_k):
        answers.append(1)

    # a double tap arrives as two distinct CallbackQuery updates with identical (user, message, data)
    first_tap, second_tap = _callback(), _callback()
    object.__setattr__(first_tap, "answer", fake_answer)
    object.__setattr__(second_tap, "answer", fake_answer)
    gate = asyncio.Event()

    async def slow_handler(event, data):
        await gate.wait()
        return "done"

    first = asyncio.create_task(mw(slow_handler, first_tap, {}))
    await asyncio.sleep(0.01)
    # second tap of the same button while the first is in flight → dropped
    assert await mw(slow_handler, second_tap, {}) is None
    assert metrics.counters["callback_dedup_total"] == 1
    gate.set()
    assert await first == "done"
    # duplicate answered once, original auto-answered once (handler never called answer)
    assert len(answers) == 2
    # key released → the same button can be pressed again afterwards
    third_tap = _callback()
    object.__setattr__(third_tap, "answer", fake_answer)
    assert await mw(slow_handler, third_tap, {}) == "done"


@pytest.mark.asyncio
async def test_callback_ack_respects_handler_answer():
    mw = CallbackAckMiddleware()
    call = _callback()
    answers: list[int] = []

    async def fake_answer(*_a, **_k):
        answers.append(1)

    object.__setattr__(call, "answer", fake_answer)

    async def handler(event, data):
        await event.answer("hi")
        return 1

    assert await mw(handler, call, {}) == 1
    assert len(answers) == 1


# --------------------------------------------------------------- web side-car
@pytest.mark.asyncio
async def test_http_sidecar_endpoints(tmp_path, monkeypatch):
    from aiohttp import ClientSession

    from bot.config import Settings
    from bot.services.database import Database
    from bot.web import AppState, build_app, start_http

    monkeypatch.setenv("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    settings = Settings.from_env()
    db = Database(settings.database_path)
    await db.connect()

    class FakeScheduler:
        running = True
        last_run = None
        last_run_duration = 0.0

    metrics = Metrics()
    metrics.inc("updates_total", 5)
    state = AppState(ready=False)
    app = build_app(settings, db, metrics, FakeScheduler(), state)
    runner = await start_http(app, "127.0.0.1", 0)
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession() as http:
            async with http.get(f"{base}/healthz") as r:
                assert r.status == 200
                assert (await r.json())["database"] is True
            async with http.get(f"{base}/readyz") as r:
                assert r.status == 503
                assert "bot not started" in (await r.json())["problems"]
            state.ready = True
            async with http.get(f"{base}/readyz") as r:
                assert r.status == 200
            async with http.get(f"{base}/metrics") as r:
                assert r.status == 200
                assert "member_tracker_updates_total 5" in await r.text()
            async with http.get(f"{base}/metrics?format=json") as r:
                assert (await r.json())["counters"]["updates_total"] == 5
    finally:
        await runner.cleanup()
        await db.close()


def test_settings_webhook_and_http(monkeypatch):
    from bot.config import Settings

    monkeypatch.setenv("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    monkeypatch.setenv("WEBHOOK_URL", "https://bot.example.com/")
    monkeypatch.setenv("WEBHOOK_PATH", "tg/hook")
    monkeypatch.setenv("WEBHOOK_SECRET", "abc$%^123")
    monkeypatch.delenv("HTTP_PORT", raising=False)
    s = Settings.from_env()
    assert s.webhook_full_url == "https://bot.example.com/tg/hook"
    assert s.webhook_secret == "abc123"
    assert s.http_port == 8080  # implied by webhook mode

    monkeypatch.setenv("WEBHOOK_URL", "http://insecure.example.com")
    s = Settings.from_env()
    assert s.webhook_url is None  # non-https rejected → polling
    assert os.environ["WEBHOOK_URL"].startswith("http://")
