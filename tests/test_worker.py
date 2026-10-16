from datetime import timedelta

from core.services.worker import run_worker_cycle
from core.utils import utcnow
from db.models import Payment
from db.uow import UnitOfWork
from texts import SUBSCRIPTION_EXPIRED

from .conftest import CHANNEL_ID, GROUP_ID, make_user


async def _reload(session_factory, max_user_id):
    async with session_factory() as session:
        return await UnitOfWork(session).users.get_by_max_id(max_user_id)


async def test_notifies_expiring_once(session_factory, uow, messenger, settings):
    await make_user(uow, 10, days_left=2)
    await run_worker_cycle(session_factory, messenger, settings)
    await run_worker_cycle(session_factory, messenger, settings)
    assert [uid for uid, _ in messenger.sent] == [10]


async def test_kicks_expired_and_breaks_legacy(session_factory, uow, messenger, settings):
    await make_user(uow, 20, days_left=-0.1, legacy_pricing=True)
    await run_worker_cycle(session_factory, messenger, settings)
    assert messenger.removed == [(GROUP_ID, 20), (CHANNEL_ID, 20)]
    assert (20, SUBSCRIPTION_EXPIRED) in messenger.sent
    user = await _reload(session_factory, 20)
    assert not user.is_active and not user.legacy_pricing


async def test_kick_failure_keeps_user_active(session_factory, uow, messenger, settings):
    await make_user(uow, 20, days_left=-0.1)
    messenger.fail_remove_for.add(20)
    await run_worker_cycle(session_factory, messenger, settings)
    assert (await _reload(session_factory, 20)).is_active


async def test_expires_stale_pending_payments(session_factory, uow, messenger, settings):
    user = await make_user(uow, 30)
    uow.session.add(Payment(id=1, user_id=user.id, amount=1, created_at=utcnow() - timedelta(hours=49)))
    uow.session.add(Payment(id=2, user_id=user.id, amount=1, created_at=utcnow()))
    await uow.commit()
    await run_worker_cycle(session_factory, messenger, settings)
    async with session_factory() as session:
        payments = UnitOfWork(session).payments
        assert (await payments.get_by_order_id(1)).status == "expired"
        assert (await payments.get_by_order_id(2)).status == "pending"


async def test_reconcile_disabled_by_default(session_factory, messenger, settings):
    messenger.members[GROUP_ID] = [77]
    await run_worker_cycle(session_factory, messenger, settings)
    assert messenger.removed == []


async def test_reconcile_removes_only_strangers(session_factory, uow, messenger, settings):
    settings.reconcile_members = True
    await make_user(uow, 40, days_left=10)
    messenger.members[GROUP_ID] = [40, 77, 1]  # 1 — владелец
    await run_worker_cycle(session_factory, messenger, settings)
    assert messenger.removed == [(GROUP_ID, 77)]


async def test_kick_failure_alerts_owners(session_factory, uow, messenger, settings):
    await make_user(uow, 20, days_left=-0.1)
    messenger.fail_remove_for.add(20)
    await run_worker_cycle(session_factory, messenger, settings)
    owner_msgs = [text for uid, text in messenger.sent if uid == 1]
    assert len(owner_msgs) == 1 and "20" in owner_msgs[0]


async def test_failed_reminder_is_not_retried(session_factory, uow, messenger, settings):
    await make_user(uow, 10, days_left=2)

    async def broken(user_id, text):
        raise RuntimeError("bot stopped")

    messenger.send_text = broken
    await run_worker_cycle(session_factory, messenger, settings)
    assert (await _reload(session_factory, 10)).expiring_notice_sent
