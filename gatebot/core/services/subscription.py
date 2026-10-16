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

    async def activate(self, user: User, days: int) -> User:
        now = utcnow()
        if user.subscription_end_date and user.subscription_end_date > now:
            user.subscription_end_date += timedelta(days=days)
        else:
            user.subscription_end_date = now + timedelta(days=days)
        user.is_active = True
        user.expiring_notice_sent = False
        if promo_active(self.settings, now):
            user.legacy_pricing = True
        await self.uow.commit()
        logger.info(
            "Subscription activated for %s until %s",
            user.max_user_id, user.subscription_end_date,
        )
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
        if ok:
            logger.info("User %s removed from gated chats", max_user_id)
        return ok

    @staticmethod
    def _break(user: User) -> None:
        user.is_active = False
        user.expiring_notice_sent = False
        user.legacy_pricing = False

    async def set_end_date(self, max_user_id: int, end_date: datetime) -> User | None:
        user = await self.uow.users.get_by_max_id(max_user_id)
        if user is None:
            return None
        user.subscription_end_date = end_date
        if end_date > utcnow():
            user.is_active = True
            user.expiring_notice_sent = False
        else:
            self._break(user)
            await self.kick(max_user_id)
        await self.uow.commit()
        logger.info("Subscription manually set for %s until %s", max_user_id, end_date)
        return user

    async def set_legacy_pricing(self, max_user_id: int, enabled: bool) -> User | None:
        user = await self.uow.users.get_by_max_id(max_user_id)
        if user is None:
            return None
        user.legacy_pricing = enabled
        await self.uow.commit()
        return user

    async def revoke(self, max_user_id: int) -> User | None:
        user = await self.uow.users.get_by_max_id(max_user_id)
        if user is None:
            return None
        if not await self.kick(max_user_id):
            logger.warning("Revoke for %s: kick failed; state left untouched", max_user_id)
            return None
        self._break(user)
        user.subscription_end_date = None
        await self.uow.commit()
        logger.info("Subscription revoked for %s", max_user_id)
        return user
