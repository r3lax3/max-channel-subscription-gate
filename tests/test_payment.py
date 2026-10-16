from core.services.payment import PaymentService

from .conftest import make_user


async def _order(uow, links, max_user_id=10, amount=2000, days=30) -> int:
    order_id, _ = await PaymentService(uow, links).create_payment_link(max_user_id, amount, days, "x")
    return order_id


async def test_create_payment_link_stores_pending_payment(uow, links):
    order_id, link = await PaymentService(uow, links).create_payment_link(
        max_user_id=10, amount=2000, days=30, product_name="Месяц"
    )
    payment = await uow.payments.get_by_order_id(order_id)
    assert payment.status == "pending" and payment.days == 30
    assert link == f"https://pay.test/{order_id}"
    assert links.calls[0]["customer_extra"] == "max:10"


async def test_claim_by_order_num(uow, links):
    """Prodamus возвращает наш номер заказа в order_num, свой — в order_id."""
    order_id = await _order(uow, links)
    claim = await PaymentService(uow, links).claim_paid(
        {"order_num": str(order_id), "order_id": "300155"}
    )
    assert claim.payment.id == order_id and claim.max_user_id == 10
    assert (await uow.payments.get_by_order_id(order_id)).status == "success"


async def test_claim_fallback_matches_sum_not_latest(uow, links):
    half_year = await _order(uow, links, amount=10200, days=180)
    await _order(uow, links, amount=2000, days=30)
    claim = await PaymentService(uow, links).claim_paid(
        {"customer_extra": "max:10", "sum": "10200.00"}
    )
    assert claim.payment.id == half_year and claim.payment.days == 180


async def test_claim_fallback_accepts_expired_payment(uow, links):
    order_id = await _order(uow, links)
    payment = await uow.payments.get_by_order_id(order_id)
    payment.status = "expired"
    await uow.commit()
    claim = await PaymentService(uow, links).claim_paid({"customer_extra": "max:10", "sum": "2000"})
    assert claim.payment.id == order_id


async def test_claim_fallback_without_matching_sum(uow, links):
    await _order(uow, links)
    claim = await PaymentService(uow, links).claim_paid({"customer_extra": "max:10", "sum": "999"})
    assert claim.payment is None and not claim.duplicate


async def test_claim_ignores_foreign_customer_extra(uow, links):
    """Голый id — платёж TG-бота с общего аккаунта Prodamus, не наш."""
    await _order(uow, links)
    claim = await PaymentService(uow, links).claim_paid({"customer_extra": "10", "sum": "2000"})
    assert claim.payment is None
    assert not PaymentService.is_ours({"customer_extra": "10"})


async def test_claim_twice_is_duplicate(uow, links):
    order_id = await _order(uow, links)
    service = PaymentService(uow, links)
    assert (await service.claim_paid({"order_num": str(order_id)})).payment
    await uow.commit()
    claim = await service.claim_paid({"order_num": str(order_id)})
    assert claim.payment is None and claim.duplicate


async def test_claim_unknown_order(uow, links):
    await make_user(uow, 10)
    for data in ({"order_num": "123"}, {"order_num": "abc"}, {"sum": "nan"}, {}):
        assert (await PaymentService(uow, links).claim_paid(data)).payment is None
