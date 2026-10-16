import logging
from datetime import datetime, timedelta

from core.config import Settings
from core.ports import Messenger
from core.pricing import promo_active
from core.utils import utcnow
from db.models import User
from db.uow import UnitOfWork

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Изменения подписки. Каждый метод берёт блокировку строки пользователя
    (UserRepository.lock) и сам коммитит."""

    def __init__(self, uow: UnitOfWork, messenger: Messenger, settings: Settings) -> None:
        self.uow = uow
        self.messenger = messenger
        self.settings = settings

    @staticmethod
    def has_active_subscription(user: User | None) -> bool:
        if user is None or not user.is_active or user.subscription_end_date is None:
            return False
        return user.subscription_end_date > utcnow()

    @staticmethod
    def is_legacy_client(user: User | None) -> bool:
        """«Старичок»: сохраняет старую цену, пока подписка непрерывна.

        Флаг ставится при оплате во время акции и из админки; сбрасывается при
        разрыве подписки (кик воркером, отзыв, дата в прошлом).
        """
        return user is not None and user.legacy_pricing

    async def activate(self, max_user_id: int, days: int) -> User:
        """Продлевает подписку на days дней. Не коммитит — вызывающий коммитит
        вместе с отметкой об оплате, чтобы оплата и продление не разошлись."""
        user = await self.uow.users.lock(max_user_id)
        if user is None:
            raise LookupError(f"user {max_user_id} not found")
        now = utcnow()
        if self.has_active_subscription(user):
            user.subscription_end_date += timedelta(days=days)
        else:
            user.subscription_end_date = now + timedelta(days=days)
        user.is_active = True
        user.expiring_notice_sent = False
        if promo_active(self.settings, now):
            user.legacy_pricing = True
        logger.info("Subscription of %s extended until %s", max_user_id, user.subscription_end_date)
        return user

    async def kick(self, max_user_id: int) -> bool:
        """Удаляет пользователя из всех закрытых чатов. True — если из всех удалось."""
        ok = True
        for chat_id in self.settings.gated_chat_ids:
            try:
                await self.messenger.remove_member(chat_id, max_user_id)
            except Exception:
                logger.exception("Failed to remove %s from chat %s", max_user_id, chat_id)
                ok = False
        return ok

    @staticmethod
    def _break(user: User) -> None:
        user.is_active = False
        user.expiring_notice_sent = False
        user.legacy_pricing = False

    async def expire_if_due(self, max_user_id: int) -> bool | None:
        """Кикает и деактивирует пользователя, если подписка всё ещё истекла.

        Проверка идёт под блокировкой: оплата, пришедшая во время кика, ждёт
        и затем продлевает подписку поверх. None — уже не истекла (продлили),
        False — кик не удался (состояние не тронуто, повторим позже), True — готово.
        """
        user = await self.uow.users.lock(max_user_id)
        if user is None or not user.is_active or self.has_active_subscription(user):
            await self.uow.rollback()
            return None
        if not await self.kick(max_user_id):
            await self.uow.rollback()
            return False
        self._break(user)
        await self.uow.commit()
        logger.info("Subscription of %s expired, removed from club", max_user_id)
        return True

    async def set_end_date(self, max_user_id: int, end_date: datetime) -> User | None:
        """Ручная установка даты окончания. Дата в прошлом = снятие подписки:
        если кик не удался, ничего не меняем и возвращаем None."""
        user = await self.uow.users.lock(max_user_id)
        if user is None:
            await self.uow.rollback()
            return None
        if end_date > utcnow():
            user.subscription_end_date = end_date
            user.is_active = True
            user.expiring_notice_sent = False
        else:
            if not await self.kick(max_user_id):
                await self.uow.rollback()
                return None
            user.subscription_end_date = end_date
            self._break(user)
        await self.uow.commit()
        logger.info("Subscription of %s manually set until %s", max_user_id, end_date)
        return user

    async def set_legacy_pricing(self, max_user_id: int, enabled: bool) -> User | None:
        user = await self.uow.users.lock(max_user_id)
        if user is None:
            await self.uow.rollback()
            return None
        user.legacy_pricing = enabled
        await self.uow.commit()
        return user

    async def revoke(self, max_user_id: int) -> User | None:
        """Снимает подписку и удаляет из клуба. None — пользователя нет или кик не удался."""
        user = await self.uow.users.lock(max_user_id)
        if user is None:
            await self.uow.rollback()
            return None
        if not await self.kick(max_user_id):
            await self.uow.rollback()
            logger.warning("Revoke for %s: kick failed; state left untouched", max_user_id)
            return None
        self._break(user)
        user.subscription_end_date = None
        await self.uow.commit()
        logger.info("Subscription revoked for %s", max_user_id)
        return user
