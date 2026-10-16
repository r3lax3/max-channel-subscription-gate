from typing import Protocol


class Messenger(Protocol):
    """То, что ядру нужно от мессенджера. Реализуется адаптером MAX."""

    async def send_text(self, user_id: int, text: str) -> None: ...

    async def remove_member(self, chat_id: int, user_id: int) -> None:
        """Удалить пользователя из чата. Отсутствие в чате — не ошибка."""
        ...


class PaymentLinkFactory(Protocol):
    """Платёжный провайдер (Prodamus)."""

    async def create_payment_link(
        self, order_id: int, amount: int, customer_extra: int, product_name: str
    ) -> str: ...
