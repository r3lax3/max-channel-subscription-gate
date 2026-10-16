from datetime import timedelta

from core.pricing import TARIFFS, monthly_price, promo_active, tariff_price
from core.utils import utcnow


def test_tariff_prices_apply_discounts():
    assert [tariff_price(2000, t) for t in TARIFFS] == [2000, 5400, 10200]


def test_monthly_price_legacy_and_promo(settings):
    assert monthly_price(settings, legacy=False) == 2000
    assert monthly_price(settings, legacy=True) == 1500
    settings.legacy_promo_until = utcnow() + timedelta(hours=1)
    assert promo_active(settings)
    assert monthly_price(settings, legacy=False) == 1500


def test_promo_until_with_timezone_is_normalized_to_utc(settings):
    from core.config import Settings

    s = Settings(_env_file=None, database_url="x", bot_token="t",
                 legacy_promo_until="2026-08-12T21:00:00+03:00")
    assert s.legacy_promo_until.isoformat() == "2026-08-12T18:00:00"
