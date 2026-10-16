from typing import Protocol


class Messenger(Protocol):
    """То, что ядру нужно от мессенджера. Реализуется адаптером MAX."""

    async def send_text(self, user_id: int, text: str) -> None: ...

    async def remove_member(self, chat_id: int, user_id: int) -> None:
        """Удалить пользователя из чата. Отсутствие в чате — не ошибка."""
        ...

    async def list_plain_member_ids(self, chat_id: int) -> list[int]:
        """Участники чата, кроме ботов, владельца и админов."""
        ...


class PaymentLinkFactory(Protocol):
    """Платёжный провайдер (Prodamus)."""

    async def create_payment_link(
        self, order_id: int, amount: int, customer_extra: str, product_name: str
    ) -> str: ...
