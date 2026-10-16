import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from core.ports import PaymentLinkFactory
from db.models import Payment, User
from db.uow import UnitOfWork

logger = logging.getLogger(__name__)

_MAX_ORDER_ID_RETRIES = 5
# Префикс customer_extra: отличает наши платежи от платежей TG-бота на том же
# аккаунте Prodamus (тот ждёт там голый telegram_id и наш просто проигнорирует).
CUSTOMER_EXTRA_PREFIX = "max:"


@dataclass
class ClaimResult:
    # Найденный и только что помеченный оплаченным платёж (иначе None)
    payment: Payment | None = None
    max_user_id: int | None = None
    # Платёж найден, но уже был оплачен — повтор уведомления
    duplicate: bool = False


class PaymentService:
    def __init__(self, uow: UnitOfWork, links: PaymentLinkFactory) -> None:
        self.uow = uow
        self.links = links

    async def create_payment_link(
        self,
        max_user_id: int,
        amount: int,
        days: int,
        product_name: str,
    ) -> tuple[int, str]:
        user = await self.uow.users.get_or_create(max_user_id)
        await self.uow.commit()

        order_id: int | None = None
        for _ in range(_MAX_ORDER_ID_RETRIES):
            candidate = secrets.randbits(62)
            try:
                await self.uow.payments.create(
                    Payment(id=candidate, user_id=user.id, amount=amount, days=days, status="pending")
                )
                order_id = candidate
                break
            except IntegrityError:
                await self.uow.rollback()
        if order_id is None:
            raise RuntimeError("Could not allocate unique order_id after retries")
        await self.uow.commit()

        link = await self.links.create_payment_link(
            order_id=order_id,
            amount=amount,
            customer_extra=f"{CUSTOMER_EXTRA_PREFIX}{max_user_id}",
            product_name=product_name,
        )
        logger.info("Payment link created for %s, order %s", max_user_id, order_id)
        return order_id, link

    @staticmethod
    def is_ours(data: dict) -> bool:
        return str(data.get("customer_extra") or "").startswith(CUSTOMER_EXTRA_PREFIX)

    async def claim_paid(self, data: dict) -> ClaimResult:
        """Находит наш платёж по уведомлению Prodamus и атомарно помечает оплаченным.

        Не коммитит: вызывающий коммитит вместе с продлением подписки.
        """
        for payment in await self._candidates(data):
            if not await self.uow.payments.claim_paid(payment.id):
                logger.info("Payment %s already processed — duplicate notification", payment.id)
                return ClaimResult(duplicate=True)
            user = await self.uow.users.get_by_id(payment.user_id)
            if user is None:
                raise LookupError(f"payment {payment.id} has no user")
            logger.info("Payment %s paid by %s", payment.id, user.max_user_id)
            return ClaimResult(payment=payment, max_user_id=user.max_user_id)
        return ClaimResult()

    async def _candidates(self, data: dict) -> list[Payment]:
        # Наш номер заказа Prodamus возвращает в order_num, а в order_id — свой
        # внутренний номер. order_id проверяем на случай иного поведения.
        for field in ("order_num", "order_id"):
            order_id = _parse_int(data.get(field))
            if order_id is None:
                continue
            payment = await self.uow.payments.get_by_order_id(order_id)
            if payment is not None:
                return [payment]

        # Запасной путь: по пользователю из customer_extra и сумме оплаты.
        if not self.is_ours(data):
            return []
        max_user_id = _parse_int(str(data["customer_extra"]).removeprefix(CUSTOMER_EXTRA_PREFIX))
        paid_sum = _parse_sum(data.get("sum"))
        user: User | None = (
            await self.uow.users.get_by_max_id(max_user_id) if max_user_id else None
        )
        if user is None or paid_sum is None:
            return []
        return [
            p for p in await self.uow.payments.get_unpaid_by_user_id(user.id)
            if p.amount == paid_sum
        ][:1]


def _parse_int(value) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 2**63 else None


def _parse_sum(value) -> int | None:
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite() or amount != amount.to_integral_value():
            return None
        return int(amount)
    except (InvalidOperation, ValueError):
        return None
