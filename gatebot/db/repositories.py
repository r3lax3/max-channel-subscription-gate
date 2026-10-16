from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.utils import utcnow
from db.models import BotState, Payment, User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_max_id(self, max_user_id: int) -> User | None:
        result = await self.session.execute(
            select(User).where(User.max_user_id == max_user_id)
        )
        return result.scalar_one_or_none()

    async def lock(self, max_user_id: int) -> User | None:
        """SELECT ... FOR UPDATE со свежими данными из БД.

        Все изменения подписки идут под этой блокировкой: вебхук оплаты,
        воркер и админка не перетирают изменения друг друга.
        """
        result = await self.session.execute(
            select(User)
            .where(User.max_user_id == max_user_id)
            # FOR NO KEY UPDATE: не мешает вставке платежей (FK на users).
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        normalized = username.lstrip("@")
        if not normalized:
            return None
        result = await self.session.execute(
            select(User).where(func.lower(User.username) == normalized.lower())
        )
        return result.scalars().first()

    async def get_or_create(
        self,
        max_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        user = await self.get_by_max_id(max_user_id)
        if user is None:
            user = User(max_user_id=max_user_id)
            self.session.add(user)
            try:
                await self.session.flush()
            except IntegrityError:
                await self.session.rollback()
                user = await self.get_by_max_id(max_user_id)
                if user is None:
                    raise
        if username and user.username != username:
            user.username = username
        if first_name and user.first_name != first_name:
            user.first_name = first_name
        return user

    async def get_all(self) -> list[User]:
        result = await self.session.execute(select(User))
        return list(result.scalars().all())

    def _expiring_filter(self, days_ahead: int):
        now = utcnow()
        return (
            User.is_active.is_(True),
            User.subscription_end_date.isnot(None),
            User.subscription_end_date > now,
            User.subscription_end_date <= now + timedelta(days=days_ahead),
        )

    async def claim_expiring_notices(self, days_ahead: int) -> list[tuple[int, datetime]]:
        """Помечает «напоминание отправлено» у истекающих и возвращает их MAX ID.

        Одним UPDATE: параллельное продление (сбрасывает флаг) не перетирается.
        """
        result = await self.session.execute(
            update(User)
            .where(*self._expiring_filter(days_ahead), User.expiring_notice_sent.is_(False))
            .values(expiring_notice_sent=True)
            .returning(User.max_user_id, User.subscription_end_date)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def get_expired_ids(self) -> list[int]:
        result = await self.session.execute(
            select(User.max_user_id).where(
                User.is_active.is_(True),
                User.subscription_end_date.isnot(None),
                User.subscription_end_date <= utcnow(),
            )
        )
        return list(result.scalars().all())

    async def count_total(self) -> int:
        result = await self.session.execute(select(func.count(User.id)))
        return int(result.scalar_one() or 0)

    async def count_active_subscribers(self) -> int:
        result = await self.session.execute(
            select(func.count(User.id)).where(
                User.is_active.is_(True),
                User.subscription_end_date.isnot(None),
                User.subscription_end_date > utcnow(),
            )
        )
        return int(result.scalar_one() or 0)

    async def count_free_access_only(self) -> int:
        """Бесплатный доступ без действующей оплаченной подписки."""
        now = utcnow()
        result = await self.session.execute(
            select(func.count(User.id)).where(
                User.free_access.is_(True),
                ~(User.is_active.is_(True) & (User.subscription_end_date > now)),
            )
        )
        return int(result.scalar_one() or 0)

    async def count_expiring_within(self, days_ahead: int) -> int:
        result = await self.session.execute(
            select(func.count(User.id)).where(*self._expiring_filter(days_ahead))
        )
        return int(result.scalar_one() or 0)


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, payment: Payment) -> Payment:
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def get_by_order_id(self, order_id: int) -> Payment | None:
        return await self.session.get(Payment, order_id)

    async def claim_paid(self, payment_id: int, provider_order_id: str | None) -> bool:
        """Атомарно переводит платёж в success. False — уже был оплачен."""
        result = await self.session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status != "success")
            .values(status="success", provider_order_id=provider_order_id)
        )
        return result.rowcount == 1

    async def try_create(self, payment: Payment) -> bool:
        """Вставка в savepoint. False — нарушена уникальность (такой уже есть)."""
        try:
            async with self.session.begin_nested():
                self.session.add(payment)
        except IntegrityError:
            return False
        return True

    async def get_unpaid_by_user_id(self, user_id: int) -> list[Payment]:
        """Неоплаченные платежи пользователя (pending и протухшие), новые первыми."""
        result = await self.session.execute(
            select(Payment)
            .where(Payment.user_id == user_id, Payment.status != "success")
            .order_by(Payment.created_at.desc(), Payment.id.desc())
        )
        return list(result.scalars().all())

    async def count_by_status(self, status: str) -> int:
        result = await self.session.execute(
            select(func.count(Payment.id)).where(Payment.status == status)
        )
        return int(result.scalar_one() or 0)

    async def sum_revenue(self, days_back: int | None = None) -> int:
        query = select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == "success"
        )
        if days_back is not None:
            query = query.where(Payment.created_at >= utcnow() - timedelta(days=days_back))
        result = await self.session.execute(query)
        return int(result.scalar_one() or 0)

    async def expire_stale_pending(self, ttl_hours: int) -> int:
        result = await self.session.execute(
            update(Payment)
            .where(
                Payment.status == "pending",
                Payment.created_at < utcnow() - timedelta(hours=ttl_hours),
            )
            .values(status="expired")
        )
        return result.rowcount or 0


class BotStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> str | None:
        row = await self.session.get(BotState, key)
        return row.value if row else None

    async def set(self, key: str, value: str) -> None:
        await self.session.merge(BotState(key=key, value=value))
