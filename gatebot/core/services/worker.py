import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.ports import Messenger
from core.services.subscription import SubscriptionService
from db.uow import UnitOfWork
from texts import SUBSCRIPTION_EXPIRED, SUBSCRIPTION_EXPIRING

logger = logging.getLogger(__name__)

WORKER_INTERVAL_SECONDS = 3600
EXPIRING_NOTICE_DAYS = 3
PENDING_PAYMENT_TTL_HOURS = 48


async def run_worker_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    messenger: Messenger,
    settings: Settings,
) -> None:
    """Один проход: напомнить об окончании, исключить истёкших, протухить старые платежи."""
    async with session_factory() as session:
        uow = UnitOfWork(session)
        service = SubscriptionService(uow, messenger, settings)

        notified = 0
        for user in await uow.users.get_expiring_unnotified(EXPIRING_NOTICE_DAYS):
            try:
                await messenger.send_text(
                    user.max_user_id, SUBSCRIPTION_EXPIRING.format(days=EXPIRING_NOTICE_DAYS)
                )
                user.expiring_notice_sent = True
                notified += 1
            except Exception:
                logger.exception("Failed to notify expiring user %s", user.max_user_id)

        kicked = 0
        for user in await uow.users.get_expired():
            if not await service.kick(user.max_user_id):
                continue
            user.is_active = False
            user.expiring_notice_sent = False
            user.legacy_pricing = False
            kicked += 1
            try:
                await messenger.send_text(user.max_user_id, SUBSCRIPTION_EXPIRED)
            except Exception:
                logger.exception("Failed to send expiry message to %s", user.max_user_id)

        stale = await uow.payments.expire_stale_pending(PENDING_PAYMENT_TTL_HOURS)
        await uow.commit()
        logger.info(
            "Worker cycle: notified=%d, kicked=%d, expired_payments=%d",
            notified, kicked, stale,
        )


async def subscription_worker(
    session_factory: async_sessionmaker[AsyncSession],
    messenger: Messenger,
    settings: Settings,
) -> None:
    logger.info("Subscription worker started")
    while True:
        try:
            await run_worker_cycle(session_factory, messenger, settings)
        except Exception:
            logger.exception("Worker iteration failed")
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
