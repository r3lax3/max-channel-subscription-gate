import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.notify import alert_owners
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
        # Флаг ставим до отправки и при ошибке не снимаем: человек мог остановить
        # бота, и без флага мы бы писали ему каждый час до конца подписки.
        expiring_ids = await uow.users.claim_expiring_notices(EXPIRING_NOTICE_DAYS)
        await uow.commit()
        notified = 0
        for max_user_id in expiring_ids:
            try:
                await messenger.send_text(
                    max_user_id, SUBSCRIPTION_EXPIRING.format(days=EXPIRING_NOTICE_DAYS),
                    renew_button=True,
                )
                notified += 1
            except Exception:
                logger.warning("Failed to notify expiring user %s", max_user_id, exc_info=True)
        stale = await uow.payments.expire_stale_pending(PENDING_PAYMENT_TTL_HOURS)
        await uow.commit()
        expired_ids = await uow.users.get_expired_ids()

    kicked = 0
    kick_failed: list[int] = []
    for max_user_id in expired_ids:
        # Своя транзакция на каждого: блокировка строки держится только на время кика.
        try:
            async with session_factory() as session:
                result = await SubscriptionService(
                    UnitOfWork(session), messenger, settings
                ).expire_if_due(max_user_id)
        except Exception:
            logger.exception("Failed to expire subscription of %s", max_user_id)
            result = False
        if result is False:
            kick_failed.append(max_user_id)
        elif result:
            kicked += 1
            try:
                await messenger.send_text(max_user_id, SUBSCRIPTION_EXPIRED, renew_button=True)
            except Exception:
                logger.warning("Failed to send expiry message to %s", max_user_id, exc_info=True)

    if kick_failed:
        # Скорее всего бот потерял права админа — без вмешательства человека
        # истёкшие подписчики так и останутся в клубе.
        await alert_owners(
            messenger, settings.owner_ids,
            KICK_FAILED_ALERT.format(count=len(kick_failed), ids=", ".join(map(str, kick_failed))),
        )

    strangers = 0
    if settings.reconcile_members:
        async with session_factory() as session:
            strangers = await _remove_strangers(UnitOfWork(session), messenger, settings)
    logger.info(
        "Worker cycle: notified=%d, kicked=%d, expired_payments=%d, strangers=%d",
        notified, kicked, stale, strangers,
    )


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
            if SubscriptionService.has_access(user):
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
