import logging
from typing import Awaitable, Callable

import aiohttp
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.notify import alert_owners
from core.ports import Messenger, PaymentLinkFactory
from core.services.payment import PaymentService
from core.services.subscription import SubscriptionService
from db.models import User
from db.uow import UnitOfWork
from infrastructure.prodamus import parse_form_body, verify_webhook_signature
from texts import PAYMENT_UNMATCHED_ALERT

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

    `on_activated` вызывается после продления подписки — бот присылает
    пользователю ссылку на клуб.
    """

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def read_form(request: web.Request) -> dict[str, str] | None:
        try:
            return {k: str(v) for k, v in (await request.post()).items()}
        except Exception:
            logger.exception("Failed to parse webhook body")
            return None

    async def prodamus_webhook(request: web.Request) -> web.Response:
        form = await read_form(request)
        if form is None:
            return web.Response(status=400, text="bad request")
        return await handle_payment(request, form)

    async def prodamus_router(request: web.Request) -> web.Response:
        """Общий вебхук кабинета Prodamus для нескольких ботов.

        Наши уведомления (customer_extra = max:<id>) обрабатываем сами, остальные
        пересылаем без изменений (тело и подпись) в prodamus_forward_url — TG-боту.
        Так URL в кабинете Prodamus менять не нужно (docs/adr/0004).
        """
        raw = await request.read()
        try:
            form = parse_form_body(raw, request.headers.get("Content-Type", ""))
        except Exception:
            logger.exception("Router: failed to parse body")
            form = None
        if form is not None and await is_ours(form):
            return await handle_payment(request, form)
        if not settings.prodamus_forward_url:
            logger.warning("Router: foreign notification and no prodamus_forward_url — dropped")
            return web.Response(text="OK")
        headers = {
            name: value for name, value in request.headers.items()
            if name.lower() in ("content-type", "sign", "user-agent")
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                async with session.post(settings.prodamus_forward_url, data=raw, headers=headers) as resp:
                    body = await resp.read()
                    logger.info("Router: forwarded notification, upstream answered %s", resp.status)
                    return web.Response(
                        status=resp.status, body=body,
                        content_type=resp.content_type or "text/plain",
                    )
        except Exception:
            # 502 — Prodamus повторит уведомление позже.
            logger.exception("Router: forwarding to %s failed", settings.prodamus_forward_url)
            return web.Response(status=502, text="upstream unavailable")

    async def is_ours(form: dict[str, str]) -> bool:
        if PaymentService.is_ours(form):
            return True
        try:
            async with session_factory() as session:
                return await PaymentService(UnitOfWork(session), links).knows_order(form)
        except Exception:
            logger.exception("Router: order lookup failed")
            return False

    async def handle_payment(request: web.Request, form: dict[str, str]) -> web.Response:
        if not verify_webhook_signature(
            form, request.headers.get("Sign", ""), settings.prodamus_secret_key
        ):
            logger.warning("Webhook rejected: bad signature (order_num=%s)", form.get("order_num"))
            return web.Response(status=403, text="bad signature")

        if form.get("payment_status") != "success":
            logger.info("Webhook ignored: payment_status=%s", form.get("payment_status"))
            return web.Response(text="OK")

        try:
            async with session_factory() as session:
                uow = UnitOfWork(session)
                claim = await PaymentService(uow, links).claim_paid(form)
                if claim.payment is None:
                    if not claim.duplicate and PaymentService.is_ours(form):
                        # Деньги пришли, а подписку выдать некому — нужен человек.
                        logger.error(
                            "Unmatched payment: order_num=%s order_id=%s sum=%s customer=%s",
                            form.get("order_num"), form.get("order_id"),
                            form.get("sum"), form.get("customer_extra"),
                        )
                        await alert_owners(messenger, settings.owner_ids, PAYMENT_UNMATCHED_ALERT.format(
                            customer=form.get("customer_extra", "—"),
                            sum=form.get("sum", "—"),
                            order=form.get("order_id", "—"),
                        ))
                    return web.Response(text="OK")
                user = await SubscriptionService(uow, messenger, settings).activate(
                    claim.max_user_id, claim.payment.days
                )
                await uow.commit()
        except Exception:
            # Ничего не закоммичено — Prodamus повторит уведомление.
            logger.exception("Failed to process payment notification")
            return web.Response(status=500, text="error")

        try:
            await on_activated(user)
        except Exception:
            logger.exception("Post-payment notification failed for %s", user.max_user_id)
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/prodamus/webhook", prodamus_webhook)
    app.router.add_post("/prodamus/router", prodamus_router)
    return app
