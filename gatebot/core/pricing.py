from dataclasses import dataclass
from datetime import datetime

from core.config import Settings
from core.utils import utcnow


@dataclass(frozen=True)
class Tariff:
    months: int
    days: int
    discount_percent: int
    title: str


TARIFFS: tuple[Tariff, ...] = (
    Tariff(months=1, days=30, discount_percent=0, title="Месяц"),
    Tariff(months=3, days=90, discount_percent=10, title="Квартал"),
    Tariff(months=6, days=180, discount_percent=15, title="Пол года"),
)


def get_tariff(months: int) -> Tariff | None:
    for tariff in TARIFFS:
        if tariff.months == months:
            return tariff
    return None


def promo_active(settings: Settings, now: datetime | None = None) -> bool:
    """Акция «успей купить по старой цене»: активна, пока now < legacy_promo_until."""
    if settings.legacy_promo_until is None:
        return False
    if now is None:
        now = utcnow()
    return now < settings.legacy_promo_until


def monthly_price(settings: Settings, legacy: bool) -> int:
    if legacy or promo_active(settings):
        return settings.subscription_price_legacy
    return settings.subscription_price


def tariff_price(monthly: int, tariff: Tariff) -> int:
    return round(monthly * tariff.months * (100 - tariff.discount_percent) / 100)
