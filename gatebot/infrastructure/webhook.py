import logging
from typing import Awaitable, Callable

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.ports import Messenger, PaymentLinkFactory
from core.services.payment import PaymentService
from core.services.subscription import SubscriptionService
from db.models import User
from db.uow import UnitOfWork
from infrastructure.prodamus import verify_webhook_signature

logger = logging.getLogger(__name__)

OnActivated = Callable[[User], Awaitable[None]]


def create_webhook_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    messenger: Messenger,
    links: PaymentLinkFactory,
    on_activated: OnActivated,
) -> web.Application:
    """HTTP-приложение: вебхук Prodamus и healthcheck.

    `on_activated` вызывается после продления подписки — бот показывает
    пользователю меню со ссылками на клуб.
    """

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def prodamus_webhook(request: web.Request) -> web.Response:
        try:
            form = {k: str(v) for k, v in (await request.post()).items()}
        except Exception:
            logger.exception("Failed to parse webhook body")
            return web.Response(status=400, text="bad request")

        if not verify_webhook_signature(
            form, request.headers.get("Sign", ""), settings.prodamus_secret_key
        ):
            logger.warning("Webhook rejected: bad signature (order_id=%s)", form.get("order_id"))
            return web.Response(status=403, text="bad signature")

        if form.get("payment_status") != "success":
            logger.info("Webhook ignored: payment_status=%s", form.get("payment_status"))
            return web.Response(text="OK")

        async with session_factory() as session:
            uow = UnitOfWork(session)
            payment = await PaymentService(uow, links).mark_paid(form)
            if payment is None:
                return web.Response(text="OK")
            user = await uow.users.get_by_id(payment.user_id)
            if user is None:
                logger.error("Payment %s references missing user %s", payment.id, payment.user_id)
                return web.Response(text="OK")
            await SubscriptionService(uow, messenger, settings).activate(user, payment.days)

        try:
            await on_activated(user)
        except Exception:
            logger.exception("Post-payment notification failed for %s", user.max_user_id)
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/prodamus/webhook", prodamus_webhook)
    return app
