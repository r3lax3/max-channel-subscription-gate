import re
from datetime import datetime, timezone

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    debug: bool = False

    database_url: str
    bot_token: str
    # MAX user_id владельцев, которым доступна админка (/admin)
    owner_ids: list[int] = []

    support_link: str = ""
    # Ссылка на бота в MAX (https://max.ru/<bot_username>) — возврат после оплаты
    bot_link: str = ""

    # Закрытые групповые чаты, куда пускаем только с подпиской. Только группы:
    # удалять подписчиков канала бот в MAX не может (см. docs/adr/0001).
    gated_chat_ids: list[int] = []
    # Ссылки-приглашения, которые бот выдаёт оплатившим. Пусто — бот возьмёт
    # ссылку из настроек чата (Chat.link).
    invite_links: list[str] = []
    # Сверять участников закрытых чатов с базой и удалять неоплативших
    # (страховка от пропущенных событий). Включать, когда все свои уже в базе.
    reconcile_members: bool = False

    # Webhook MAX: публичный HTTPS-URL на порту 443. Пусто — long polling.
    max_webhook_url: str = ""
    max_webhook_secret: str = ""

    # Базовая цена руб/месяц; цены тарифов (квартал, полгода) считаются от неё
    subscription_price: int = 2000
    # Цена для «старичков» — непрерывная подписка, оплаченная до даты отсечки
    subscription_price_legacy: int = 1500
    # Окончание акции «успей купить по старой цене». Пусто — акция выключена.
    # Хранится как naive UTC; значение с таймзоной нормализуется.
    legacy_promo_until: datetime | None = None

    @field_validator("legacy_promo_until", mode="before")
    @classmethod
    def _empty_promo_until_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("legacy_promo_until", mode="after")
    @classmethod
    def _promo_until_to_naive_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    prodamus_domain: str = ""
    prodamus_secret_key: str = ""
    # Публичный URL вебхука этого бота (https://<домен>/.../prodamus/webhook).
    # Его же нужно добавить в кабинете Prodamus: «Настройки → URL для уведомлений».
    prodamus_notification_url: str = ""

    webhook_port: int = 8080

    def problems(self) -> list[str]:
        """Ошибки конфигурации, с которыми бот работать не может."""
        errors = []
        if not self.prodamus_domain or not self.prodamus_secret_key:
            errors.append("prodamus_domain и prodamus_secret_key обязательны: без них нет оплат")
        if self.max_webhook_url and not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", self.max_webhook_secret):
            errors.append("max_webhook_secret обязателен при max_webhook_url (5–256 символов [A-Za-z0-9_-])")
        if self.max_webhook_url and not self.max_webhook_url.startswith("https://"):
            errors.append("max_webhook_url должен быть https:// на 443-м порту")
        return errors
