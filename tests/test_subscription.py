from datetime import timedelta

from core.services.subscription import SubscriptionService
from core.utils import utcnow

from .conftest import CHANNEL_ID, GROUP_ID, make_user


async def test_activate_new_user_starts_from_now(uow, messenger, settings):
    await make_user(uow, 10)
    user = await SubscriptionService(uow, messenger, settings).activate(10, 30)
    assert user.is_active
    assert abs(user.subscription_end_date - (utcnow() + timedelta(days=30))) < timedelta(minutes=1)


async def test_activate_extends_active_subscription(uow, messenger, settings):
    end = (await make_user(uow, 10, days_left=5, expiring_notice_sent=True)).subscription_end_date
    user = await SubscriptionService(uow, messenger, settings).activate(10, 30)
    assert user.subscription_end_date == end + timedelta(days=30)
    assert user.expiring_notice_sent is False


async def test_activate_during_promo_grants_legacy(uow, messenger, settings):
    settings.legacy_promo_until = utcnow() + timedelta(days=1)
    await make_user(uow, 10)
    user = await SubscriptionService(uow, messenger, settings).activate(10, 30)
    assert user.legacy_pricing


async def test_kick_removes_from_all_gated_chats(uow, messenger, settings):
    assert await SubscriptionService(uow, messenger, settings).kick(10)
    assert messenger.removed == [(GROUP_ID, 10), (CHANNEL_ID, 10)]


async def test_revoke_keeps_state_when_kick_fails(uow, messenger, settings):
    await make_user(uow, 10, days_left=5)
    messenger.fail_remove_for.add(10)
    assert await SubscriptionService(uow, messenger, settings).revoke(10) is None
    user = await uow.users.get_by_max_id(10)
    assert user.is_active


async def test_revoke_breaks_subscription(uow, messenger, settings):
    await make_user(uow, 10, days_left=5, legacy_pricing=True)
    user = await SubscriptionService(uow, messenger, settings).revoke(10)
    assert not user.is_active and user.subscription_end_date is None and not user.legacy_pricing


async def test_set_end_date_in_past_kicks(uow, messenger, settings):
    await make_user(uow, 10, days_left=5, legacy_pricing=True)
    user = await SubscriptionService(uow, messenger, settings).set_end_date(
        10, utcnow() - timedelta(days=1)
    )
    assert not user.is_active and not user.legacy_pricing
    assert (GROUP_ID, 10) in messenger.removed


async def test_set_end_date_in_future_activates(uow, messenger, settings):
    await make_user(uow, 10)
    user = await SubscriptionService(uow, messenger, settings).set_end_date(
        10, utcnow() + timedelta(days=10)
    )
    assert SubscriptionService.has_active_subscription(user)
    assert messenger.removed == []


async def test_set_end_date_in_past_keeps_state_when_kick_fails(uow, messenger, settings):
    await make_user(uow, 10, days_left=5)
    messenger.fail_remove_for.add(10)
    service = SubscriptionService(uow, messenger, settings)
    assert await service.set_end_date(10, utcnow() - timedelta(days=1)) is None
    assert SubscriptionService.has_active_subscription(await uow.users.get_by_max_id(10))


async def test_expire_skips_user_renewed_meanwhile(uow, messenger, settings):
    await make_user(uow, 10, days_left=10)
    assert await SubscriptionService(uow, messenger, settings).expire_if_due(10) is None
    assert messenger.removed == []
