import asyncio
import logging
import signal

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

BOT_COMMANDS = [{"name": "start", "description": "Открыть меню клуба"}]


async def main() -> None:
    setup_logging()
    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    client = MaxClient(settings.bot_token)

    me = await client.get_me()
    logger.info("Bot @%s (user_id=%s) starting", me.get("username"), me.get("user_id"))
    if not settings.gated_chat_ids:
        logger.warning("gated_chat_ids is empty — вход в чаты не контролируется")
    try:
        await client.set_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("Failed to set bot commands")

    bot = GateBot(client, settings, session_factory, ProdamusClient(settings))
    app = create_webhook_app(
        settings, session_factory, bot.messenger, bot.links, bot.on_subscription_activated
    )

    jobs = [subscription_worker(session_factory, bot.messenger, settings)]
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
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if task is not stopper and task.exception():
            logger.error("Background job crashed", exc_info=task.exception())

    logger.info("Shutting down")
    for task in [stopper, *tasks]:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
