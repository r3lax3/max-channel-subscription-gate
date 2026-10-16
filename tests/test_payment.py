from core.services.payment import PaymentService

from .conftest import make_user


async def test_create_payment_link_stores_pending_payment(uow, links):
    order_id, link = await PaymentService(uow, links).create_payment_link(
        max_user_id=10, amount=2000, days=30, product_name="Месяц"
    )
    payment = await uow.payments.get_by_order_id(order_id)
    assert payment.status == "pending" and payment.days == 30
    assert link == f"https://pay.test/{order_id}"
    assert links.calls[0]["customer_extra"] == "max:10"


async def test_mark_paid_by_order_id(uow, links):
    order_id, _ = await PaymentService(uow, links).create_payment_link(10, 2000, 30, "x")
    payment = await PaymentService(uow, links).mark_paid({"order_id": str(order_id)})
    assert payment.id == order_id and payment.status == "success"


async def test_mark_paid_falls_back_to_customer_extra(uow, links):
    order_id, _ = await PaymentService(uow, links).create_payment_link(10, 2000, 30, "x")
    payment = await PaymentService(uow, links).mark_paid({"customer_extra": "max:10"})
    assert payment.id == order_id


async def test_mark_paid_ignores_foreign_customer_extra(uow, links):
    """Голый id — платёж TG-бота с общего аккаунта Prodamus, не наш."""
    await PaymentService(uow, links).create_payment_link(10, 2000, 30, "x")
    assert await PaymentService(uow, links).mark_paid({"customer_extra": "10"}) is None


async def test_mark_paid_ignores_already_paid(uow, links):
    service = PaymentService(uow, links)
    order_id, _ = await service.create_payment_link(10, 2000, 30, "x")
    await service.mark_paid({"order_id": str(order_id)})
    await uow.commit()
    assert await service.mark_paid({"order_id": str(order_id)}) is None


async def test_mark_paid_unknown_order(uow, links):
    await make_user(uow, 10)
    assert await PaymentService(uow, links).mark_paid({"order_id": "123"}) is None
