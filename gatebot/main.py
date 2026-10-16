import asyncio
import logging
import signal
import sys

from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import Settings
from core.services.worker import subscription_worker
from core.utils import setup_logging
from infrastructure.prodamus import ProdamusClient
from infrastructure.webhook import create_webhook_app
from maxbot.bot import UPDATE_TYPES, GateBot
from maxbot.client import MaxClient
from maxbot.transport import add_webhook_route, keep_webhook_subscription, run_polling

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    {"name": "start", "description": "Открыть меню клуба"},
    {"name": "id", "description": "Показать мой MAX ID"},
]


async def announce(client: MaxClient, settings: Settings) -> None:
    """Проверка токена и команды бота. Ретраим в фоне: недоступность MAX
    не должна мешать приёму оплат (HTTP-сервер уже работает)."""
    while True:
        try:
            me = await client.get_me()
            logger.info("Bot @%s (user_id=%s) is up", me.get("username"), me.get("user_id"))
            await client.set_commands(BOT_COMMANDS)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MAX API is unavailable, retrying in 30s")
            await asyncio.sleep(30)


async def main() -> int:
    setup_logging()
    settings = Settings()  # type: ignore[call-arg]
    if problems := settings.problems():
        for problem in problems:
            logger.error("Config: %s", problem)
        return 2
    if not settings.gated_chat_ids:
        logger.warning("gated_chat_ids пуст — вход в клуб не контролируется")
    if not settings.owner_ids:
        logger.warning("owner_ids пуст — админка и уведомления владельцам недоступны")

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    client = MaxClient(settings.bot_token)
    bot = GateBot(client, settings, session_factory, ProdamusClient(settings))

    app = create_webhook_app(
        settings, session_factory, bot.messenger, bot.links, bot.on_subscription_activated
    )
    jobs = [
        announce(client, settings),
        subscription_worker(session_factory, bot.messenger, settings),
    ]
    if settings.max_webhook_url:
        add_webhook_route(app, settings.max_webhook_secret, bot.handle_update)
        jobs.append(keep_webhook_subscription(
            client, settings.max_webhook_url, settings.max_webhook_secret, UPDATE_TYPES
        ))
        logger.info("MAX updates via webhook %s", settings.max_webhook_url)
    else:
        jobs.append(run_polling(client, bot.handle_update, session_factory, UPDATE_TYPES))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.webhook_port).start()
    logger.info("HTTP server on port %s", settings.webhook_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(job) for job in jobs]
    announce_task = tasks[0]
    stopper = asyncio.create_task(stop.wait())
    exit_code = 0
    pending = {stopper, *tasks}
    while not stop.is_set():
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        crashed = [t for t in done if t is not stopper and not (t is announce_task and not t.exception())]
        if crashed:
            for task in crashed:
                logger.error("Background job stopped", exc_info=task.exception())
            exit_code = 1
            break

    logger.info("Shutting down")
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await runner.cleanup()
    if bot._background:
        # Незаконченную рассылку не ждём дольше 10 с — docker всё равно убьёт процесс.
        await asyncio.wait(bot._background, timeout=10)
    await client.close()
    await engine.dispose()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
