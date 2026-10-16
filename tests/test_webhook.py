from datetime import timedelta

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from core.services.payment import PaymentService
from core.utils import utcnow
from db.uow import UnitOfWork
from infrastructure.prodamus import _create_hmac, _unflatten, verify_webhook_signature
from infrastructure.webhook import create_webhook_app

SECRET = "test_secret"



def test_signature_roundtrip_with_nested_products():
    nested = {
        "order_id": "5",
        "payment_status": "success",
        "products": [{"name": "Месяц", "price": "2000.00", "quantity": "1"}],
    }
    form = {
        "order_id": "5",
        "payment_status": "success",
        "products[0][name]": "Месяц",
        "products[0][price]": "2000.00",
        "products[0][quantity]": "1",
    }
    assert verify_webhook_signature(form, _create_hmac(nested, SECRET), SECRET)
    assert not verify_webhook_signature(form, _create_hmac(nested, "other"), SECRET)
    assert not verify_webhook_signature(form, "", SECRET)


@pytest_asyncio.fixture
async def client(settings, session_factory, messenger, links):
    activated = []

    async def on_activated(user):
        activated.append(user.max_user_id)

    app = create_webhook_app(settings, session_factory, messenger, links, on_activated)
    async with TestClient(TestServer(app)) as client:
        client.activated = activated
        yield client


async def _create_order(session_factory, links, max_user_id=10) -> int:
    async with session_factory() as session:
        order_id, _ = await PaymentService(UnitOfWork(session), links).create_payment_link(
            max_user_id, 2000, 30, "x"
        )
    return order_id


async def test_paid_webhook_activates_subscription(client, session_factory, links):
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "777", "payment_status": "success", "sum": "2000.00", "customer_extra": "max:10"}
    resp = await client.post("/prodamus/webhook", data=form, headers={"Sign": _create_hmac(form, SECRET)})
    assert resp.status == 200
    assert client.activated == [10]
    async with session_factory() as session:
        uow = UnitOfWork(session)
        user = await uow.users.get_by_max_id(10)
        assert user.is_active and user.subscription_end_date > utcnow() + timedelta(days=29)
        assert (await uow.payments.get_by_order_id(order_id)).status == "success"


async def test_bad_signature_is_rejected(client, session_factory, links):
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "777", "payment_status": "success", "sum": "2000.00", "customer_extra": "max:10"}
    resp = await client.post("/prodamus/webhook", data=form, headers={"Sign": "deadbeef"})
    assert resp.status == 403
    assert client.activated == []


async def test_repeated_webhook_activates_once(client, session_factory, links):
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "777", "payment_status": "success", "sum": "2000.00", "customer_extra": "max:10"}
    headers = {"Sign": _create_hmac(form, SECRET)}
    await client.post("/prodamus/webhook", data=form, headers=headers)
    await client.post("/prodamus/webhook", data=form, headers=headers)
    assert client.activated == [10]


async def test_health(client):
    assert (await client.get("/health")).status == 200


async def test_unmatched_payment_alerts_owner(client, messenger):
    form = {"order_num": "1", "payment_status": "success", "sum": "5.00", "customer_extra": "max:10"}
    resp = await client.post("/prodamus/webhook", data=form, headers={"Sign": _create_hmac(form, SECRET)})
    assert resp.status == 200
    assert [uid for uid, _ in messenger.sent] == [1]


async def test_foreign_payment_is_silently_ignored(client, messenger):
    form = {"order_num": "1", "payment_status": "success", "sum": "5.00", "customer_extra": "12345"}
    resp = await client.post("/prodamus/webhook", data=form, headers={"Sign": _create_hmac(form, SECRET)})
    assert resp.status == 200 and messenger.sent == []


def test_signature_list_detection_like_php():
    from infrastructure.prodamus import _unflatten

    form = {f"products[{i}][name]": str(i) for i in (1, 0, 10, 2, 3, 4, 5, 6, 7, 8, 9)}
    assert [p["name"] for p in _unflatten(form)["products"]] == [str(i) for i in range(11)]


@pytest_asyncio.fixture
async def upstream():
    """Фейковый TG-бот, куда роутер пересылает чужие уведомления."""
    from aiohttp import web

    received = []

    async def tg_webhook(request):
        received.append({"body": await request.read(), "sign": request.headers.get("Sign"),
                         "type": request.headers.get("Content-Type")})
        return web.Response(text="TG-OK")

    app = web.Application()
    app.router.add_post("/prodamus/webhook", tg_webhook)
    server = TestServer(app)
    await server.start_server()
    server.received = received
    yield server
    await server.close()


