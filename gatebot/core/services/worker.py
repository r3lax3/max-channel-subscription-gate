import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.ports import Messenger
from core.services.subscription import SubscriptionService
from db.uow import UnitOfWork
from texts import KICK_FAILED_ALERT, SUBSCRIPTION_EXPIRED, SUBSCRIPTION_EXPIRING

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
            # Флаг ставим и при ошибке: человек мог остановить бота, и без флага
            # мы бы пытались ему писать каждый час до конца подписки.
            user.expiring_notice_sent = True
            try:
                await messenger.send_text(
                    user.max_user_id, SUBSCRIPTION_EXPIRING.format(days=EXPIRING_NOTICE_DAYS)
                )
                notified += 1
            except Exception:
                logger.warning("Failed to notify expiring user %s", user.max_user_id, exc_info=True)
        await uow.commit()

        kicked = 0
        kick_failed: list[int] = []
        for user in await uow.users.get_expired():
            if not await service.kick(user.max_user_id):
                kick_failed.append(user.max_user_id)
                continue
            user.is_active = False
            user.expiring_notice_sent = False
            user.legacy_pricing = False
            kicked += 1
            await uow.commit()
            try:
                await messenger.send_text(user.max_user_id, SUBSCRIPTION_EXPIRED)
            except Exception:
                logger.warning("Failed to send expiry message to %s", user.max_user_id, exc_info=True)

        if kick_failed:
            # Скорее всего бот потерял права админа — без вмешательства человека
            # истёкшие подписчики так и останутся в клубе.
            await _alert_owners(
                messenger, settings,
                KICK_FAILED_ALERT.format(count=len(kick_failed), ids=", ".join(map(str, kick_failed))),
            )

        stale = await uow.payments.expire_stale_pending(PENDING_PAYMENT_TTL_HOURS)
        await uow.commit()

        strangers = 0
        if settings.reconcile_members:
            strangers = await _remove_strangers(uow, messenger, settings)
        logger.info(
            "Worker cycle: notified=%d, kicked=%d, expired_payments=%d, strangers=%d",
            notified, kicked, stale, strangers,
        )


async def _alert_owners(messenger: Messenger, settings: Settings, text: str) -> None:
    for owner_id in settings.owner_ids:
        try:
            await messenger.send_text(owner_id, text)
        except Exception:
            logger.warning("Failed to alert owner %s", owner_id, exc_info=True)


async def _remove_strangers(uow: UnitOfWork, messenger: Messenger, settings: Settings) -> int:
    """Страховка от пропущенных событий входа: удаляет участников без подписки."""
    removed = 0
    for chat_id in settings.gated_chat_ids:
        try:
            member_ids = await messenger.list_plain_member_ids(chat_id)
        except Exception:
            logger.exception("Failed to list members of %s", chat_id)
            continue
        for max_user_id in member_ids:
            if max_user_id in settings.owner_ids:
                continue
            user = await uow.users.get_by_max_id(max_user_id)
            if SubscriptionService.has_active_subscription(user):
                continue
            try:
                await messenger.remove_member(chat_id, max_user_id)
                removed += 1
                logger.info("Removed stranger %s from %s", max_user_id, chat_id)
            except Exception:
                logger.exception("Failed to remove stranger %s from %s", max_user_id, chat_id)
    return removed


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
