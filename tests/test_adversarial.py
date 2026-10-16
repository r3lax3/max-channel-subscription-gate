"""Регрессии из атакующего ревью (проход 2): каждый тест когда-то доказывал баг."""
import asyncio
from datetime import date

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.services.payment import PaymentService
from core.utils import utcnow
from core.services.subscription import SubscriptionService
from core.services.worker import run_worker_cycle
from db.uow import UnitOfWork
from infrastructure.prodamus import _create_hmac
from infrastructure.webhook import create_webhook_app
from maxbot.bot import GateBot, end_of_day_msk
from maxbot.transport import run_polling

from .conftest import GROUP_ID, FakeMessenger, make_user
from .test_bot import OWNER, FakeClient, callback, dm, user

SECRET = "test_secret"


def signed(form):
    return {"Sign": _create_hmac(form, SECRET)}


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def bot(client, settings, session_factory, links):
    settings.gated_chat_ids = [GROUP_ID]
    return GateBot(client, settings, session_factory, links)


async def make_webapp(settings, session_factory, messenger, links):
    activated = []

    async def on_activated(u):
        activated.append(u.max_user_id)

    tc = TestClient(TestServer(create_webhook_app(settings, session_factory, messenger, links, on_activated)))
    await tc.start_server()
    tc.activated = activated
    return tc


async def new_order(session_factory, links, uid, amount=2000, days=30):
    async with session_factory() as s:
        oid, _ = await PaymentService(UnitOfWork(s), links).create_payment_link(uid, amount, days, "x")
    return oid


def paid_form(oid, uid, prodamus_id="777"):
    return {"order_num": str(oid), "order_id": prodamus_id, "payment_status": "success",
            "sum": "2000.00", "customer_extra": f"max:{uid}"}


# BUG 1 (medium): admin user-card buttons are "adm:revoke"/"adm:extend"/"adm:legacy" without
# the user id; the target is AdminState.selected_max_id. A press on an older card still in the
# chat history acts on whoever was opened last -> wrong person is kicked from the club.
async def test_stale_admin_card_revokes_wrong_user(bot, client, uow, session_factory):
    await make_user(uow, 55, days_left=30)
    await make_user(uow, 66, days_left=30)
    client.members[GROUP_ID] = [55, 66]
    await bot.handle_update(callback(OWNER, "adm:users"))
    await bot.handle_update(dm(OWNER, "55"))      # card of 55 shown
    await bot.handle_update(callback(OWNER, "adm:users"))
    await bot.handle_update(dm(OWNER, "66"))      # card of 66 shown
    # Admin scrolls up and presses "Revoke" on the card of 55: payload carries its id.
    await bot.handle_update(callback(OWNER, "adm:revoke_yes:55"))
    async with session_factory() as s:
        uow2 = UnitOfWork(s)
        assert not SubscriptionService.has_active_subscription(await uow2.users.get_by_max_id(55))
        assert SubscriptionService.has_active_subscription(await uow2.users.get_by_max_id(66))


# BUG 2 (low-medium): admin sets a "lifetime" date 9999-12-31 (accepted by the date prompt);
# any later payment does end_date += timedelta(days) -> OverflowError -> webhook 500 forever:
# money taken, Prodamus retries endlessly, owners never alerted.
async def test_far_future_end_date_breaks_payment(settings, session_factory, messenger, links, uow):
    await make_user(uow, 10)
    async with session_factory() as s:
        await SubscriptionService(UnitOfWork(s), messenger, settings).set_end_date(
            10, end_of_day_msk(date(9999, 12, 31)))
    oid = await new_order(session_factory, links, 10, days=180)
    tc = await make_webapp(settings, session_factory, messenger, links)
    try:
        form = paid_form(oid, 10)
        resp = await tc.post("/prodamus/webhook", data=form, headers=signed(form))
        assert resp.status == 200
    finally:
        await tc.close()


# BUG 3 (low): unauthenticated request with conflicting form keys ("a" and "a[b]") crashes
# _unflatten inside verify_webhook_signature (TypeError) -> 500 instead of 403.
async def test_malformed_form_keys_give_500_before_signature(settings, session_factory, messenger, links):
    tc = await make_webapp(settings, session_factory, messenger, links)
    try:
        resp = await tc.post("/prodamus/webhook", data=[("a", "1"), ("a[b]", "2")],
                             headers={"Sign": "deadbeef"})
        assert resp.status in (400, 403)
    finally:
        await tc.close()


# BUG 4 (low; high impact if it ever happens): handle_update computes the lock key
# (_update_owner / dict lookup) OUTSIDE its try/except. An update with an unhashable
# user_id/update_type raises out of asyncio.gather in run_polling -> polling task dies ->
# main exits(1) -> marker was not saved -> after restart the same batch -> crash loop.
async def test_poison_update_kills_polling(settings, session_factory, links):
    class PollClient(FakeClient):
        calls = 0

        async def get_subscriptions(self):
            return []

        async def get_updates(self, marker, timeout=30, types=None):
            self.calls += 1
            if self.calls == 1:
                return {"updates": [{"update_type": "message_callback",
                                     "callback": {"callback_id": "c", "payload": "pay",
                                                  "user": {"user_id": [1]}}}],
                        "marker": 5}
            raise asyncio.CancelledError  # second poll reached = loop survived

    pc = PollClient()
    gb = GateBot(pc, settings, session_factory, links)
    with pytest.raises(asyncio.CancelledError):
        await run_polling(pc, gb.handle_update, session_factory, [])


# BUG 5 (medium, when reconcile_members=True): on_user_added let people added by an owner
# stay, but reconcile then removed them within an hour. Policy now: the owner is asked and
# grants free access explicitly; with it, reconcile keeps the member.
async def test_reconcile_keeps_member_owner_granted_free_access(bot, client, settings, session_factory):
    settings.reconcile_members = True
    await bot.handle_update({"update_type": "user_added", "chat_id": GROUP_ID,
                             "user": user(77), "inviter_id": OWNER})
    assert client.removed == []
    await bot.handle_update(callback(OWNER, "adm:free:77"))
    await asyncio.gather(*bot._background)
    m = FakeMessenger()
    m.members[GROUP_ID] = [77]
    await run_worker_cycle(session_factory, m, settings)
    assert m.removed == []


# BUG 6 (medium, depends on Prodamus allowing a link to be paid twice within 48 h): a second
# real payment of the same link arrives with the same order_num but a NEW Prodamus order_id.
# It is treated as a duplicate notification: no extension AND no owner alert -> money lost
# silently. Duplicates should be told apart by Prodamus order_id (store it), or at least alert.
async def test_second_payment_of_same_link_is_silently_lost(settings, session_factory, messenger, links):
    oid = await new_order(session_factory, links, 10)
    tc = await make_webapp(settings, session_factory, messenger, links)
    try:
        f1 = paid_form(oid, 10, prodamus_id="777")
        await tc.post("/prodamus/webhook", data=f1, headers=signed(f1))
        f2 = paid_form(oid, 10, prodamus_id="778")
        resp = await tc.post("/prodamus/webhook", data=f2, headers=signed(f2))
        assert resp.status == 200
        assert tc.activated == [10, 10]  # вторая оплата тоже продлила подписку
        async with session_factory() as s:
            u = await UnitOfWork(s).users.get_by_max_id(10)
            assert (u.subscription_end_date - utcnow()).days >= 59
        f2_repeat = dict(f2)
        resp = await tc.post("/prodamus/webhook", data=f2_repeat, headers=signed(f2_repeat))
        assert tc.activated == [10, 10]  # а повтор уведомления — нет
    finally:
        await tc.close()
