"""Доставка событий MAX в GateBot: long polling или webhook."""
import asyncio
import hmac
import json
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.uow import UnitOfWork
from maxbot.client import MaxClient

logger = logging.getLogger(__name__)

UpdateHandler = Callable[[dict], Awaitable[None]]

MARKER_KEY = "updates_marker"
SUBSCRIPTION_CHECK_SECONDS = 3600


async def run_polling(
    client: MaxClient,
    handle: UpdateHandler,
    session_factory: async_sessionmaker[AsyncSession],
    update_types: list[str],
) -> None:
    """Long polling. marker храним в БД, чтобы после рестарта не терять события."""
    for sub in await client.get_subscriptions():
        # При активной webhook-подписке /updates молчит.
        logger.warning("Removing webhook subscription %s to use long polling", sub.get("url"))
        await client.unsubscribe(sub["url"])

    async with session_factory() as session:
        raw = await UnitOfWork(session).state.get(MARKER_KEY)
    marker = int(raw) if raw else None
    logger.info("Long polling started (marker=%s)", marker)

    while True:
        try:
            data = await client.get_updates(marker, timeout=30, types=update_types)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("get_updates failed")
            await asyncio.sleep(5)
            continue
        for update in data.get("updates", []):
            await handle(update)
        new_marker = data.get("marker")
        if new_marker is not None and new_marker != marker:
            marker = new_marker
            async with session_factory() as session:
                uow = UnitOfWork(session)
                await uow.state.set(MARKER_KEY, str(marker))
                await uow.commit()


class _Deduplicator:
    """MAX может доставить событие повторно — пропускаем уже виденные."""

    def __init__(self, size: int = 2000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def is_new(self, update: dict) -> bool:
        key = update_key(update)
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


def update_key(update: dict) -> str:
    callback_id = (update.get("callback") or {}).get("callback_id")
    if callback_id:
        return f"cb:{callback_id}"
    mid = ((update.get("message") or {}).get("body") or {}).get("mid")
    if mid and update.get("update_type") == "message_created":
        return f"msg:{mid}"
    return json.dumps(update, sort_keys=True, ensure_ascii=False)


def add_webhook_route(app: web.Application, secret: str, handle: UpdateHandler) -> None:
    """POST /max/webhook. Отвечаем сразу, событие обрабатываем в фоне (лимит MAX — 30 с)."""
    dedup = _Deduplicator()
    tasks: set[asyncio.Task] = set()

    async def max_webhook(request: web.Request) -> web.Response:
        got = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not secret or not hmac.compare_digest(got, secret):
            return web.Response(status=403, text="bad secret")
        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        if dedup.is_new(update):
            task = asyncio.create_task(handle(update))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return web.Response(text="OK")

    app.router.add_post("/max/webhook", max_webhook)


async def keep_webhook_subscription(
    client: MaxClient, url: str, secret: str, update_types: list[str]
) -> None:
    """MAX молча отписывает бота после 8 часов недоставок — периодически переподписываемся."""
    while True:
        try:
            subs = await client.get_subscriptions()
            if not any(s.get("url") == url for s in subs):
                await client.subscribe(url, secret, update_types)
                logger.info("Webhook subscription created: %s", url)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Webhook subscription check failed")
        await asyncio.sleep(SUBSCRIPTION_CHECK_SECONDS)
