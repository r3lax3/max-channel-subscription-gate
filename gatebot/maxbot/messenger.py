import logging

import texts

from maxbot.client import MaxApiError, MaxClient

logger = logging.getLogger(__name__)


class MaxMessenger:
    """Реализация порта core.ports.Messenger поверх MAX Bot API."""

    def __init__(self, client: MaxClient) -> None:
        self.client = client

    async def send_text(self, user_id: int, text: str, renew_button: bool = False) -> None:
        body: dict = {"text": text, "format": "markdown"}
        if renew_button:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": [
                [{"type": "callback", "text": texts.BTN_RENEW, "payload": "renew"}]
            ]}}]
        await self.client.send_message(user_id=user_id, body=body)

    async def remove_member(self, chat_id: int, user_id: int) -> None:
        # block=False: метода разблокировки в API нет, а после повторной оплаты
        # человек должен суметь вернуться по общей ссылке.
        try:
            await self.client.remove_member(chat_id, user_id, block=False)
        except MaxApiError:
            if await self.is_member(chat_id, user_id):
                raise
            logger.info("User %s is not in chat %s — nothing to remove", user_id, chat_id)

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        page = await self.client.request(
            "GET", f"/chats/{chat_id}/members", params={"user_ids": str(user_id)},
            timeout=10, attempts=2,
        )
        return any(m.get("user_id") == user_id for m in page.get("members", []))

    async def list_plain_member_ids(self, chat_id: int) -> list[int]:
        """Участники чата, кроме ботов, владельца и админов."""
        return [
            m["user_id"]
            for m in await self.client.get_members(chat_id)
            if not (m.get("is_bot") or m.get("is_owner") or m.get("is_admin"))
        ]