async def test_router_forwards_foreign_notification_verbatim(client, settings, upstream):
    settings.prodamus_forward_url = str(upstream.make_url("/prodamus/webhook"))
    form = {"order_num": "5", "payment_status": "success", "customer_extra": "12345",
            "products[0][name]": "Подписка"}
    resp = await client.post("/prodamus/router", data=form, headers={"Sign": "tg-signature"})
    assert resp.status == 200 and await resp.text() == "TG-OK"
    sent = upstream.received[0]
    assert sent["sign"] == "tg-signature"
    assert b"customer_extra=12345" in sent["body"]
    assert sent["type"].startswith("application/x-www-form-urlencoded")
    assert client.activated == []


async def test_router_handles_own_notification(client, settings, session_factory, links, upstream):
    settings.prodamus_forward_url = str(upstream.make_url("/prodamus/webhook"))
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "9", "payment_status": "success",
            "sum": "2000.00", "customer_extra": "max:10"}
    resp = await client.post("/prodamus/router", data=form, headers={"Sign": _create_hmac(form, SECRET)})
    assert resp.status == 200
    assert client.activated == [10] and upstream.received == []


async def test_router_upstream_down_returns_502(client, settings):
    settings.prodamus_forward_url = "http://127.0.0.1:9/prodamus/webhook"
    resp = await client.post("/prodamus/router", data={"customer_extra": "1"}, headers={"Sign": "x"})
    assert resp.status == 502


async def test_router_recognises_own_order_without_customer_extra(client, settings, session_factory, links, upstream):
    settings.prodamus_forward_url = str(upstream.make_url("/prodamus/webhook"))
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "9", "payment_status": "success", "sum": "2000"}
    resp = await client.post("/prodamus/router", data=form, headers={"Sign": _create_hmac(form, SECRET)})
    assert resp.status == 200 and client.activated == [10] and upstream.received == []


def _multipart(form: dict[str, str]):
    """Как шлёт Prodamus: multipart/form-data."""
    import aiohttp

    writer = aiohttp.MultipartWriter("form-data")
    for key, value in form.items():
        part = writer.append(value)
        part.set_content_disposition("form-data", name=key)
    return writer


async def test_router_handles_own_multipart_notification(client, settings, session_factory, links, upstream):
    settings.prodamus_forward_url = str(upstream.make_url("/prodamus/webhook"))
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "9", "payment_status": "success",
            "sum": "2000.00", "customer_extra": "max:10", "products[0][name]": "Подписка — месяц"}
    resp = await client.post("/prodamus/router", data=_multipart(form),
                             headers={"Sign": _create_hmac(_unflatten(form), SECRET)})
    assert resp.status == 200
    assert client.activated == [10] and upstream.received == []


async def test_router_forwards_foreign_multipart_verbatim(client, settings, upstream):
    settings.prodamus_forward_url = str(upstream.make_url("/prodamus/webhook"))
    resp = await client.post("/prodamus/router", data=_multipart({"customer_extra": "12345"}),
                             headers={"Sign": "tg"})
    assert resp.status == 200
    sent = upstream.received[0]
    assert sent["type"].startswith("multipart/form-data") and b"12345" in sent["body"]


def test_parse_form_body_urlencoded_and_multipart():
    from infrastructure.prodamus import parse_form_body

    assert parse_form_body(b"a=1&b=&c=%D1%8F", "application/x-www-form-urlencoded") == {"a": "1", "b": "", "c": "я"}
    body = (b"--XX\r\nContent-Disposition: form-data; name=\"k\"\r\n\r\n\xd0\xb7\r\n"
            b"--XX\r\nContent-Disposition: form-data; name=\"e\"\r\n\r\n\r\n--XX--\r\n")
    assert parse_form_body(body, "multipart/form-data; boundary=XX") == {"k": "з", "e": ""}


async def test_direct_webhook_accepts_multipart(client, session_factory, links):
    order_id = await _create_order(session_factory, links)
    form = {"order_num": str(order_id), "order_id": "9", "payment_status": "success",
            "sum": "2000.00", "customer_extra": "max:10"}
    resp = await client.post("/prodamus/webhook", data=_multipart(form),
                             headers={"Sign": _create_hmac(form, SECRET)})
    assert resp.status == 200 and client.activated == [10]
