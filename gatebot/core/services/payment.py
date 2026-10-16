import logging
import secrets

from sqlalchemy.exc import IntegrityError

from core.ports import PaymentLinkFactory
from db.models import Payment
from db.uow import UnitOfWork

logger = logging.getLogger(__name__)

_MAX_ORDER_ID_RETRIES = 5
# Префикс customer_extra: отличает наши платежи от платежей TG-бота на том же
# аккаунте Prodamus (тот ждёт там голый telegram_id и наш просто проигнорирует).
CUSTOMER_EXTRA_PREFIX = "max:"


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

    async def mark_paid(self, data: dict) -> Payment | None:
        """Находит ожидающий платёж по данным вебхука Prodamus и помечает успешным.

        Не коммитит: вызывающий коммитит вместе с продлением подписки.
        """
        payment = await self._find_pending(data)
        if payment is None:
            return None
        payment.status = "success"
        logger.info("Payment %s paid by user_id=%s", payment.id, payment.user_id)
        return payment

    async def _find_pending(self, data: dict) -> Payment | None:
        order_id_raw = data.get("order_id")
        customer_extra_raw = data.get("customer_extra")

        payment: Payment | None = None
        if order_id_raw:
            try:
                payment = await self.uow.payments.get_by_order_id(int(order_id_raw))
            except (TypeError, ValueError):
                logger.warning("Webhook: invalid order_id=%r", order_id_raw)

        # Prodamus `order_num` — их внутренний счётчик. Если нашего order_id нет,
        # ищем последний ожидающий платёж пользователя по customer_extra ("max:<id>").
        if payment is None and str(customer_extra_raw or "").startswith(CUSTOMER_EXTRA_PREFIX):
            try:
                max_user_id = int(str(customer_extra_raw).removeprefix(CUSTOMER_EXTRA_PREFIX))
            except ValueError:
                max_user_id = 0
            if max_user_id:
                user = await self.uow.users.get_by_max_id(max_user_id)
                if user is not None:
                    payment = await self.uow.payments.get_latest_pending_by_user_id(user.id)

        if payment is None:
            logger.warning(
                "Webhook ignored: no matching payment (order_id=%s, customer_extra=%s)",
                order_id_raw, customer_extra_raw,
            )
            return None
        if payment.status == "success":
            logger.warning("Webhook ignored: payment %s already processed", payment.id)
            return None
        return payment
